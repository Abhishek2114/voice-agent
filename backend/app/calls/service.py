"""Call lifecycle persistence, DB-backed agent tools, post-call analysis and simulated calls."""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

from engine.audio.recorder import CallRecorder
from engine.conversation.outcome import CallAnalysis, analyze_call, render_transcript
from engine.conversation.session import ConversationSession
from engine.conversation.tools import KnowledgeSnippet, ToolContext, default_registry
from engine.providers.base import ProviderError
from engine.providers.telephony.base import CallStatus
from engine.providers.telephony.simulator import (
    LLMCallee,
    ScriptedCallee,
    SimulatedTelephonyProvider,
    SimulationScenario,
)
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.runtime import session_config
from app.ai.hub import ProviderHub, get_hub
from app.compliance.policy import policy_for
from app.compliance.service import record_opt_out
from app.contacts.phone import normalize_phone
from app.core.audit import audit, system_event
from app.core.config import get_settings
from app.database.base import utcnow
from app.database.models import (
    Agent,
    Call,
    CallbackRequest,
    Campaign,
    ConsentRecord,
    Contact,
    Conversation,
    Message,
    Recording,
    Transcript,
    Workspace,
)
from app.database.session import sessionmaker
from app.knowledge.service import search

log = logging.getLogger(__name__)
registry = default_registry()

OUTCOME_TO_STATUS = {
    "interested": "interested", "callback_requested": "callback", "appointment_requested": "callback",
    "not_interested": "not_interested", "wrong_number": "wrong_number", "opted_out": "do_not_call",
    "information_provided": "contacted",
}  # fmt: skip


async def create_call(db: AsyncSession, *, ws_id: str, agent: Agent, mode: str, direction: str, channel: str,
                      contact: Contact | None = None, campaign: Campaign | None = None, job_id: str | None = None,
                      to_number: str | None = None, from_number: str | None = None, provider: str | None = None,
                      status: str = "queued", llm_model: str | None = None,
                      persona: str | None = None) -> Call:  # fmt: skip
    if mode not in ("WEBRTC", "SIMULATION", "PSTN"):
        raise ValueError("invalid call mode")
    call = Call(workspace_id=ws_id, agent_id=agent.id, agent_version=agent.version, mode=mode, direction=direction,
                channel=channel, contact_id=contact.id if contact else None,
                campaign_id=campaign.id if campaign else None, job_id=job_id,
                to_number=to_number if to_number is not None else (contact.phone_e164 if contact else None),
                from_number=from_number, provider=provider, status=status, started_at=utcnow(),
                llm_model=llm_model, simulation_persona=persona, metrics={})  # fmt: skip
    db.add(call)
    await db.flush()
    return call


