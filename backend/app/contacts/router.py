"""Contacts / leads: CRUD, search, filters, tags, custom fields, history, export, erasure."""

from __future__ import annotations

import csv
import io
import re
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import Select, String, cast, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import WorkspaceContext, ws_admin, ws_editor, ws_viewer
from app.contacts.csv_safety import escape_cell
from app.contacts.phone import normalize_phone
from app.core.audit import audit
from app.core.config import get_settings
from app.core.errors import APIError, bad_request, not_found
from app.database.base import utcnow
from app.database.models import (
    AuditLog,
    Call,
    CallbackRequest,
    ConsentRecord,
    Contact,
    ContactCustomField,
    Conversation,
    Message,
    Recording,
    SuppressionEntry,
    Transcript,
)
from app.database.session import get_db

router = APIRouter(prefix="/api/workspaces/{workspace_id}/contacts", tags=["contacts"])

STATUSES = ("new", "lead", "contacted", "interested", "not_interested", "callback", "converted", "do_not_call",
            "wrong_number")  # fmt: skip
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class ContactIn(BaseModel):
    first_name: str = Field(default="", max_length=120)
    last_name: str = Field(default="", max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    email: EmailStr | None = None
    company: str | None = Field(default=None, max_length=160)
    tags: list[str] = Field(default_factory=list, max_length=30)
    notes: str = Field(default="", max_length=5000)
    status: str = Field(default="new")
    custom_fields: dict[str, str] = Field(default_factory=dict)

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        out = []
        for t in v:
            t = t.strip().lower()[:40]
            if t and t not in out:
                out.append(t)
        return out

    @field_validator("status")
    @classmethod
    def _status(cls, v: str) -> str:
        if v not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        return v

    @field_validator("custom_fields")
    @classmethod
    def _cf(cls, v: dict[str, str]) -> dict[str, str]:
        if len(v) > 50:
            raise ValueError("too many custom fields")
        for k, val in v.items():
            if not _KEY.match(k):
                raise ValueError(f"invalid custom field key '{k}' (lowercase letters, digits, _)")
            if len(str(val)) > 2000:
                raise ValueError(f"custom field '{k}' is too long")
        return {k: str(val) for k, val in v.items()}


class ContactPatch(ContactIn):
    status: str | None = None  # type: ignore[assignment]
    tags: list[str] | None = None  # type: ignore[assignment]
    custom_fields: dict[str, str] | None = None  # type: ignore[assignment]

    @field_validator("status")
    @classmethod
    def _status(cls, v: str | None) -> str | None:  # type: ignore[override]
        if v is not None and v not in STATUSES:
            raise ValueError("invalid status")
        return v


class BulkTagIn(BaseModel):
    contact_ids: list[str] = Field(min_length=1, max_length=1000)
    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)


def contact_out(c: Contact, *, duplicate_of: str | None = None) -> dict[str, Any]:
    return {
        "id": c.id, "first_name": c.first_name, "last_name": c.last_name, "full_name": c.full_name,
        "phone_raw": c.phone_raw, "phone_e164": c.phone_e164, "phone_valid": bool(c.phone_e164),
        "email": c.email, "company": c.company, "tags": c.tags or [], "notes": c.notes, "status": c.status,
        "source": c.source, "dnd_status": c.dnd_status, "dnd_checked_at": c.dnd_checked_at,
        "is_fictional": c.is_fictional, "created_at": c.created_at, "updated_at": c.updated_at,
        "custom_fields": {f.key: f.value for f in c.custom_fields},
        **({"duplicate_of": duplicate_of} if duplicate_of else {}),
    }  # fmt: skip


async def get_contact(db: AsyncSession, ws_id: str, contact_id: str) -> Contact:
    c = await db.get(Contact, contact_id)
    if c is None or c.workspace_id != ws_id or c.deleted_at is not None:
        raise not_found("Contact")
    return c


