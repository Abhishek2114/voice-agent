"""Outbound campaigns (SIMULATION by default; PSTN only when explicitly enabled and authorised)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from engine.providers.telephony.simulator import PERSONA_SCRIPTS
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import String, cast, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import WorkspaceContext, ws_editor, ws_viewer
from app.compliance.policy import policy_for
from app.compliance.service import check_eligibility, effective_window, window_status
from app.core.audit import audit
from app.core.config import get_settings
from app.core.errors import APIError, bad_request, conflict, not_found
from app.database.base import new_id, utcnow
from app.database.models import Agent, Call, Campaign, CampaignContact, Contact, ScheduledJob
from app.database.session import get_db

router = APIRouter(prefix="/api/workspaces/{workspace_id}/campaigns", tags=["campaigns"])

DEFAULT_PERSONAS: dict[str, float] = {"interested": 3, "questions": 2, "callback": 2, "not_interested": 2, "wrong_number": 1, "opt_out": 1}


class SimulationCfg(BaseModel):
    answer_rate: float = Field(default=0.75, ge=0, le=1)
    busy_rate: float = Field(default=0.05, ge=0, le=1)
    fail_rate: float = Field(default=0.05, ge=0, le=1)
    callee_mode: Literal["scripted", "llm"] = "scripted"
    persona_weights: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_PERSONAS))
    ignore_calling_window: bool = False

    @field_validator("persona_weights")
    @classmethod
    def _p(cls, v: dict[str, float]) -> dict[str, float]:
        bad = set(v) - set(PERSONA_SCRIPTS)
        if bad:
            raise ValueError(f"unknown personas: {', '.join(sorted(bad))}")
        if not any(w > 0 for w in v.values()):
            raise ValueError("at least one persona weight must be positive")
        return v

    @model_validator(mode="after")
    def _rates(self) -> SimulationCfg:
        if self.answer_rate + self.busy_rate + self.fail_rate > 1.0001:
            raise ValueError("answer + busy + fail rates must not exceed 1")
        return self


class CampaignIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    agent_id: str
    objective: str = Field(default="", max_length=2000)
    purpose: Literal["promotional", "service", "transactional", "inquiry_followup"] = "promotional"
    mode: Literal["SIMULATION", "PSTN"] = "SIMULATION"
    schedule_start_at: datetime | None = None
    schedule_end_at: datetime | None = None
    timezone: str = "Asia/Kolkata"
    window_start: str = Field(default="10:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    window_end: str = Field(default="19:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    calling_days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4, 5])
    max_attempts: int = Field(default=2, ge=1, le=10)
    retry_delay_minutes: int = Field(default=60, ge=1, le=10_080)
    max_concurrent_calls: int = Field(default=1, ge=1, le=20)
    simulation: SimulationCfg = Field(default_factory=SimulationCfg)
    contact_ids: list[str] = Field(default_factory=list, max_length=50_000)
    contact_filter: dict[str, str] | None = None  # {"tag": "...", "status": "..."}

    @model_validator(mode="after")
    def _check(self) -> CampaignIn:
        if self.window_start >= self.window_end:
            raise ValueError("window_start must be before window_end")
        if self.schedule_start_at and self.schedule_end_at and self.schedule_end_at <= self.schedule_start_at:
            raise ValueError("schedule end must be after start")
        if not self.calling_days or any(d < 0 or d > 6 for d in self.calling_days):
            raise ValueError("calling_days must be weekday numbers 0–6")
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(self.timezone)
        except Exception as exc:
            raise ValueError("unknown timezone") from exc
        return self


class ContactsIn(BaseModel):
    contact_ids: list[str] = Field(default_factory=list, max_length=50_000)
    contact_filter: dict[str, str] | None = None


def campaign_out(c: Campaign, progress: dict[str, int] | None = None, agent_name: str | None = None) -> dict[str, Any]:
    return {
        "id": c.id, "name": c.name, "description": c.description, "agent_id": c.agent_id, "agent_name": agent_name,
        "objective": c.objective, "purpose": c.purpose, "mode": c.mode, "status": c.status,
        "status_reason": c.status_reason, "schedule_start_at": c.schedule_start_at, "schedule_end_at": c.schedule_end_at,
        "timezone": c.timezone, "window_start": c.window_start, "window_end": c.window_end,
        "calling_days": c.calling_days, "max_attempts": c.max_attempts, "retry_delay_minutes": c.retry_delay_minutes,
        "max_concurrent_calls": c.max_concurrent_calls, "simulation": c.simulation, "started_at": c.started_at,
        "completed_at": c.completed_at, "created_at": c.created_at, "progress": progress or {},
    }  # fmt: skip


async def _get(db: AsyncSession, ws_id: str, cid: str) -> Campaign:
    c = await db.get(Campaign, cid)
    if c is None or c.workspace_id != ws_id:
        raise not_found("Campaign")
    return c


async def _progress(db: AsyncSession, cid: str) -> dict[str, int]:
    rows = (await db.execute(select(CampaignContact.status, func.count()).where(CampaignContact.campaign_id == cid)
                             .group_by(CampaignContact.status))).all()  # fmt: skip
    out = {s: n for s, n in rows}
    out["total"] = sum(out.values())
    out["calls"] = (await db.execute(select(func.count(Call.id)).where(Call.campaign_id == cid))).scalar_one()
    return out


async def _resolve_contacts(db: AsyncSession, ws_id: str, ids: list[str], flt: dict[str, str] | None) -> list[str]:
    stmt = select(Contact.id).where(Contact.workspace_id == ws_id, Contact.deleted_at.is_(None))
    if ids:
        stmt = stmt.where(Contact.id.in_(ids))
    elif flt:
        if flt.get("tag"):
            stmt = stmt.where(cast(Contact.tags, String).like(f'%"{flt["tag"].lower()}"%'))
        if flt.get("status"):
            stmt = stmt.where(Contact.status == flt["status"])
    else:
        return []
    return list((await db.execute(stmt)).scalars())


async def _add_contacts(db: AsyncSession, campaign: Campaign, contact_ids: list[str]) -> int:
    if not contact_ids:
        return 0
    now = utcnow()
    before = (await db.execute(select(func.count()).where(CampaignContact.campaign_id == campaign.id))).scalar_one()
    for i in range(0, len(contact_ids), 500):
        batch = [{"id": new_id(), "campaign_id": campaign.id, "contact_id": cid, "workspace_id": campaign.workspace_id,
                  "status": "pending", "attempts": 0, "skip_reason": "", "created_at": now, "updated_at": now}
                 for cid in contact_ids[i : i + 500]]  # fmt: skip
        await db.execute(sqlite_insert(CampaignContact).values(batch).on_conflict_do_nothing())
    after = (await db.execute(select(func.count()).where(CampaignContact.campaign_id == campaign.id))).scalar_one()
    return int(after - before)


def _check_mode(mode: str) -> None:
    if mode == "PSTN" and not get_settings().real_telephony_enabled:
        raise APIError(400, "pstn_disabled", "Real (PSTN) calling is disabled on this installation. "
                       "Campaigns run in SIMULATION mode unless real telephony is configured and authorised.")  # fmt: skip


@router.get("")
async def list_campaigns(ctx: WorkspaceContext = Depends(ws_viewer), db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    rows = (await db.execute(select(Campaign).where(Campaign.workspace_id == ctx.id).order_by(Campaign.created_at.desc()))).scalars().all()
    agents = dict((await db.execute(select(Agent.id, Agent.name).where(Agent.workspace_id == ctx.id))).all())
    return [campaign_out(c, await _progress(db, c.id), agents.get(c.agent_id)) for c in rows]


@router.post("", status_code=201)
async def create_campaign(body: CampaignIn, request: Request, ctx: WorkspaceContext = Depends(ws_editor),
                          db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    _check_mode(body.mode)
    agent = await db.get(Agent, body.agent_id)
    if agent is None or agent.workspace_id != ctx.id or agent.deleted_at is not None:
        raise not_found("Agent")
    data = body.model_dump(exclude={"contact_ids", "contact_filter", "simulation"})
    c = Campaign(workspace_id=ctx.id, created_by=ctx.user.id, status="draft", simulation=body.simulation.model_dump(), **data)
    db.add(c)
    await db.flush()
    added = await _add_contacts(db, c, await _resolve_contacts(db, ctx.id, body.contact_ids, body.contact_filter))
    audit(db, "campaign.created", user_id=ctx.user.id, workspace_id=ctx.id, entity_type="campaign", entity_id=c.id,
          details={"mode": c.mode, "contacts": added}, request=request)  # fmt: skip
    await db.commit()
    return campaign_out(c, await _progress(db, c.id), agent.name)


@router.get("/{campaign_id}")
async def get_campaign(campaign_id: str, ctx: WorkspaceContext = Depends(ws_viewer), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    c = await _get(db, ctx.id, campaign_id)
    agent = await db.get(Agent, c.agent_id)
    out = campaign_out(c, await _progress(db, c.id), agent.name if agent else None)
    policy = policy_for(ctx.workspace.compliance)
    start, end, days = effective_window(policy, c)
    inside, nxt = window_status(utcnow(), c.timezone, start, end, days) if days and start < end else (False, None)
    out["window"] = {"start": start, "end": end, "days": days, "open_now": inside, "next_open": nxt,
                     "ignored": bool(c.mode == "SIMULATION" and (c.simulation or {}).get("ignore_calling_window"))}  # fmt: skip
    return out


@router.put("/{campaign_id}")
async def update_campaign(campaign_id: str, body: CampaignIn, request: Request, ctx: WorkspaceContext = Depends(ws_editor),
                          db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    c = await _get(db, ctx.id, campaign_id)
    if c.status not in ("draft", "paused", "scheduled"):
        raise conflict("Only draft, scheduled or paused campaigns can be edited")
    _check_mode(body.mode)
    agent = await db.get(Agent, body.agent_id)
    if agent is None or agent.workspace_id != ctx.id or agent.deleted_at is not None:
        raise not_found("Agent")
    for k, v in body.model_dump(exclude={"contact_ids", "contact_filter", "simulation"}).items():
        setattr(c, k, v)
    c.simulation = body.simulation.model_dump()
    added = await _add_contacts(db, c, await _resolve_contacts(db, ctx.id, body.contact_ids, body.contact_filter))
    audit(db, "campaign.updated", user_id=ctx.user.id, workspace_id=ctx.id, entity_type="campaign", entity_id=c.id,
          details={"contacts_added": added}, request=request)  # fmt: skip
    await db.commit()
    return campaign_out(c, await _progress(db, c.id), agent.name)


@router.post("/{campaign_id}/contacts")
async def add_contacts(campaign_id: str, body: ContactsIn, ctx: WorkspaceContext = Depends(ws_editor),
                       db: AsyncSession = Depends(get_db)) -> dict[str, int]:  # fmt: skip
    c = await _get(db, ctx.id, campaign_id)
    if c.status in ("completed", "cancelled"):
        raise conflict("Campaign is finished")
    added = await _add_contacts(db, c, await _resolve_contacts(db, ctx.id, body.contact_ids, body.contact_filter))
    await db.commit()
    return {"added": added}


@router.get("/{campaign_id}/contacts")
async def campaign_contacts(campaign_id: str, ctx: WorkspaceContext = Depends(ws_viewer), db: AsyncSession = Depends(get_db),
                            status: str | None = None, page: int = 1) -> dict[str, Any]:  # fmt: skip
    c = await _get(db, ctx.id, campaign_id)
    stmt = select(CampaignContact).where(CampaignContact.campaign_id == c.id)
    if status:
        stmt = stmt.where(CampaignContact.status == status)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(CampaignContact.updated_at.desc()).offset((max(page, 1) - 1) * 50).limit(50))).scalars()
    return {"total": total, "items": [{
        "id": r.id, "contact_id": r.contact_id, "name": r.contact.full_name, "phone_e164": r.contact.phone_e164,
        "status": r.status, "attempts": r.attempts, "last_outcome": r.last_outcome, "last_call_id": r.last_call_id,
        "next_attempt_at": r.next_attempt_at, "skip_reason": r.skip_reason} for r in rows]}  # fmt: skip


@router.post("/{campaign_id}/preview")
async def preview(campaign_id: str, ctx: WorkspaceContext = Depends(ws_viewer), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Compliance preview: how many contacts are eligible right now and why others are not."""
    c = await _get(db, ctx.id, campaign_id)
    rows = (await db.execute(select(CampaignContact).where(CampaignContact.campaign_id == c.id).limit(2000))).scalars().all()
    eligible, reasons = 0, {}  # type: ignore[var-annotated]
    for r in rows:
        res = await check_eligibility(db, ctx.workspace, r.contact, purpose=c.purpose, mode=c.mode, campaign=c,
                                      attempts=r.attempts)  # fmt: skip
        if res.allowed:
            eligible += 1
        for ch in res.checks:
            if not ch.passed:
                reasons[ch.name] = reasons.get(ch.name, 0) + 1
    return {"checked": len(rows), "eligible_now": eligible, "blocked_by": reasons}


