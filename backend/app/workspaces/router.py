"""Workspaces, membership, branding and compliance policy."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, EmailStr, Field, ValidationError, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import ROLE_RANK, WorkspaceContext, current_user, ws_admin, ws_owner, ws_viewer
from app.compliance.policy import CompliancePolicy, policy_for
from app.core.audit import audit
from app.core.config import get_settings
from app.core.errors import APIError, bad_request, not_found
from app.core.files import sniff_image
from app.database.base import utcnow
from app.database.models import User, Workspace, WorkspaceMembership
from app.database.session import get_db
from app.workspaces.service import create_workspace
from app.workspaces.transfer import export_workspace, import_workspace

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])

_HEX = r"^#[0-9a-fA-F]{6}$"


class WorkspaceIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)
    brand_color: str = Field(default="#6366f1", pattern=_HEX)
    contact_email: EmailStr | None = None
    contact_phone: str | None = Field(default=None, max_length=32)
    website: str | None = Field(default=None, max_length=300)
    address: str | None = Field(default=None, max_length=1000)
    timezone: str = Field(default="Asia/Kolkata", max_length=64)
    default_country: str = Field(default="IN", pattern=r"^[A-Z]{2}$")

    @field_validator("website")
    @classmethod
    def _web(cls, v: str | None) -> str | None:
        if v and not v.startswith(("http://", "https://")):
            raise ValueError("website must start with http:// or https://")
        return v or None

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("unknown timezone") from exc
        return v


class WorkspacePatch(WorkspaceIn):
    name: str | None = Field(default=None, min_length=1, max_length=120)  # type: ignore[assignment]


class MemberIn(BaseModel):
    email: EmailStr
    role: str = Field(pattern="^(owner|admin|editor|viewer)$")


class MemberPatch(BaseModel):
    role: str = Field(pattern="^(owner|admin|editor|viewer)$")


def ws_out(ws: Workspace, role: str | None = None) -> dict[str, Any]:
    return {
        "id": ws.id, "name": ws.name, "slug": ws.slug, "description": ws.description, "brand_color": ws.brand_color,
        "contact_email": ws.contact_email, "contact_phone": ws.contact_phone, "website": ws.website,
        "address": ws.address, "timezone": ws.timezone, "default_country": ws.default_country,
        "has_logo": bool(ws.logo_path), "created_at": ws.created_at, "role": role,
    }  # fmt: skip


@router.get("")
async def list_workspaces(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    rows = (await db.execute(
        select(WorkspaceMembership).join(Workspace).where(WorkspaceMembership.user_id == user.id,
                                                          Workspace.deleted_at.is_(None)).order_by(Workspace.name)
    )).scalars()  # fmt: skip
    return [ws_out(m.workspace, m.role) for m in rows]


@router.post("", status_code=201)
async def create(body: WorkspaceIn, request: Request, user: User = Depends(current_user),
                 db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    data = body.model_dump()
    name = data.pop("name")
    ws = await create_workspace(db, user, name=name, **data)
    audit(db, "workspace.created", user_id=user.id, workspace_id=ws.id, request=request)
    await db.commit()
    return ws_out(ws, "owner")


@router.get("/{workspace_id}")
async def get_ws(ctx: WorkspaceContext = Depends(ws_viewer)) -> dict[str, Any]:
    return ws_out(ctx.workspace, ctx.role)


@router.patch("/{workspace_id}")
async def update_ws(body: WorkspacePatch, request: Request, ctx: WorkspaceContext = Depends(ws_admin),
                    db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    ws = await db.get(Workspace, ctx.id)
    assert ws is not None
    changes = body.model_dump(exclude_unset=True)
    for k, v in changes.items():
        if v is not None or k in ("contact_email", "contact_phone", "website", "address"):
            setattr(ws, k, v)
    audit(db, "workspace.updated", user_id=ctx.user.id, workspace_id=ws.id, details={"fields": list(changes)},
          request=request)  # fmt: skip
    await db.commit()
    return ws_out(ws, ctx.role)


@router.delete("/{workspace_id}")
async def delete_ws(request: Request, ctx: WorkspaceContext = Depends(ws_owner), db: AsyncSession = Depends(get_db)) -> dict[str, bool]:
    ws = await db.get(Workspace, ctx.id)
    assert ws is not None
    ws.deleted_at = utcnow()
    ws.slug = f"{ws.slug}-deleted-{ws.id[:6]}"
    audit(db, "workspace.deleted", user_id=ctx.user.id, workspace_id=ws.id, request=request)
    await db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ branding
@router.post("/{workspace_id}/logo")
async def upload_logo(request: Request, file: UploadFile = File(...), ctx: WorkspaceContext = Depends(ws_admin),
                      db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    data = await file.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise APIError(413, "too_large", "Logo must be 1 MB or smaller")
    ext = sniff_image(data)
    if ext is None:
        raise bad_request("Logo must be a PNG, JPEG or WebP image", "invalid_file")
    settings = get_settings()
    folder = settings.uploads_dir / ctx.id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"logo.{ext}"
    for old in folder.glob("logo.*"):
        old.unlink(missing_ok=True)
    path.write_bytes(data)
    path.chmod(0o600)
    ws = await db.get(Workspace, ctx.id)
    assert ws is not None
    ws.logo_path = str(path.relative_to(settings.data_dir))
    audit(db, "workspace.logo_updated", user_id=ctx.user.id, workspace_id=ws.id, request=request)
    await db.commit()
    return ws_out(ws, ctx.role)


@router.get("/{workspace_id}/logo")
async def get_logo(ctx: WorkspaceContext = Depends(ws_viewer)) -> Response:
    if not ctx.workspace.logo_path:
        raise not_found("Logo")
    settings = get_settings()
    path = (settings.data_dir / ctx.workspace.logo_path).resolve()
    if not path.is_relative_to(settings.uploads_dir.resolve()) or not path.is_file():
        raise not_found("Logo")
    media = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}[path.suffix.lstrip(".")]
    return FileResponse(path, media_type=media, headers={"Cache-Control": "private, max-age=300"})


# ------------------------------------------------------------------ members
@router.get("/{workspace_id}/members")
async def members(ctx: WorkspaceContext = Depends(ws_viewer), db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    rows = (await db.execute(select(WorkspaceMembership).where(WorkspaceMembership.workspace_id == ctx.id))).scalars()
    return [{"id": m.id, "user_id": m.user_id, "email": m.user.email, "name": m.user.name, "role": m.role}
            for m in rows]  # fmt: skip


@router.post("/{workspace_id}/members", status_code=201)
async def add_member(body: MemberIn, request: Request, ctx: WorkspaceContext = Depends(ws_admin),
                     db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    if ROLE_RANK[body.role] > ROLE_RANK[ctx.role]:
        raise APIError(403, "forbidden", "You cannot grant a role higher than your own")
    user = (await db.execute(select(User).where(User.email == body.email.lower()))).scalar_one_or_none()
    if user is None:
        raise not_found("User (ask an instance administrator to create the account first)")
    exists = (await db.execute(select(WorkspaceMembership).where(
        WorkspaceMembership.workspace_id == ctx.id, WorkspaceMembership.user_id == user.id))).scalar_one_or_none()  # fmt: skip
    if exists:
        raise APIError(409, "conflict", "User is already a member")
    m = WorkspaceMembership(workspace_id=ctx.id, user_id=user.id, role=body.role)
    db.add(m)
    audit(db, "member.added", user_id=ctx.user.id, workspace_id=ctx.id, entity_type="user", entity_id=user.id,
          details={"role": body.role}, request=request)  # fmt: skip
    await db.commit()
    return {"id": m.id, "user_id": user.id, "email": user.email, "name": user.name, "role": m.role}


async def _owner_count(db: AsyncSession, ws_id: str) -> int:
    return (await db.execute(select(func.count(WorkspaceMembership.id)).where(
        WorkspaceMembership.workspace_id == ws_id, WorkspaceMembership.role == "owner"))).scalar_one()  # fmt: skip


@router.patch("/{workspace_id}/members/{member_id}")
async def update_member(member_id: str, body: MemberPatch, request: Request, ctx: WorkspaceContext = Depends(ws_admin),
                        db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    m = await db.get(WorkspaceMembership, member_id)
    if m is None or m.workspace_id != ctx.id:
        raise not_found("Member")
    if ROLE_RANK[body.role] > ROLE_RANK[ctx.role] or ROLE_RANK[m.role] > ROLE_RANK[ctx.role]:
        raise APIError(403, "forbidden", "You cannot change roles above your own")
    if m.role == "owner" and body.role != "owner" and await _owner_count(db, ctx.id) <= 1:
        raise bad_request("A workspace must keep at least one owner")
    m.role = body.role
    audit(db, "member.role_changed", user_id=ctx.user.id, workspace_id=ctx.id, entity_type="user",
          entity_id=m.user_id, details={"role": body.role}, request=request)  # fmt: skip
    await db.commit()
    return {"id": m.id, "role": m.role}


@router.delete("/{workspace_id}/members/{member_id}")
async def remove_member(member_id: str, request: Request, ctx: WorkspaceContext = Depends(ws_admin),
                        db: AsyncSession = Depends(get_db)) -> dict[str, bool]:  # fmt: skip
    m = await db.get(WorkspaceMembership, member_id)
    if m is None or m.workspace_id != ctx.id:
        raise not_found("Member")
    if ROLE_RANK[m.role] > ROLE_RANK[ctx.role]:
        raise APIError(403, "forbidden", "You cannot remove a member with a higher role")
    if m.role == "owner" and await _owner_count(db, ctx.id) <= 1:
        raise bad_request("A workspace must keep at least one owner")
    await db.delete(m)
    audit(db, "member.removed", user_id=ctx.user.id, workspace_id=ctx.id, entity_type="user", entity_id=m.user_id,
          request=request)  # fmt: skip
    await db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ compliance policy
@router.get("/{workspace_id}/compliance")
async def get_policy(ctx: WorkspaceContext = Depends(ws_viewer)) -> dict[str, Any]:
    policy = policy_for(ctx.workspace.compliance)
    return {"policy": policy.model_dump(), "pstn_missing": policy.pstn_prerequisites_missing()}


@router.put("/{workspace_id}/compliance")
async def put_policy(body: dict[str, Any], request: Request, ctx: WorkspaceContext = Depends(ws_admin),
                     db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    try:
        policy = CompliancePolicy.model_validate(body)
    except ValidationError as exc:
        raise bad_request("Invalid compliance policy", "validation_error",
                          errors=json.loads(exc.json(include_url=False, include_input=False))) from exc  # fmt: skip
    ws = await db.get(Workspace, ctx.id)
    assert ws is not None
    before = policy_for(ws.compliance).model_dump()
    ws.compliance = policy.model_dump()
    changed = {k: v for k, v in ws.compliance.items() if before.get(k) != v}
    audit(db, "compliance.policy_updated", user_id=ctx.user.id, workspace_id=ctx.id, details={"changed": changed},
          request=request)  # fmt: skip
    await db.commit()
    return {"policy": ws.compliance, "pstn_missing": policy.pstn_prerequisites_missing()}


# ------------------------------------------------------------------ export / import
@router.get("/{workspace_id}/export")
async def export_ws(request: Request, include_contacts: bool = True, ctx: WorkspaceContext = Depends(ws_admin),
                    db: AsyncSession = Depends(get_db)) -> Response:  # fmt: skip
    data = await export_workspace(db, ctx.id, include_contacts=include_contacts)
    audit(db, "workspace.exported", user_id=ctx.user.id, workspace_id=ctx.id,
          details={"include_contacts": include_contacts}, request=request)  # fmt: skip
    await db.commit()
    return Response(json.dumps(data, default=str, indent=2), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{ctx.workspace.slug}-export.json"'})  # fmt: skip


@router.post("/import", status_code=201)
async def import_ws(request: Request, file: UploadFile = File(...), user: User = Depends(current_user),
                    db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    raw = await file.read(get_settings().max_upload_mb * 1024 * 1024 + 1)
    if len(raw) > get_settings().max_upload_mb * 1024 * 1024:
        raise APIError(413, "too_large", "Export file is too large")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise bad_request("File is not valid JSON", "invalid_file") from exc
    ws, report = await import_workspace(db, user, data)
    audit(db, "workspace.imported", user_id=user.id, workspace_id=ws.id, details=report, request=request)
    await db.commit()
    return {"workspace": ws_out(ws, "owner"), "report": report}
