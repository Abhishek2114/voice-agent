"""Consent records, suppression list, DND scrub imports, eligibility preview and audit log."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import WorkspaceContext, ws_admin, ws_editor, ws_viewer
from app.compliance.service import check_eligibility, record_opt_out
from app.contacts.phone import normalize_phone
from app.core.audit import audit
from app.core.errors import bad_request, not_found
from app.database.base import utcnow
from app.database.models import AuditLog, ConsentRecord, Contact, SuppressionEntry
from app.database.session import get_db

router = APIRouter(prefix="/api/workspaces/{workspace_id}/compliance", tags=["compliance"])

Purpose = Literal["promotional", "service", "transactional", "inquiry_followup"]
Source = Literal["verbal_call", "web_form", "written", "digital_consent", "customer_inquiry", "other"]


class ConsentIn(BaseModel):
    contact_id: str
    purpose: Purpose
    source: Source
    evidence: str = Field(min_length=3, max_length=2000, description="Where/how consent was captured")
    obtained_at: datetime | None = None
    expires_at: datetime | None = None


class SuppressionIn(BaseModel):
    phone: str = Field(max_length=40)
    reason: Literal["opt_out", "dnd_registry", "manual", "wrong_number"] = "manual"
    notes: str = Field(default="", max_length=500)


class ScrubEntry(BaseModel):
    phone: str = Field(max_length=40)
    status: Literal["registered", "not_registered"]


class ScrubIn(BaseModel):
    entries: list[ScrubEntry] = Field(min_length=1, max_length=50_000)
    checked_at: datetime | None = None
    source: str = Field(default="operator_scrub", max_length=60)


class EligibilityIn(BaseModel):
    contact_id: str
    purpose: Purpose = "promotional"
    mode: Literal["SIMULATION", "PSTN"] = "SIMULATION"


def consent_out(c: ConsentRecord) -> dict[str, Any]:
    return {"id": c.id, "contact_id": c.contact_id, "phone_e164": c.phone_e164, "purpose": c.purpose,
            "status": c.status, "source": c.source, "evidence": c.evidence, "obtained_at": c.obtained_at,
            "expires_at": c.expires_at, "revoked_at": c.revoked_at, "call_id": c.call_id}  # fmt: skip


@router.get("/consents")
async def list_consents(ctx: WorkspaceContext = Depends(ws_viewer), db: AsyncSession = Depends(get_db),
                        contact_id: str | None = None, page: int = Query(1, ge=1)) -> dict[str, Any]:  # fmt: skip
    stmt = select(ConsentRecord).where(ConsentRecord.workspace_id == ctx.id)
    if contact_id:
        stmt = stmt.where(ConsentRecord.contact_id == contact_id)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(ConsentRecord.obtained_at.desc()).offset((page - 1) * 50).limit(50))).scalars()
    return {"items": [consent_out(c) for c in rows], "total": total}


@router.post("/consents", status_code=201)
async def add_consent(body: ConsentIn, request: Request, ctx: WorkspaceContext = Depends(ws_editor),
                      db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    contact = await db.get(Contact, body.contact_id)
    if contact is None or contact.workspace_id != ctx.id or contact.deleted_at is not None:
        raise not_found("Contact")
    if body.expires_at and body.obtained_at and body.expires_at <= body.obtained_at:
        raise bad_request("expires_at must be after obtained_at")
    rec = ConsentRecord(workspace_id=ctx.id, contact_id=contact.id, phone_e164=contact.phone_e164,
                        purpose=body.purpose, source=body.source, evidence=body.evidence,
                        obtained_at=body.obtained_at or utcnow(), expires_at=body.expires_at,
                        recorded_by=ctx.user.id)  # fmt: skip
    db.add(rec)
    await db.flush()
    audit(db, "consent.recorded", user_id=ctx.user.id, workspace_id=ctx.id, entity_type="contact",
          entity_id=contact.id, details={"purpose": body.purpose, "source": body.source}, request=request)  # fmt: skip
    await db.commit()
    return consent_out(rec)


@router.post("/consents/{consent_id}/revoke")
async def revoke_consent(consent_id: str, request: Request, ctx: WorkspaceContext = Depends(ws_editor),
                         db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    rec = await db.get(ConsentRecord, consent_id)
    if rec is None or rec.workspace_id != ctx.id:
        raise not_found("Consent record")
    rec.status, rec.revoked_at = "revoked", utcnow()
    audit(db, "consent.revoked", user_id=ctx.user.id, workspace_id=ctx.id, entity_type="contact",
          entity_id=rec.contact_id, details={"purpose": rec.purpose}, request=request)  # fmt: skip
    await db.commit()
    return consent_out(rec)


@router.get("/suppression")
async def list_suppression(ctx: WorkspaceContext = Depends(ws_viewer), db: AsyncSession = Depends(get_db),
                           page: int = Query(1, ge=1), q: str | None = Query(None, max_length=40)) -> dict[str, Any]:  # fmt: skip
    stmt = select(SuppressionEntry).where(SuppressionEntry.workspace_id == ctx.id)
    if q:
        stmt = stmt.where(SuppressionEntry.phone_e164.like(f"%{q.strip()}%"))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(SuppressionEntry.created_at.desc()).offset((page - 1) * 50).limit(50))).scalars()
    return {"items": [{"id": s.id, "phone_e164": s.phone_e164, "reason": s.reason, "source": s.source,
                       "notes": s.notes, "created_at": s.created_at} for s in rows], "total": total}  # fmt: skip


@router.post("/suppression", status_code=201)
async def add_suppression(body: SuppressionIn, request: Request, ctx: WorkspaceContext = Depends(ws_editor),
                          db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    phone = normalize_phone(body.phone, ctx.workspace.default_country)
    if not phone.valid or not phone.e164:
        raise bad_request(f"Invalid phone number: {phone.error}", "invalid_phone")
    contact = (await db.execute(select(Contact).where(Contact.workspace_id == ctx.id, Contact.phone_e164 == phone.e164,
               Contact.deleted_at.is_(None)).limit(1))).scalar_one_or_none()  # fmt: skip
    if body.reason == "opt_out":
        await record_opt_out(db, ctx.id, phone_e164=phone.e164, contact_id=contact.id if contact else None,
                             source="manual", user_id=ctx.user.id)  # fmt: skip
    else:
        exists = (await db.execute(select(SuppressionEntry).where(SuppressionEntry.workspace_id == ctx.id,
                  SuppressionEntry.phone_e164 == phone.e164))).scalar_one_or_none()  # fmt: skip
        if exists is None:
            db.add(SuppressionEntry(workspace_id=ctx.id, phone_e164=phone.e164, reason=body.reason, source="manual",
                                    notes=body.notes, created_by=ctx.user.id))  # fmt: skip
        audit(db, "suppression.added", user_id=ctx.user.id, workspace_id=ctx.id, details={"reason": body.reason},
              request=request)  # fmt: skip
    await db.commit()
    return {"ok": True, "phone_e164": phone.e164}


@router.delete("/suppression/{entry_id}")
async def remove_suppression(entry_id: str, request: Request, ctx: WorkspaceContext = Depends(ws_admin),
                             db: AsyncSession = Depends(get_db)) -> dict[str, bool]:  # fmt: skip
    entry = await db.get(SuppressionEntry, entry_id)
    if entry is None or entry.workspace_id != ctx.id:
        raise not_found("Suppression entry")
    await db.delete(entry)
    audit(db, "suppression.removed", user_id=ctx.user.id, workspace_id=ctx.id, details={"reason": entry.reason},
          request=request)  # fmt: skip
    await db.commit()
    return {"ok": True}


@router.post("/dnd-scrub")
async def import_scrub(body: ScrubIn, request: Request, ctx: WorkspaceContext = Depends(ws_admin),
                       db: AsyncSession = Depends(get_db)) -> dict[str, int]:  # fmt: skip
    """Apply DND/NCPR scrub results obtained by the operator through their authorised channel."""
    checked = body.checked_at or utcnow()
    updated = invalid = unmatched = 0
    for e in body.entries:
        phone = normalize_phone(e.phone, ctx.workspace.default_country)
        if not phone.e164:
            invalid += 1
            continue
        rows = (await db.execute(select(Contact).where(Contact.workspace_id == ctx.id,
                Contact.phone_e164 == phone.e164))).scalars().all()  # fmt: skip
        if not rows:
            unmatched += 1
        for c in rows:
            c.dnd_status, c.dnd_checked_at = e.status, checked
            updated += 1
    audit(db, "compliance.dnd_scrub_imported", user_id=ctx.user.id, workspace_id=ctx.id,
          details={"updated": updated, "invalid": invalid, "unmatched": unmatched, "source": body.source},
          request=request)  # fmt: skip
    await db.commit()
    return {"updated": updated, "invalid": invalid, "unmatched": unmatched}


@router.post("/eligibility")
async def eligibility(body: EligibilityIn, ctx: WorkspaceContext = Depends(ws_viewer),
                      db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    contact = await db.get(Contact, body.contact_id)
    if contact is None or contact.workspace_id != ctx.id:
        raise not_found("Contact")
    result = await check_eligibility(db, ctx.workspace, contact, purpose=body.purpose, mode=body.mode)
    return result.to_dict()


@router.get("/audit")
async def audit_log(ctx: WorkspaceContext = Depends(ws_admin), db: AsyncSession = Depends(get_db),
                    action: str | None = Query(None, max_length=64), page: int = Query(1, ge=1)) -> dict[str, Any]:  # fmt: skip
    stmt = select(AuditLog).where(AuditLog.workspace_id == ctx.id)
    if action:
        stmt = stmt.where(AuditLog.action.like(f"{action}%"))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(AuditLog.created_at.desc()).offset((page - 1) * 50).limit(50))).scalars()
    return {"items": [{"id": a.id, "action": a.action, "user_id": a.user_id, "entity_type": a.entity_type,
                       "entity_id": a.entity_id, "details": a.details, "ip": a.ip, "created_at": a.created_at}
                      for a in rows], "total": total}  # fmt: skip