async def find_duplicate(db: AsyncSession, ws_id: str, e164: str | None, email: str | None,
                         exclude_id: str | None = None) -> Contact | None:  # fmt: skip
    conds = []
    if e164:
        conds.append(Contact.phone_e164 == e164)
    if email:
        conds.append(func.lower(Contact.email) == email.lower())
    if not conds:
        return None
    q = select(Contact).where(Contact.workspace_id == ws_id, Contact.deleted_at.is_(None), or_(*conds))
    if exclude_id:
        q = q.where(Contact.id != exclude_id)
    return (await db.execute(q.limit(1))).scalar_one_or_none()


def _apply_custom_fields(c: Contact, ws_id: str, fields: dict[str, str]) -> None:
    existing = {f.key: f for f in c.custom_fields}
    for key, value in fields.items():
        if key in existing:
            existing[key].value = value
        else:
            c.custom_fields.append(ContactCustomField(workspace_id=ws_id, key=key, value=value))
    for key, f in existing.items():
        if key not in fields:
            c.custom_fields.remove(f)


def _filtered(ws_id: str, q: str | None, status: str | None, tag: str | None, has_phone: bool | None,
              source: str | None) -> Select[Contact]:  # fmt: skip
    stmt = select(Contact).where(Contact.workspace_id == ws_id, Contact.deleted_at.is_(None))
    if q:
        like = f"%{q.strip().lower()[:100]}%"
        stmt = stmt.where(or_(func.lower(Contact.first_name).like(like), func.lower(Contact.last_name).like(like),
                              func.lower(Contact.email).like(like), Contact.phone_e164.like(like),
                              func.lower(Contact.company).like(like)))  # fmt: skip
    if status:
        stmt = stmt.where(Contact.status == status)
    if tag:
        stmt = stmt.where(cast(Contact.tags, String).like(f'%"{tag.lower()}"%'))  # JSON array text match
    if has_phone is True:
        stmt = stmt.where(Contact.phone_e164.is_not(None))
    elif has_phone is False:
        stmt = stmt.where(Contact.phone_e164.is_(None))
    if source:
        stmt = stmt.where(Contact.source == source)
    return stmt


@router.get("")
async def list_contacts(
    ctx: WorkspaceContext = Depends(ws_viewer), db: AsyncSession = Depends(get_db),
    q: str | None = Query(None, max_length=100), status: str | None = None, tag: str | None = Query(None, max_length=40),
    has_phone: bool | None = None, source: str | None = Query(None, max_length=40),
    page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=200),
    sort: str = Query("-created_at", pattern=r"^-?(created_at|first_name|last_name|status|updated_at)$"),
) -> dict[str, Any]:  # fmt: skip
    stmt = _filtered(ctx.id, q, status, tag, has_phone, source)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    col = getattr(Contact, sort.lstrip("-"))
    stmt = stmt.order_by(col.desc() if sort.startswith("-") else col.asc(), Contact.id)
    rows = (await db.execute(stmt.offset((page - 1) * page_size).limit(page_size))).scalars().all()
    tags = await all_tags(db, ctx.id)
    return {"items": [contact_out(c) for c in rows], "total": total, "page": page, "page_size": page_size, "tags": tags}


async def all_tags(db: AsyncSession, ws_id: str) -> list[str]:
    rows = (await db.execute(select(Contact.tags).where(Contact.workspace_id == ws_id, Contact.deleted_at.is_(None)))).scalars()
    seen: set[str] = set()
    for t in rows:
        seen.update(t or [])
    return sorted(seen)


