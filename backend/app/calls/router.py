from __future__ import annotations

import asyncio
import csv
import io
import logging
from datetime import datetime
from typing import Any, Literal

from engine.providers.telephony.simulator import PERSONA_SCRIPTS
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import WorkspaceContext, ws_admin, ws_editor, ws_viewer
from app.calls.service import run_simulated_call
from app.contacts.csv_safety import escape_cell
from app.core.audit import audit
from app.core.config import get_settings
from app.core.errors import bad_request, not_found
from app.database.models import (
    Agent,
    Call,
    CallbackRequest,
    Campaign,
    Contact,
    Conversation,
    Message,
    Recording,
    Transcript,
)
from app.database.session import get_db

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/workspaces/{workspace_id}", tags=["calls"])
_bg: set[asyncio.Task[Any]] = set()


class SimulateIn(BaseModel):
    agent_id: str
    contact_id: str | None = None
    persona: str = "questions"
    callee_mode: Literal["scripted", "llm"] = "scripted"
    direction: Literal["outbound", "inbound"] = "outbound"
    from_number: str | None = Field(default=None, max_length=20)


class CallbackPatch(BaseModel):
    status: Literal["open", "scheduled", "done", "cancelled"]
    notes: str | None = Field(default=None, max_length=2000)


def call_out(c: Call, names: dict[str, str] | None = None) -> dict[str, Any]:
    names = names or {}
    return {
        "id": c.id, "mode": c.mode, "direction": c.direction, "channel": c.channel, "status": c.status,
        "agent_id": c.agent_id, "agent_name": names.get(f"a:{c.agent_id}"), "contact_id": c.contact_id,
        "contact_name": names.get(f"c:{c.contact_id}"), "campaign_id": c.campaign_id,
        "campaign_name": names.get(f"k:{c.campaign_id}"), "to_number": c.to_number, "from_number": c.from_number,
        "started_at": c.started_at, "answered_at": c.answered_at, "ended_at": c.ended_at,
        "duration_seconds": c.duration_seconds, "outcome": c.outcome, "summary": c.summary, "sentiment": c.sentiment,
        "end_reason": c.end_reason, "error_code": c.error_code, "error_detail": c.error_detail,
        "analysis_error": c.analysis_error, "llm_model": c.llm_model, "simulation_persona": c.simulation_persona,
        "is_simulated": c.mode == "SIMULATION",
    }  # fmt: skip


async def _names(db: AsyncSession, calls: list[Call]) -> dict[str, str]:
    out: dict[str, str] = {}
    agent_ids = {c.agent_id for c in calls if c.agent_id}
    contact_ids = {c.contact_id for c in calls if c.contact_id}
    camp_ids = {c.campaign_id for c in calls if c.campaign_id}
    if agent_ids:
        for i, n in (await db.execute(select(Agent.id, Agent.name).where(Agent.id.in_(agent_ids)))).all():
            out[f"a:{i}"] = n
    if contact_ids:
        for i, f, last in (await db.execute(select(Contact.id, Contact.first_name, Contact.last_name)
                                            .where(Contact.id.in_(contact_ids)))).all():  # fmt: skip
            out[f"c:{i}"] = f"{f} {last}".strip() or "Unnamed"
    if camp_ids:
        for i, n in (await db.execute(select(Campaign.id, Campaign.name).where(Campaign.id.in_(camp_ids)))).all():
            out[f"k:{i}"] = n
    return out


def _filter(ws_id: str, mode: str | None, status: str | None, outcome: str | None, agent_id: str | None,
            campaign_id: str | None, contact_id: str | None, q: str | None, date_from: datetime | None,
            date_to: datetime | None) -> Select[Call]:  # fmt: skip
    stmt = select(Call).where(Call.workspace_id == ws_id)
    for col, val in ((Call.mode, mode), (Call.status, status), (Call.outcome, outcome), (Call.agent_id, agent_id),
                     (Call.campaign_id, campaign_id), (Call.contact_id, contact_id)):  # fmt: skip
        if val:
            stmt = stmt.where(col == val)
    if date_from:
        stmt = stmt.where(Call.started_at >= date_from)
    if date_to:
        stmt = stmt.where(Call.started_at <= date_to)
    if q:
        like = f"%{q.strip()[:100]}%"
        stmt = stmt.outerjoin(Transcript, Transcript.call_id == Call.id).where(
            or_(Call.summary.like(like), Transcript.content.like(like), Call.to_number.like(like)))
    return stmt


