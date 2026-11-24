"""Telephony: provider status, simulated inbound calls."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from engine.providers.telephony.simulator import PERSONA_SCRIPTS, SimulationScenario
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.hub import get_hub
from app.auth.deps import WorkspaceContext, ws_editor, ws_viewer
from app.calls.service import create_call, run_simulated_call
from app.compliance.policy import policy_for
from app.contacts.phone import normalize_phone
from app.core.audit import audit
from app.core.config import get_settings
from app.core.errors import bad_request, not_found
from app.database.models import Agent, Contact
from app.database.session import get_db

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/workspaces/{workspace_id}/telephony", tags=["telephony"])
_bg: set[asyncio.Task[Any]] = set()


class InboundIn(BaseModel):
    agent_id: str
    from_number: str = Field(default="+919000000001", max_length=40)
    persona: str = "questions"
    callee_mode: str = Field(default="scripted", pattern="^(scripted|llm)$")


@router.get("/status")
async def status(ctx: WorkspaceContext = Depends(ws_viewer)) -> dict[str, Any]:
    hub = get_hub()
    settings = get_settings()
    row = hub.configs.get("telephony")
    policy = policy_for(ctx.workspace.compliance)
    return {
        "real_telephony_enabled": settings.real_telephony_enabled,
        "provider": row.provider if row else "simulator",
        "provider_enabled": bool(row and row.enabled),
        "pstn_prerequisites_missing": policy.pstn_prerequisites_missing(),
        "modes": {"SIMULATION": "available", "WEBRTC": "available",
                  "PSTN": "available" if settings.real_telephony_enabled and row and row.enabled
                  and not policy.pstn_prerequisites_missing() else "disabled"},
        "health": (await hub.health())["telephony"],
    }  # fmt: skip


@router.post("/simulate-inbound", status_code=202)
async def simulate_inbound(body: InboundIn, request: Request, ctx: WorkspaceContext = Depends(ws_editor),
                           db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    if body.persona not in PERSONA_SCRIPTS:
        raise bad_request(f"persona must be one of {', '.join(PERSONA_SCRIPTS)}")
    agent = await db.get(Agent, body.agent_id)
    if agent is None or agent.workspace_id != ctx.id or agent.deleted_at is not None:
        raise not_found("Agent")
    phone = normalize_phone(body.from_number, ctx.workspace.default_country)
    from sqlalchemy import select

    contact = None
    if phone.e164:
        contact = (await db.execute(select(Contact).where(Contact.workspace_id == ctx.id, Contact.phone_e164 == phone.e164,
                   Contact.deleted_at.is_(None)).limit(1))).scalar_one_or_none()  # fmt: skip
    call = await create_call(db, ws_id=ctx.id, agent=agent, mode="SIMULATION", direction="inbound", channel="inbound",
                             contact=contact, provider="simulator", status="ringing", persona=body.persona,
                             from_number=phone.e164 or body.from_number, to_number="")  # fmt: skip
    audit(db, "call.simulated_inbound", user_id=ctx.user.id, workspace_id=ctx.id, entity_type="call", entity_id=call.id,
          request=request)  # fmt: skip
    await db.commit()

    async def runner() -> None:
        try:
            await run_simulated_call(ws_id=ctx.id, agent_id=agent.id, contact_id=contact.id if contact else None,
                                     persona=body.persona, callee_mode=body.callee_mode, direction="inbound",
                                     from_number=phone.e164 or body.from_number, call_id=call.id,
                                     scenario=SimulationScenario(answer_rate=1.0, busy_rate=0, fail_rate=0))  # fmt: skip
        except Exception:
            log.warning("simulated inbound call failed", exc_info=True)

    task = asyncio.create_task(runner())
    _bg.add(task)
    task.add_done_callback(_bg.discard)
    return {"call_id": call.id, "mode": "SIMULATION", "direction": "inbound"}