@router.post("", status_code=201)
async def create_contact(body: ContactIn, request: Request, ctx: WorkspaceContext = Depends(ws_editor),
                         db: AsyncSession = Depends(get_db), allow_duplicate: bool = False) -> dict[str, Any]:  # fmt: skip
    phone = normalize_phone(body.phone, ctx.workspace.default_country) if body.phone else None
    if body.phone and phone and not phone.valid:
        raise bad_request(f"Invalid phone number: {phone.error}", "invalid_phone")
    if not (body.first_name or body.last_name or body.phone or body.email):
        raise bad_request("Provide at least a name, phone or email")
    dup = await find_duplicate(db, ctx.id, phone.e164 if phone else None, body.email)
    if dup and not allow_duplicate:
        raise APIError(409, "duplicate_contact", "A contact with this phone or email already exists",
                       {"duplicate_id": dup.id})  # fmt: skip
    c = Contact(workspace_id=ctx.id, first_name=body.first_name.strip(), last_name=body.last_name.strip(),
                phone_raw=body.phone, phone_e164=phone.e164 if phone else None,
                email=body.email.lower() if body.email else None, company=body.company, tags=body.tags,
                notes=body.notes, status=body.status, source="manual", custom_fields=[])  # fmt: skip
    _apply_custom_fields(c, ctx.id, body.custom_fields)
    db.add(c)
    await db.flush()
    audit(db, "contact.created", user_id=ctx.user.id, workspace_id=ctx.id, entity_type="contact", entity_id=c.id,
          request=request)  # fmt: skip
    await db.commit()
    return contact_out(c)


@router.get("/export")
async def export_contacts(request: Request, ctx: WorkspaceContext = Depends(ws_admin), db: AsyncSession = Depends(get_db),
                          q: str | None = None, status: str | None = None, tag: str | None = None) -> Response:  # fmt: skip
    rows = (await db.execute(_filtered(ctx.id, q, status, tag, None, None).order_by(Contact.created_at))).scalars().all()
    keys = sorted({f.key for c in rows for f in c.custom_fields})
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "first_name", "last_name", "phone_e164", "email", "company", "status", "tags", "source",
                "dnd_status", "notes", "created_at", *keys])  # fmt: skip
    for c in rows:
        cf = {f.key: f.value for f in c.custom_fields}
        w.writerow([escape_cell(v) for v in (c.id, c.first_name, c.last_name, c.phone_e164, c.email, c.company,
                    c.status, ";".join(c.tags or []), c.source, c.dnd_status, c.notes, c.created_at.isoformat(),
                    *[cf.get(k, "") for k in keys])])  # fmt: skip
    audit(db, "contact.exported", user_id=ctx.user.id, workspace_id=ctx.id, details={"count": len(rows)},
          request=request)  # fmt: skip
    await db.commit()
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{ctx.workspace.slug}-contacts.csv"'})  # fmt: skip


