"""Analytics computed from stored records only (no synthetic numbers)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audio.sessions import manager
from app.auth.deps import WorkspaceContext, ws_viewer
from app.database.base import utcnow
from app.database.models import Agent, Call, CallbackRequest, Campaign, Contact
from app.database.session import get_db

router = APIRouter(prefix="/api/workspaces/{workspace_id}/analytics", tags=["analytics"])

POSITIVE = ("interested", "callback_requested", "appointment_requested")
DURATION_BUCKETS = [(0, 30, "<30s"), (30, 60, "30–60s"), (60, 120, "1–2m"), (120, 300, "2–5m"), (300, 10**9, "5m+")]


def _scope(ws_id: str, date_from: datetime | None, date_to: datetime | None, mode: str | None, agent_id: str | None,
           campaign_id: str | None) -> list[Any]:  # fmt: skip
    conds: list[Any] = [Call.workspace_id == ws_id]
    if date_from:
        conds.append(Call.started_at >= date_from)
    if date_to:
        conds.append(Call.started_at <= date_to)
    if mode:
        conds.append(Call.mode == mode)
    if agent_id:
        conds.append(Call.agent_id == agent_id)
    if campaign_id:
        conds.append(Call.campaign_id == campaign_id)
    return conds


@router.get("/overview")
async def overview(ctx: WorkspaceContext = Depends(ws_viewer), db: AsyncSession = Depends(get_db),
                   mode: str | None = Query(None, pattern="^(WEBRTC|SIMULATION|PSTN)$")) -> dict[str, Any]:  # fmt: skip
    conds = _scope(ctx.id, None, None, mode, None, None)
    row = (await db.execute(select(
        func.count(Call.id),
        func.sum(case((Call.status == "completed", 1), else_=0)),
        func.sum(case((Call.status == "failed", 1), else_=0)),
        func.avg(Call.duration_seconds),
        func.sum(Call.duration_seconds),
    ).where(*conds))).one()  # fmt: skip
    by_mode = dict((await db.execute(select(Call.mode, func.count()).where(Call.workspace_id == ctx.id)
                                     .group_by(Call.mode))).all())  # fmt: skip
    agents_active = (await db.execute(select(func.count(Agent.id)).where(Agent.workspace_id == ctx.id,
                     Agent.is_active.is_(True), Agent.deleted_at.is_(None)))).scalar_one()  # fmt: skip
    agents_total = (await db.execute(select(func.count(Agent.id)).where(Agent.workspace_id == ctx.id,
                    Agent.deleted_at.is_(None)))).scalar_one()  # fmt: skip
    camp = dict((await db.execute(select(Campaign.status, func.count()).where(Campaign.workspace_id == ctx.id)
                                  .group_by(Campaign.status))).all())  # fmt: skip
    contacts = (await db.execute(select(func.count(Contact.id)).where(Contact.workspace_id == ctx.id,
                Contact.deleted_at.is_(None)))).scalar_one()  # fmt: skip
    leads = (await db.execute(select(func.count(Contact.id)).where(Contact.workspace_id == ctx.id,
             Contact.deleted_at.is_(None), Contact.status.in_(("lead", "interested", "callback", "converted"))))).scalar_one()  # fmt: skip
    open_callbacks = (await db.execute(select(func.count(CallbackRequest.id)).where(
        CallbackRequest.workspace_id == ctx.id, CallbackRequest.status == "open",
        CallbackRequest.is_simulated.is_(False)))).scalar_one()  # fmt: skip
    recent = (await db.execute(select(Call).where(*conds).order_by(Call.created_at.desc()).limit(8))).scalars().all()
    names = dict((await db.execute(select(Agent.id, Agent.name).where(Agent.workspace_id == ctx.id))).all())
    return {
        "total_calls": row[0] or 0,
        "successful_calls": int(row[1] or 0),
        "failed_calls": int(row[2] or 0),
        "avg_duration_seconds": round(float(row[3] or 0), 1),
        "total_duration_seconds": round(float(row[4] or 0), 1),
        "calls_by_mode": by_mode,
        "agents_active": agents_active,
        "agents_total": agents_total,
        "campaigns": camp,
        "contacts": contacts,
        "leads": leads,
        "open_callbacks": open_callbacks,
        "live_sessions": sum(1 for s in manager.sessions.values() if s.workspace_id == ctx.id),
        "recent_calls": [{"id": c.id, "mode": c.mode, "status": c.status, "outcome": c.outcome,
                          "agent_name": names.get(c.agent_id) if c.agent_id else None, "started_at": c.started_at,
                          "duration_seconds": c.duration_seconds, "channel": c.channel} for c in recent],
    }  # fmt: skip


@router.get("/report")
async def report(
    ctx: WorkspaceContext = Depends(ws_viewer), db: AsyncSession = Depends(get_db),
    days: int = Query(30, ge=1, le=365), date_from: datetime | None = None, date_to: datetime | None = None,
    mode: str | None = Query(None, pattern="^(WEBRTC|SIMULATION|PSTN)$"), agent_id: str | None = None,
    campaign_id: str | None = None,
) -> dict[str, Any]:  # fmt: skip
    end = date_to or utcnow()
    start = date_from or (end - timedelta(days=days))
    conds = _scope(ctx.id, start, end, mode, agent_id, campaign_id)
    day = func.date(Call.started_at)
    series = (await db.execute(select(day, func.count(Call.id),
              func.sum(case((Call.status == "completed", 1), else_=0)),
              func.sum(case((Call.status == "failed", 1), else_=0)),
              func.sum(case((Call.outcome.in_(POSITIVE), 1), else_=0)))
              .where(*conds).group_by(day).order_by(day))).all()  # fmt: skip
    outcomes = (await db.execute(select(Call.outcome, func.count()).where(*conds).group_by(Call.outcome))).all()
    durations = (await db.execute(select(Call.duration_seconds).where(*conds, Call.duration_seconds.is_not(None)))).scalars().all()
    buckets = {label: 0 for *_r, label in DURATION_BUCKETS}
    for d in durations:
        if d is None:
            continue
        for lo, hi, label in DURATION_BUCKETS:
            if lo <= d < hi:
                buckets[label] += 1
                break
    agents = (await db.execute(select(Agent.id, Agent.name, func.count(Call.id),
              func.sum(case((Call.outcome.in_(POSITIVE), 1), else_=0)), func.avg(Call.duration_seconds),
              func.sum(case((Call.status == "failed", 1), else_=0)))
              .join(Call, Call.agent_id == Agent.id).where(*conds).group_by(Agent.id, Agent.name))).all()  # fmt: skip
    campaigns = (await db.execute(select(Campaign.id, Campaign.name, Campaign.mode, func.count(Call.id),
                 func.sum(case((Call.answered_at.is_not(None), 1), else_=0)),
                 func.sum(case((Call.outcome.in_(POSITIVE), 1), else_=0)))
                 .join(Call, Call.campaign_id == Campaign.id).where(*conds).group_by(Campaign.id))).all()  # fmt: skip
    callbacks = (await db.execute(select(func.count(CallbackRequest.id)).join(Call, Call.id == CallbackRequest.call_id)
                                  .where(*conds))).scalar_one()  # fmt: skip
    total = sum(r[1] for r in series)
    positive = sum(int(r[4] or 0) for r in series)
    completed = sum(int(r[2] or 0) for r in series)
    leads = (await db.execute(select(func.count(Contact.id)).where(Contact.workspace_id == ctx.id,
             Contact.source.in_(("conversation", "public_link")), Contact.created_at >= start,
             Contact.created_at <= end))).scalar_one()  # fmt: skip
    return {
        "range": {"from": start, "to": end},
        "totals": {"calls": total, "completed": completed, "failed": sum(int(r[3] or 0) for r in series),
                   "positive": positive, "success_rate": round(completed / total, 3) if total else 0,
                   "conversion_rate": round(positive / completed, 3) if completed else 0,
                   "callback_requests": callbacks, "leads_captured": leads},
        "calls_over_time": [{"date": str(r[0]), "calls": r[1], "completed": int(r[2] or 0), "failed": int(r[3] or 0),
                             "positive": int(r[4] or 0)} for r in series],
        "outcomes": [{"outcome": o or "unclassified", "count": n} for o, n in outcomes],
        "duration_distribution": [{"bucket": k, "count": v} for k, v in buckets.items()],
        "agents": [{"id": a[0], "name": a[1], "calls": a[2], "positive": int(a[3] or 0),
                    "avg_duration_seconds": round(float(a[4] or 0), 1), "failed": int(a[5] or 0),
                    "conversion_rate": round(int(a[3] or 0) / a[2], 3) if a[2] else 0} for a in agents],
        "campaigns": [{"id": c[0], "name": c[1], "mode": c[2], "calls": c[3], "connected": int(c[4] or 0),
                       "positive": int(c[5] or 0)} for c in campaigns],
    }  # fmt: skip