async def _transition(db: AsyncSession, c: Campaign, ctx: WorkspaceContext, request: Request, action: str) -> dict[str, Any]:
    now = utcnow()
    if action == "start":
        if c.status not in ("draft", "paused", "scheduled"):
            raise conflict(f"Cannot start a {c.status} campaign")
        _check_mode(c.mode)
        agent = await db.get(Agent, c.agent_id)
        if agent is None or agent.deleted_at is not None or not agent.is_active:
            raise bad_request("The campaign's agent is missing or inactive")
        total = (await db.execute(select(func.count()).where(CampaignContact.campaign_id == c.id))).scalar_one()
        if total == 0:
            raise bad_request("Add contacts before starting the campaign")
        start_t, end_t, days = effective_window(policy_for(ctx.workspace.compliance), c)
        if (not days or start_t >= end_t) and not (c.mode == "SIMULATION" and (c.simulation or {}).get("ignore_calling_window")):
            raise APIError(400, "no_calling_window", "The campaign's calling window does not overlap the workspace's "
                           "permitted calling window (Settings → Compliance).")
        if c.mode == "PSTN":
            missing = policy_for(ctx.workspace.compliance).pstn_prerequisites_missing()
            if missing:
                raise APIError(400, "pstn_prerequisites", "; ".join(missing))
        c.status = "scheduled" if c.schedule_start_at and c.schedule_start_at > now else "running"
        c.started_at = c.started_at or now
        c.status_reason = ""
        c.consecutive_failures = 0
    elif action == "pause":
        if c.status not in ("running", "scheduled"):
            raise conflict(f"Cannot pause a {c.status} campaign")
        c.status, c.status_reason = "paused", "Paused by user"
        # queued-but-not-started jobs go back to pending so they are not dialled while paused
        await db.execute(update(ScheduledJob).where(ScheduledJob.campaign_id == c.id, ScheduledJob.status == "pending")
                         .values(status="cancelled", finished_at=now, error="campaign paused"))  # fmt: skip
        await db.execute(update(CampaignContact).where(CampaignContact.campaign_id == c.id, CampaignContact.status == "queued")
                         .values(status="pending"))  # fmt: skip
    elif action == "cancel":
        if c.status in ("completed", "cancelled"):
            raise conflict(f"Campaign already {c.status}")
        c.status, c.status_reason, c.completed_at = "cancelled", "Cancelled by user", now
        await db.execute(update(ScheduledJob).where(ScheduledJob.campaign_id == c.id, ScheduledJob.status == "pending")
                         .values(status="cancelled", finished_at=now, error="campaign cancelled"))  # fmt: skip
        await db.execute(update(CampaignContact).where(CampaignContact.campaign_id == c.id,
                         CampaignContact.status.in_(("pending", "queued"))).values(status="cancelled"))  # fmt: skip
    audit(db, f"campaign.{action}", user_id=ctx.user.id, workspace_id=ctx.id, entity_type="campaign", entity_id=c.id,
          request=request)  # fmt: skip
    await db.commit()
    return campaign_out(c, await _progress(db, c.id))