@router.get("/calls")
async def list_calls(
    ctx: WorkspaceContext = Depends(ws_viewer), db: AsyncSession = Depends(get_db),
    mode: str | None = Query(None, pattern="^(WEBRTC|SIMULATION|PSTN)$"), status: str | None = None,
    outcome: str | None = None, agent_id: str | None = None, campaign_id: str | None = None,
    contact_id: str | None = None, q: str | None = Query(None, max_length=100),
    date_from: datetime | None = None, date_to: datetime | None = None,
    page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=200),
) -> dict[str, Any]:  # fmt: skip
    stmt = _filter(ctx.id, mode, status, outcome, agent_id, campaign_id, contact_id, q, date_from, date_to)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(Call.created_at.desc()).offset((page - 1) * page_size).limit(page_size))).scalars().all()
    names = await _names(db, list(rows))
    return {"items": [call_out(c, names) for c in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/calls/export")
async def export_calls(request: Request, ctx: WorkspaceContext = Depends(ws_admin), db: AsyncSession = Depends(get_db),
                       mode: str | None = None, outcome: str | None = None, include_transcripts: bool = False) -> Response:  # fmt: skip
    rows = (await db.execute(_filter(ctx.id, mode, None, outcome, None, None, None, None, None, None)
                             .order_by(Call.created_at))).scalars().all()  # fmt: skip
    names = await _names(db, list(rows))
    transcripts: dict[str, str] = {}
    if include_transcripts and rows:
        transcripts = dict((await db.execute(select(Transcript.call_id, Transcript.content).where(
            Transcript.workspace_id == ctx.id, Transcript.call_id.in_([c.id for c in rows])))).all())  # fmt: skip
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = ["id", "mode", "direction", "status", "agent_name", "contact_name", "campaign_name", "to_number",
            "started_at", "duration_seconds", "outcome", "sentiment", "summary", "end_reason", "error_code"]  # fmt: skip
    w.writerow(cols + (["transcript"] if include_transcripts else []))
    for c in rows:
        d = call_out(c, names)
        w.writerow([escape_cell(d[k]) for k in cols] + ([escape_cell(transcripts.get(c.id, ""))] if include_transcripts else []))
    audit(db, "calls.exported", user_id=ctx.user.id, workspace_id=ctx.id,
          details={"count": len(rows), "transcripts": include_transcripts}, request=request)  # fmt: skip
    await db.commit()
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{ctx.workspace.slug}-calls.csv"'})  # fmt: skip


@router.get("/calls/{call_id}")
async def call_detail(call_id: str, ctx: WorkspaceContext = Depends(ws_viewer), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    c = await db.get(Call, call_id)
    if c is None or c.workspace_id != ctx.id:
        raise not_found("Call")
    conv = (await db.execute(select(Conversation).where(Conversation.call_id == c.id))).scalar_one_or_none()
    messages = []
    if conv:
        messages = [{"seq": m.seq, "role": m.role, "content": m.content, "interrupted": m.interrupted,
                     "metrics": m.metrics, "created_at": m.created_at}
                    for m in (await db.execute(select(Message).where(Message.conversation_id == conv.id,
                              Message.workspace_id == ctx.id).order_by(Message.seq))).scalars()]  # fmt: skip
    rec = (await db.execute(select(Recording).where(Recording.call_id == c.id, Recording.deleted_at.is_(None)))).scalar_one_or_none()
    callbacks = (await db.execute(select(CallbackRequest).where(CallbackRequest.call_id == c.id,
                 CallbackRequest.workspace_id == ctx.id))).scalars()  # fmt: skip
    names = await _names(db, [c])
    return {
        **call_out(c, names),
        "metrics": c.metrics,
        "messages": messages,
        "tool_log": conv.tool_log if conv else [],
        "effects": conv.effects if conv else [],
        "recording": {"duration_seconds": rec.duration_seconds, "size_bytes": rec.size_bytes} if rec else None,
        "callbacks": [{"id": x.id, "kind": x.kind, "preferred_time": x.preferred_time, "status": x.status,
                       "notes": x.notes} for x in callbacks],
    }  # fmt: skip


@router.get("/calls/{call_id}/recording")
async def recording(call_id: str, request: Request, ctx: WorkspaceContext = Depends(ws_editor),
                    db: AsyncSession = Depends(get_db)) -> Response:  # fmt: skip
    rec = (await db.execute(select(Recording).where(Recording.call_id == call_id, Recording.workspace_id == ctx.id,
                                                    Recording.deleted_at.is_(None)))).scalar_one_or_none()  # fmt: skip
    settings = get_settings()
    if rec is None:
        raise not_found("Recording")
    path = (settings.data_dir / rec.relative_path).resolve()
    if not path.is_relative_to((settings.recordings_dir / ctx.id).resolve()) or not path.is_file():
        raise not_found("Recording")
    audit(db, "recording.accessed", user_id=ctx.user.id, workspace_id=ctx.id, entity_type="call", entity_id=call_id,
          request=request)  # fmt: skip
    await db.commit()
    return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "no-store"})


@router.delete("/calls/{call_id}")
async def delete_call(call_id: str, request: Request, ctx: WorkspaceContext = Depends(ws_admin),
                      db: AsyncSession = Depends(get_db)) -> dict[str, bool]:  # fmt: skip
    c = await db.get(Call, call_id)
    if c is None or c.workspace_id != ctx.id:
        raise not_found("Call")
    rec = (await db.execute(select(Recording).where(Recording.call_id == c.id))).scalar_one_or_none()
    if rec:
        settings = get_settings()
        path = (settings.data_dir / rec.relative_path).resolve()
        if path.is_relative_to(settings.recordings_dir.resolve()):
            path.unlink(missing_ok=True)
    await db.delete(c)
    audit(db, "call.deleted", user_id=ctx.user.id, workspace_id=ctx.id, entity_type="call", entity_id=call_id,
          request=request)  # fmt: skip
    await db.commit()
    return {"ok": True}


@router.post("/calls/simulate", status_code=202)
async def simulate(body: SimulateIn, request: Request, ctx: WorkspaceContext = Depends(ws_editor),
                   db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    """Start a SIMULATED call (no real phone call). Runs in the background."""
    if body.persona not in PERSONA_SCRIPTS:
        raise bad_request(f"persona must be one of {', '.join(PERSONA_SCRIPTS)}")
    agent = await db.get(Agent, body.agent_id)
    if agent is None or agent.workspace_id != ctx.id or agent.deleted_at is not None:
        raise not_found("Agent")
    contact = None
    if body.contact_id:
        contact = await db.get(Contact, body.contact_id)
        if contact is None or contact.workspace_id != ctx.id:
            raise not_found("Contact")
    from app.calls.service import create_call

    call = await create_call(db, ws_id=ctx.id, agent=agent, mode="SIMULATION", direction=body.direction,
                             channel="simulation", contact=contact, provider="simulator", status="dialing",
                             persona=body.persona, from_number=body.from_number)  # fmt: skip
    audit(db, "call.simulation_started", user_id=ctx.user.id, workspace_id=ctx.id, entity_type="call",
          entity_id=call.id, details={"persona": body.persona}, request=request)  # fmt: skip
    await db.commit()

    async def runner() -> None:
        try:
            from engine.providers.telephony.simulator import SimulationScenario

            await run_simulated_call(ws_id=ctx.id, agent_id=agent.id, contact_id=contact.id if contact else None,
                                     persona=body.persona, callee_mode=body.callee_mode, direction=body.direction,
                                     from_number=body.from_number, call_id=call.id,
                                     scenario=SimulationScenario(answer_rate=1.0, busy_rate=0, fail_rate=0))  # fmt: skip
        except Exception:
            log.warning("simulated call %s failed", call.id, exc_info=True)

    task = asyncio.create_task(runner())
    _bg.add(task)
    task.add_done_callback(_bg.discard)
    return {"call_id": call.id, "mode": "SIMULATION"}


@router.get("/callbacks")
async def callbacks(ctx: WorkspaceContext = Depends(ws_viewer), db: AsyncSession = Depends(get_db),
                    status: str | None = None, include_simulated: bool = False,
                    page: int = Query(1, ge=1)) -> dict[str, Any]:  # fmt: skip
    stmt = select(CallbackRequest).where(CallbackRequest.workspace_id == ctx.id)
    if status:
        stmt = stmt.where(CallbackRequest.status == status)
    if not include_simulated:
        stmt = stmt.where(CallbackRequest.is_simulated.is_(False))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(CallbackRequest.created_at.desc()).offset((page - 1) * 50).limit(50))).scalars().all()
    contacts = {}
    ids = {r.contact_id for r in rows if r.contact_id}
    if ids:
        contacts = {c.id: c for c in (await db.execute(select(Contact).where(Contact.id.in_(ids)))).scalars()}
    return {"total": total, "items": [{
        "id": r.id, "kind": r.kind, "preferred_time": r.preferred_time, "notes": r.notes, "status": r.status,
        "call_id": r.call_id, "contact_id": r.contact_id, "is_simulated": r.is_simulated, "created_at": r.created_at,
        "contact_name": contacts[r.contact_id].full_name if r.contact_id in contacts else None,
        "contact_phone": contacts[r.contact_id].phone_e164 if r.contact_id in contacts else None,
    } for r in rows]}  # fmt: skip


@router.patch("/callbacks/{callback_id}")
async def update_callback(callback_id: str, body: CallbackPatch, request: Request, ctx: WorkspaceContext = Depends(ws_editor),
                          db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    cb = await db.get(CallbackRequest, callback_id)
    if cb is None or cb.workspace_id != ctx.id:
        raise not_found("Callback request")
    cb.status = body.status
    if body.notes is not None:
        cb.notes = body.notes
    audit(db, "callback.updated", user_id=ctx.user.id, workspace_id=ctx.id, entity_type="callback", entity_id=cb.id,
          details={"status": body.status}, request=request)  # fmt: skip
    await db.commit()
    return {"id": cb.id, "status": cb.status}