class DbToolHost:
    """Executes agent tool requests against the database, scoped to one call/workspace.

    In SIMULATION mode, actions that would change real contact data (opt-outs, lead
    creation) are *not* applied: the callee is fictional. They are visible in the
    call's tool log instead. Callback requests are stored flagged as simulated.
    """

    def __init__(self, ws_id: str, call_id: str, mode: str, contact_id: str | None, default_country: str,
                 channel: str) -> None:  # fmt: skip
        self.ws_id = ws_id
        self.call_id = call_id
        self.mode = mode
        self.contact_id = contact_id
        self.default_country = default_country
        self.channel = channel

    @property
    def simulated(self) -> bool:
        return self.mode == "SIMULATION"

    async def create_callback_request(self, preferred_time: str, notes: str, kind: str) -> str:
        async with sessionmaker()() as db:
            cb = CallbackRequest(workspace_id=self.ws_id, call_id=self.call_id, contact_id=self.contact_id, kind=kind,
                                 preferred_time=preferred_time[:160], notes=notes[:2000], is_simulated=self.simulated)  # fmt: skip
            db.add(cb)
            if self.contact_id and not self.simulated:
                c = await db.get(Contact, self.contact_id)
                if c and c.workspace_id == self.ws_id and c.status not in ("do_not_call",):
                    c.status = "callback"
            await db.commit()
            return cb.id

    async def record_opt_out(self, reason: str) -> bool:
        if self.simulated:
            return True
        async with sessionmaker()() as db:
            call = await db.get(Call, self.call_id)
            phone = call.to_number if call and call.direction == "outbound" else (call.from_number if call else None)
            ok = await record_opt_out(db, self.ws_id, phone_e164=phone, contact_id=self.contact_id,
                                      source=f"call:{self.mode.lower()}", call_id=self.call_id)  # fmt: skip
            await db.commit()
            return ok

    async def search_knowledge(self, query: str, limit: int = 3) -> list[KnowledgeSnippet]:
        async with sessionmaker()() as db:
            return await search(db, self.ws_id, query, limit)

    async def capture_lead(self, details: dict[str, Any]) -> dict[str, Any]:
        phone = normalize_phone(details.get("phone"), self.default_country) if details.get("phone") else None
        if self.simulated:
            return {"contact_id": self.contact_id or "simulated", "created": False,
                    "phone_valid": bool(phone and phone.valid), "simulated": True}  # fmt: skip
        email = (details.get("email") or "").strip().lower() or None
        name = (details.get("name") or "").strip()
        first, _, last = name.partition(" ")
        async with sessionmaker()() as db:
            contact = await db.get(Contact, self.contact_id) if self.contact_id else None
            if contact is None or contact.workspace_id != self.ws_id:
                conds = []
                if phone and phone.e164:
                    conds.append(Contact.phone_e164 == phone.e164)
                if email:
                    conds.append(func.lower(Contact.email) == email)
                contact = None
                if conds:
                    contact = (await db.execute(select(Contact).where(Contact.workspace_id == self.ws_id,
                               Contact.deleted_at.is_(None), or_(*conds)).limit(1))).scalar_one_or_none()  # fmt: skip
            created = contact is None
            if contact is None:
                contact = Contact(workspace_id=self.ws_id, first_name=first[:120], last_name=last[:120],
                                  source="public_link" if self.channel == "public_link" else "conversation",
                                  status="lead", tags=["ai-lead"], custom_fields=[])  # fmt: skip
                db.add(contact)
            else:
                contact.first_name = contact.first_name or first[:120]
                contact.last_name = contact.last_name or last[:120]
                if contact.status in ("new", "contacted"):
                    contact.status = "lead"
            if phone and phone.e164 and not contact.phone_e164:
                contact.phone_raw, contact.phone_e164 = details.get("phone"), phone.e164
            if email and not contact.email:
                contact.email = email
            if details.get("company") and not contact.company:
                contact.company = str(details["company"])[:160]
            if details.get("requirement"):
                stamp = utcnow().strftime("%Y-%m-%d")
                contact.notes = ((contact.notes or "") + f"\n[{stamp}] Requirement (AI call): {details['requirement']}").strip()
            await db.flush()
            consent = False
            if details.get("consent_to_contact") is True:
                db.add(ConsentRecord(workspace_id=self.ws_id, contact_id=contact.id, phone_e164=contact.phone_e164,
                                     purpose="inquiry_followup", source="verbal_call", call_id=self.call_id,
                                     evidence=f"Verbal consent to follow-up given during call {self.call_id} "
                                              "(transcript retained).",
                                     expires_at=utcnow() + timedelta(days=policy_days(await db.get(Workspace, self.ws_id)))))  # fmt: skip
                consent = True
            call = await db.get(Call, self.call_id)
            if call is not None and call.contact_id is None:
                call.contact_id = contact.id
            self.contact_id = contact.id
            audit(db, "lead.captured", workspace_id=self.ws_id, entity_type="contact", entity_id=contact.id,
                  details={"call_id": self.call_id, "created": created, "consent": consent, "channel": self.channel})  # fmt: skip
            await db.commit()
            return {"contact_id": contact.id, "created": created, "phone_valid": bool(phone and phone.valid) if phone else True,
                    "consent_recorded": consent}  # fmt: skip


def policy_days(ws: Workspace | None) -> int:
    return policy_for(ws.compliance if ws else None).inquiry_followup_days


async def analyse(hub: ProviderHub, session: ConversationSession, objective: str) -> CallAnalysis:
    async with hub.ai_slot:
        return await analyze_call(hub.llm(), session.config.llm_options, session.transcript, session.effects, objective)