@router.post("/{campaign_id}/start")
async def start(campaign_id: str, request: Request, ctx: WorkspaceContext = Depends(ws_editor),
                db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    return await _transition(db, await _get(db, ctx.id, campaign_id), ctx, request, "start")


@router.post("/{campaign_id}/pause")
async def pause(campaign_id: str, request: Request, ctx: WorkspaceContext = Depends(ws_editor),
                db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    return await _transition(db, await _get(db, ctx.id, campaign_id), ctx, request, "pause")


@router.post("/{campaign_id}/resume")
async def resume(campaign_id: str, request: Request, ctx: WorkspaceContext = Depends(ws_editor),
                 db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    return await _transition(db, await _get(db, ctx.id, campaign_id), ctx, request, "start")


@router.post("/{campaign_id}/cancel")
async def cancel(campaign_id: str, request: Request, ctx: WorkspaceContext = Depends(ws_editor),
                 db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    return await _transition(db, await _get(db, ctx.id, campaign_id), ctx, request, "cancel")


@router.delete("/{campaign_id}")
async def delete_campaign(campaign_id: str, request: Request, ctx: WorkspaceContext = Depends(ws_editor),
                          db: AsyncSession = Depends(get_db)) -> dict[str, bool]:  # fmt: skip
    c = await _get(db, ctx.id, campaign_id)
    if c.status in ("running", "scheduled"):
        raise conflict("Pause or cancel the campaign before deleting it")
    await db.delete(c)
    audit(db, "campaign.deleted", user_id=ctx.user.id, workspace_id=ctx.id, entity_type="campaign",
          entity_id=campaign_id, request=request)  # fmt: skip
    await db.commit()
    return {"ok": True}


@router.get("/{campaign_id}/report")
async def report(campaign_id: str, ctx: WorkspaceContext = Depends(ws_viewer), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    c = await _get(db, ctx.id, campaign_id)
    outcomes = dict((await db.execute(select(Call.outcome, func.count()).where(Call.campaign_id == c.id)
                                      .group_by(Call.outcome))).all())  # fmt: skip
    statuses = dict((await db.execute(select(Call.status, func.count()).where(Call.campaign_id == c.id)
                                      .group_by(Call.status))).all())  # fmt: skip
    agg = (await db.execute(select(func.count(Call.id), func.avg(Call.duration_seconds), func.sum(Call.duration_seconds))
                            .where(Call.campaign_id == c.id, Call.answered_at.is_not(None)))).one()  # fmt: skip
    total_calls = sum(statuses.values())
    positive = sum(outcomes.get(k, 0) for k in ("interested", "callback_requested", "appointment_requested"))
    return {
        "campaign": campaign_out(c, await _progress(db, c.id)),
        "mode": c.mode,
        "calls": total_calls,
        "connected": agg[0] or 0,
        "connect_rate": round((agg[0] or 0) / total_calls, 3) if total_calls else 0,
        "positive_outcomes": positive,
        "conversion_rate": round(positive / (agg[0] or 1), 3) if agg[0] else 0,
        "avg_duration_seconds": round(agg[1] or 0, 1),
        "total_duration_seconds": round(agg[2] or 0, 1),
        "outcomes": {k or "unclassified": v for k, v in outcomes.items()},
        "statuses": statuses,
    }
