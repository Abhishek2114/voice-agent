"""Voice session signalling (dashboard + optional public "talk to us" links)."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audio.sessions import manager
from app.auth.deps import WorkspaceContext, ws_editor, ws_viewer
from app.core.audit import audit, client_ip
from app.core.config import get_settings
from app.core.errors import APIError, not_found
from app.core.ratelimit import limiter
from app.core.security import token_hash
from app.database.models import Agent, Contact, Workspace
from app.database.session import get_db

router = APIRouter(tags=["voice"])

MAX_SDP = 20_000


class OfferIn(BaseModel):
    agent_id: str
    sdp: str = Field(min_length=10, max_length=MAX_SDP)
    type: Literal["offer"] = "offer"
    contact_id: str | None = None


class ControlIn(BaseModel):
    type: Literal["interrupt", "mute", "end"]
    muted: bool | None = None


class PublicOfferIn(BaseModel):
    sdp: str = Field(min_length=10, max_length=MAX_SDP)
    type: Literal["offer"] = "offer"


@router.post("/api/workspaces/{workspace_id}/voice/sessions", status_code=201)
async def start_session(body: OfferIn, request: Request, ctx: WorkspaceContext = Depends(ws_editor),
                        db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    agent = await db.get(Agent, body.agent_id)
    if agent is None or agent.workspace_id != ctx.id or agent.deleted_at is not None:
        raise not_found("Agent")
    contact = None
    if body.contact_id:
        contact = await db.get(Contact, body.contact_id)
        if contact is None or contact.workspace_id != ctx.id:
            raise not_found("Contact")
    call_id, answer = await manager.start(ws=ctx.workspace, agent=agent, sdp=body.sdp, sdp_type=body.type,
                                          user_id=ctx.user.id, channel="dashboard", contact=contact)  # fmt: skip
    audit(db, "voice.session_started", user_id=ctx.user.id, workspace_id=ctx.id, entity_type="call", entity_id=call_id,
          request=request)  # fmt: skip
    await db.commit()
    return {"call_id": call_id, "sdp": answer.sdp, "type": answer.type}


@router.post("/api/workspaces/{workspace_id}/voice/sessions/{call_id}/control")
async def control(call_id: str, body: ControlIn, ctx: WorkspaceContext = Depends(ws_editor)) -> dict[str, bool]:
    manager.get(call_id, ctx.id)
    if body.type == "end":
        await manager.end(call_id, ctx.id)
    else:
        await manager.control(call_id, body.model_dump())
    return {"ok": True}


@router.get("/api/workspaces/{workspace_id}/voice/sessions")
async def active_sessions(ctx: WorkspaceContext = Depends(ws_viewer)) -> list[dict[str, Any]]:
    return [{"call_id": s.call_id, "state": s.pipeline.state, "channel": s.channel}
            for s in manager.sessions.values() if s.workspace_id == ctx.id]  # fmt: skip


# ----------------------------------------------------------------- public links
async def _public_agent(db: AsyncSession, token: str) -> tuple[Agent, Workspace]:
    if not get_settings().public_calls_enabled or not (16 <= len(token) <= 100):
        raise not_found("Link")
    agent = (await db.execute(select(Agent).where(Agent.public_token_hash == token_hash(token),
             Agent.public_link_enabled.is_(True), Agent.deleted_at.is_(None)))).scalar_one_or_none()  # fmt: skip
    if agent is None or not agent.is_active:
        raise not_found("Link")
    ws = await db.get(Workspace, agent.workspace_id)
    if ws is None or ws.deleted_at is not None:
        raise not_found("Link")
    return agent, ws


@router.get("/api/public/talk/{token}")
async def public_info(token: str, request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    ok, _ = limiter.hit(f"public-info:{client_ip(request)}", 60, 60)
    if not ok:
        raise APIError(429, "rate_limited", "Too many requests")
    agent, ws = await _public_agent(db, token)
    return {"agent_name": agent.name, "business_name": ws.name, "brand_color": ws.brand_color,
            "description": agent.description[:300], "ai_notice": f"You will be speaking with an AI assistant for {ws.name}.",
            "recording": bool((ws.compliance or {}).get("recording_enabled"))}  # fmt: skip


@router.post("/api/public/talk/{token}/sessions", status_code=201)
async def public_session(token: str, body: PublicOfferIn, request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    ip = client_ip(request) or "unknown"
    ok, retry = limiter.hit(f"public-call:{ip}", 5, 600)
    if not ok:
        raise APIError(429, "rate_limited", f"Too many calls from your network. Try again in {int(retry)}s.")
    agent, ws = await _public_agent(db, token)
    call_id, answer = await manager.start(ws=ws, agent=agent, sdp=body.sdp, sdp_type=body.type, user_id=None,
                                          channel="public_link")  # fmt: skip
    audit(db, "voice.public_session_started", workspace_id=ws.id, entity_type="call", entity_id=call_id, request=request)
    await db.commit()
    return {"call_id": call_id, "sdp": answer.sdp, "type": answer.type}


@router.post("/api/public/talk/{token}/sessions/{call_id}/end")
async def public_end(token: str, call_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, bool]:
    _agent, ws = await _public_agent(db, token)
    live = manager.get(call_id, ws.id)
    if live.channel != "public_link":
        raise not_found("Session")
    await manager.end(call_id, ws.id)
    return {"ok": True}