async def persist_conversation(
    call_id: str,
    session: ConversationSession,
    *,
    status: str,
    end_reason: str | None,
    analysis: CallAnalysis | None,
    error: ProviderError | None = None,
    metrics: dict[str, Any] | None = None,
    recorder: CallRecorder | None = None,
    simulated_duration: float | None = None,
) -> None:
    settings = get_settings()
    async with sessionmaker()() as db:
        call = await db.get(Call, call_id)
        if call is None:
            return
        ws = await db.get(Workspace, call.workspace_id)
        conv = Conversation(call_id=call.id, workspace_id=call.workspace_id, agent_id=call.agent_id,
                            system_prompt=session.system_prompt, effects=list(dict.fromkeys(session.effects)),
                            tool_log=session.tool_log, turn_count=session.user_turns)  # fmt: skip
        db.add(conv)
        await db.flush()
        for i, e in enumerate(session.transcript):
            db.add(Message(conversation_id=conv.id, workspace_id=call.workspace_id, seq=i, role=e.role,
                           content=e.text, interrupted=e.interrupted, metrics=_jsonable(e.metrics), created_at=e.at))  # fmt: skip
        text = render_transcript(session.transcript)
        if text:
            db.add(Transcript(call_id=call.id, workspace_id=call.workspace_id, content=text, word_count=len(text.split())))
        now = utcnow()
        call.ended_at = now
        call.status = status
        call.end_reason = end_reason
        if simulated_duration is not None:
            call.duration_seconds = round(simulated_duration, 1)
        elif call.answered_at:
            call.duration_seconds = round((now - call.answered_at).total_seconds(), 1)
        if analysis:
            call.outcome = analysis.outcome
            call.summary = analysis.summary
            call.sentiment = analysis.sentiment
            call.analysis_error = analysis.error
        if error is not None:
            call.error_code, call.error_detail = error.code, str(error)[:2000]
        call.metrics = {**(call.metrics or {}), **(metrics or {})}
        if recorder is not None and ws is not None and policy_for(ws.compliance).recording_enabled and recorder.duration_seconds() > 0.5:
            rel = Path("recordings") / call.workspace_id / f"{call.id}.wav"
            path = settings.data_dir / rel
            size = recorder.write(path)
            db.add(Recording(call_id=call.id, workspace_id=call.workspace_id, relative_path=str(rel),
                             duration_seconds=round(recorder.duration_seconds(), 1), size_bytes=size,
                             sha256=hashlib.sha256(path.read_bytes()).hexdigest()))  # fmt: skip
        # real conversations update the CRM status; simulated outcomes never touch real contacts
        if call.mode != "SIMULATION" and call.contact_id and analysis and analysis.outcome in OUTCOME_TO_STATUS:
            contact = await db.get(Contact, call.contact_id)
            if contact and contact.workspace_id == call.workspace_id and contact.status != "do_not_call":
                contact.status = OUTCOME_TO_STATUS[analysis.outcome]
        await db.commit()