@router.get("/duplicates")
async def duplicates(ctx: WorkspaceContext = Depends(ws_viewer), db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    stmt = (select(Contact.phone_e164, func.count(Contact.id)).where(
        Contact.workspace_id == ctx.id, Contact.deleted_at.is_(None), Contact.phone_e164.is_not(None))
        .group_by(Contact.phone_e164).having(func.count(Contact.id) > 1))  # fmt: skip
    groups = []
    for phone, _n in (await db.execute(stmt)).all():
        members = (await db.execute(select(Contact).where(Contact.workspace_id == ctx.id, Contact.phone_e164 == phone,
                                                          Contact.deleted_at.is_(None)))).scalars()  # fmt: skip
        groups.append({"phone_e164": phone, "contacts": [contact_out(c) for c in members]})
    return groups


@router.post("/bulk-tags")
async def bulk_tags(body: BulkTagIn, ctx: WorkspaceContext = Depends(ws_editor), db: AsyncSession = Depends(get_db)) -> dict[str, int]:
    rows = (await db.execute(select(Contact).where(Contact.workspace_id == ctx.id, Contact.id.in_(body.contact_ids),
                                                   Contact.deleted_at.is_(None)))).scalars().all()  # fmt: skip
    add = [t.strip().lower()[:40] for t in body.add if t.strip()]
    remove = {t.strip().lower() for t in body.remove}
    for c in rows:
        tags = [t for t in (c.tags or []) if t not in remove]
        tags += [t for t in add if t not in tags]
        c.tags = tags
    await db.commit()
    return {"updated": len(rows)}


@router.get("/{contact_id}")
async def get_one(contact_id: str, ctx: WorkspaceContext = Depends(ws_viewer), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return contact_out(await get_contact(db, ctx.id, contact_id))


@router.patch("/{contact_id}")
async def update_contact(contact_id: str, body: ContactPatch, request: Request, ctx: WorkspaceContext = Depends(ws_editor),
                         db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    c = await get_contact(db, ctx.id, contact_id)
    data = body.model_dump(exclude_unset=True)
    if "phone" in data:
        if data["phone"]:
            phone = normalize_phone(data["phone"], ctx.workspace.default_country)
            if not phone.valid:
                raise bad_request(f"Invalid phone number: {phone.error}", "invalid_phone")
            dup = await find_duplicate(db, ctx.id, phone.e164, None, exclude_id=c.id)
            if dup:
                raise APIError(409, "duplicate_contact", "Another contact already has this phone number",
                               {"duplicate_id": dup.id})  # fmt: skip
            c.phone_raw, c.phone_e164 = data["phone"], phone.e164
        else:
            c.phone_raw = c.phone_e164 = None
    for field in ("first_name", "last_name", "company", "notes", "status", "tags"):
        if field in data and data[field] is not None:
            setattr(c, field, data[field])
    if "email" in data:
        c.email = data["email"].lower() if data["email"] else None
    if data.get("custom_fields") is not None:
        _apply_custom_fields(c, ctx.id, data["custom_fields"])
    audit(db, "contact.updated", user_id=ctx.user.id, workspace_id=ctx.id, entity_type="contact", entity_id=c.id,
          details={"fields": [k for k in data if k != "notes"]}, request=request)  # fmt: skip
    await db.commit()
    return contact_out(await get_contact(db, ctx.id, c.id))


@router.delete("/{contact_id}")
async def delete_contact(contact_id: str, request: Request, ctx: WorkspaceContext = Depends(ws_editor),
                         db: AsyncSession = Depends(get_db)) -> dict[str, bool]:  # fmt: skip
    c = await get_contact(db, ctx.id, contact_id)
    c.deleted_at = utcnow()
    audit(db, "contact.deleted", user_id=ctx.user.id, workspace_id=ctx.id, entity_type="contact", entity_id=c.id,
          request=request)  # fmt: skip
    await db.commit()
    return {"ok": True}


@router.post("/{contact_id}/erase")
async def erase_contact(contact_id: str, request: Request, keep_suppression: bool = True,
                        ctx: WorkspaceContext = Depends(ws_admin), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    """Data-deletion workflow: remove personal data, transcripts and recordings for a contact.

    If ``keep_suppression`` is true, the phone number stays on the suppression list so the
    person is never called again (the minimum data needed to honour an opt-out)."""
    c = await db.get(Contact, contact_id)
    if c is None or c.workspace_id != ctx.id:
        raise not_found("Contact")
    settings = get_settings()
    calls = (await db.execute(select(Call).where(Call.workspace_id == ctx.id, Call.contact_id == c.id))).scalars().all()
    removed_files = 0
    for call in calls:
        rec = (await db.execute(select(Recording).where(Recording.call_id == call.id))).scalar_one_or_none()
        if rec:
            path = (settings.data_dir / rec.relative_path).resolve()
            if path.is_relative_to(settings.recordings_dir.resolve()):
                path.unlink(missing_ok=True)
                removed_files += 1
            await db.delete(rec)
        await db.execute(delete(Transcript).where(Transcript.call_id == call.id))
        conv_ids = select(Conversation.id).where(Conversation.call_id == call.id, Conversation.workspace_id == ctx.id)
        await db.execute(delete(Message).where(Message.workspace_id == ctx.id, Message.conversation_id.in_(conv_ids)))
        await db.execute(update(Conversation).where(Conversation.call_id == call.id)
                         .values(tool_log=[], effects=[], system_prompt=""))  # prompt may name the contact
        call.summary = None
        call.to_number = call.from_number = None
        call.contact_id = None
    if keep_suppression and c.phone_e164:
        exists = (await db.execute(select(SuppressionEntry).where(SuppressionEntry.workspace_id == ctx.id,
                  SuppressionEntry.phone_e164 == c.phone_e164))).scalar_one_or_none()  # fmt: skip
        if not exists:
            db.add(SuppressionEntry(workspace_id=ctx.id, phone_e164=c.phone_e164, reason="erasure", source="erasure",
                                    created_by=ctx.user.id))  # fmt: skip
    await db.execute(delete(ConsentRecord).where(ConsentRecord.workspace_id == ctx.id, ConsentRecord.contact_id == c.id))
    await db.execute(delete(CallbackRequest).where(CallbackRequest.workspace_id == ctx.id, CallbackRequest.contact_id == c.id))
    await db.delete(c)
    audit(db, "contact.erased", user_id=ctx.user.id, workspace_id=ctx.id, entity_type="contact", entity_id=contact_id,
          details={"calls": len(calls), "recordings_deleted": removed_files, "kept_suppression": keep_suppression},
          request=request)  # fmt: skip
    await db.commit()
    return {"ok": True, "calls_scrubbed": len(calls), "recordings_deleted": removed_files}


@router.get("/{contact_id}/history")
async def history(contact_id: str, ctx: WorkspaceContext = Depends(ws_viewer), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    c = await get_contact(db, ctx.id, contact_id)
    calls = (await db.execute(select(Call).where(Call.workspace_id == ctx.id, Call.contact_id == c.id)
                              .order_by(Call.created_at.desc()).limit(100))).scalars()  # fmt: skip
    consents = (await db.execute(select(ConsentRecord).where(ConsentRecord.workspace_id == ctx.id,
                or_(ConsentRecord.contact_id == c.id, ConsentRecord.phone_e164 == c.phone_e164))
                .order_by(ConsentRecord.obtained_at.desc()))).scalars()  # fmt: skip
    callbacks = (await db.execute(select(CallbackRequest).where(CallbackRequest.workspace_id == ctx.id,
                 CallbackRequest.contact_id == c.id).order_by(CallbackRequest.created_at.desc()))).scalars()  # fmt: skip
    events = (await db.execute(select(AuditLog).where(AuditLog.workspace_id == ctx.id, AuditLog.entity_id == c.id)
              .order_by(AuditLog.created_at.desc()).limit(50))).scalars()  # fmt: skip
    suppressed = None
    if c.phone_e164:
        s = (await db.execute(select(SuppressionEntry).where(SuppressionEntry.workspace_id == ctx.id,
             SuppressionEntry.phone_e164 == c.phone_e164))).scalar_one_or_none()  # fmt: skip
        suppressed = {"reason": s.reason, "created_at": s.created_at} if s else None
    return {
        "calls": [{"id": x.id, "mode": x.mode, "status": x.status, "outcome": x.outcome, "started_at": x.started_at,
                   "duration_seconds": x.duration_seconds, "summary": x.summary} for x in calls],
        "consents": [{"id": x.id, "purpose": x.purpose, "status": x.status, "source": x.source,
                      "obtained_at": x.obtained_at, "expires_at": x.expires_at, "evidence": x.evidence} for x in consents],
        "callbacks": [{"id": x.id, "kind": x.kind, "preferred_time": x.preferred_time, "status": x.status,
                       "created_at": x.created_at} for x in callbacks],
        "events": [{"action": e.action, "created_at": e.created_at} for e in events],
        "suppressed": suppressed,
    }  # fmt: skip