def _jsonable(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (d or {}).items():
        if isinstance(v, str | int | float | bool) or v is None or isinstance(v, list | dict):
            out[k] = v
        else:
            out[k] = str(v)
    return out


def estimate_speech_seconds(session: ConversationSession) -> float:
    words = sum(len(e.text.split()) for e in session.transcript if e.role in ("user", "assistant"))
    turns = sum(1 for e in session.transcript if e.role in ("user", "assistant"))
    return words / 2.6 + turns * 0.8  # ~156 wpm plus turn-taking gaps


# ---------------------------------------------------------------- simulation
async def run_simulated_call(*, ws_id: str, agent_id: str, contact_id: str | None = None,
                             campaign_id: str | None = None, job_id: str | None = None,
                             persona: str = "questions", callee_mode: str = "scripted", seed: str | None = None,
                             direction: str = "outbound", from_number: str | None = None,
                             scenario: SimulationScenario | None = None, call_id: str | None = None) -> str:  # fmt: skip
    """Run a complete text-mode simulated call. Returns the call id. Never places a real call."""
    hub = get_hub()
    async with sessionmaker()() as db:
        ws = await db.get(Workspace, ws_id)
        agent = await db.get(Agent, agent_id)
        if ws is None or agent is None or agent.workspace_id != ws_id:
            raise ValueError("workspace/agent mismatch")
        contact = await db.get(Contact, contact_id) if contact_id else None
        if contact is not None and contact.workspace_id != ws_id:
            raise ValueError("contact belongs to another workspace")
        campaign = await db.get(Campaign, campaign_id) if campaign_id else None
        cfg = session_config(agent, ws, hub, channel="simulation", campaign=campaign, contact=contact)
        if call_id:
            call = await db.get(Call, call_id)
            assert call is not None
        else:
            call = await create_call(db, ws_id=ws_id, agent=agent, mode="SIMULATION", direction=direction,
                                     channel="campaign" if campaign else "simulation", contact=contact,
                                     campaign=campaign, job_id=job_id, provider="simulator", status="dialing",
                                     llm_model=cfg.llm_options.model, persona=persona, from_number=from_number)  # fmt: skip
        call.simulation_persona = persona
        await db.commit()
        call_id = call.id
        default_country = ws.default_country
        objective = campaign.objective if campaign else ""
        contact_name = contact.first_name if contact else None
        to_number = call.to_number or "+910000000000"

    provider = SimulatedTelephonyProvider(scenario or SimulationScenario())
    if direction == "outbound":
        tel = await provider.dial(to_number, from_number, seed=seed or call_id)
    else:
        tel = await provider.simulate_inbound(from_number or "simulated-caller")
    if tel.status != CallStatus.ANSWERED:
        status = tel.status.value
        outcome = "failed" if status == "failed" else "no_answer"
        async with sessionmaker()() as db:
            call = await db.get(Call, call_id)
            assert call is not None
            call.status, call.outcome, call.ended_at, call.duration_seconds = status, outcome, utcnow(), 0.0
            call.end_reason = f"simulated_{status}"
            call.metrics = {"telephony_events": tel.events, "simulated": True}
            await db.commit()
        return call_id

    async with sessionmaker()() as db:
        call = await db.get(Call, call_id)
        assert call is not None
        call.status, call.answered_at, call.provider_call_id = "in_progress", utcnow(), tel.provider_call_id
        await db.commit()

    host = DbToolHost(ws_id, call_id, "SIMULATION", contact_id, default_country, "simulation")
    session = ConversationSession(cfg, hub.llm(), registry, ToolContext(ws_id, call_id, host, call_id, contact_id),
                                  knowledge=(lambda q: host.search_knowledge(q, 3)) if cfg.use_knowledge else None)  # fmt: skip
    callee = (LLMCallee(hub.llm(), cfg.llm_options, persona, contact_name) if callee_mode == "llm"
              else ScriptedCallee(persona, contact_name))  # fmt: skip
    error: ProviderError | None = None
    analysis: CallAnalysis | None = None
    agent_text = session.greeting_text()
    session.add_assistant(agent_text)
    end_reason = "callee_hung_up"
    try:
        for _ in range(min(cfg.max_turns, 12)):
            reply = await callee.reply(agent_text) if callee_mode != "llm" else await _llm_reply(hub, callee, agent_text)
            if not reply:
                break
            async with hub.ai_slot:
                agent_text, _m = await session.respond_text(reply)
            if session.end_requested:
                end_reason = session.end_reason or "agent_ended"
                break
        analysis = await analyse(hub, session, objective)
        status = "completed"
    except ProviderError as exc:
        error = exc
        status, end_reason = "failed", "provider_error"
        analysis = CallAnalysis("failed", None, source="rules", error=str(exc))
    await provider.hangup(tel)
    await persist_conversation(call_id, session, status=status, end_reason=end_reason, analysis=analysis, error=error,
                               metrics={"telephony_events": tel.events, "simulated": True, "persona": persona,
                                        "callee_mode": callee_mode},
                               simulated_duration=estimate_speech_seconds(session))  # fmt: skip
    if error is not None:
        async with sessionmaker()() as db:
            system_event(db, "simulation", f"Simulated call failed: {error}", level="error", workspace_id=ws_id,
                         details={"call_id": call_id, "code": error.code})  # fmt: skip
            await db.commit()
        raise error
    return call_id


async def _llm_reply(hub: ProviderHub, callee: Any, agent_text: str) -> str | None:
    async with hub.ai_slot:
        return await callee.reply(agent_text)
