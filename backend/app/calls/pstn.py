"""Real outbound PSTN call through the configured provider (Asterisk ARI).

**Integration-ready, not production-verified**: exercised in tests with a mocked ARI
server; it has not been run against a live carrier from this repository. It is only
reachable when ``VA_REAL_TELEPHONY_ENABLED=true``, a provider is configured and enabled,
and the workspace's PSTN prerequisites are recorded (see compliance policy).
"""

from __future__ import annotations

import contextlib

from engine.audio.pcm import STT_SAMPLE_RATE, StreamResampler
from engine.audio.vad import SpeechSegmenter
from engine.conversation.outcome import CallAnalysis
from engine.conversation.pipeline import VoicePipeline
from engine.conversation.session import ConversationSession
from engine.conversation.tools import ToolContext
from engine.providers.base import ProviderError
from engine.providers.telephony.base import CallStatus, TelephonyError

from app.agents.runtime import pipeline_config, session_config, voice_settings
from app.ai.hub import get_hub
from app.calls.service import DbToolHost, analyse, create_call, persist_conversation, registry
from app.compliance.policy import policy_for
from app.core.config import get_settings
from app.database.base import utcnow
from app.database.models import Agent, Call, Campaign, Contact, Workspace
from app.database.session import sessionmaker


async def run_pstn_call(*, ws_id: str, agent_id: str, contact_id: str, campaign_id: str | None,
                        job_id: str | None) -> str:  # fmt: skip
    settings = get_settings()
    if not settings.real_telephony_enabled:
        raise TelephonyError("Real telephony is disabled", code="pstn_disabled")
    hub = get_hub()
    async with sessionmaker()() as db:
        ws = await db.get(Workspace, ws_id)
        agent = await db.get(Agent, agent_id)
        contact = await db.get(Contact, contact_id)
        campaign = await db.get(Campaign, campaign_id) if campaign_id else None
        if not (ws and agent and contact) or agent.workspace_id != ws_id or contact.workspace_id != ws_id:
            raise ValueError("workspace mismatch")
        missing = policy_for(ws.compliance).pstn_prerequisites_missing()
        if missing:  # defence in depth: the scheduler already checked eligibility
            raise TelephonyError("; ".join(missing), code="pstn_prerequisites")
        provider = hub.telephony(real=True)
        cfg = session_config(agent, ws, hub, channel="phone", campaign=campaign, contact=contact)
        call = await create_call(db, ws_id=ws_id, agent=agent, mode="PSTN", direction="outbound", channel="campaign",
                                 contact=contact, campaign=campaign, job_id=job_id, provider=provider.name,
                                 status="dialing", llm_model=cfg.llm_options.model)  # fmt: skip
        await db.commit()
        call_id, to_number, default_country = call.id, contact.phone_e164 or "", ws.default_country
        agent_snapshot = agent

    tel = await provider.dial(to_number, None, ring_timeout_s=30)
    if tel.status != CallStatus.ANSWERED or tel.media is None:
        async with sessionmaker()() as db:
            c = await db.get(Call, call_id)
            assert c is not None
            c.status, c.outcome, c.ended_at = tel.status.value, "no_answer" if tel.status != CallStatus.FAILED else "failed", utcnow()
            c.end_reason, c.provider_call_id = tel.hangup_cause or tel.status.value, tel.provider_call_id
            c.metrics = {"telephony_events": tel.events}
            await db.commit()
        return call_id

    async with sessionmaker()() as db:
        c = await db.get(Call, call_id)
        assert c is not None
        c.status, c.answered_at, c.provider_call_id = "in_progress", utcnow(), tel.provider_call_id
        await db.commit()

    host = DbToolHost(ws_id, call_id, "PSTN", contact_id, default_country, "campaign")
    session = ConversationSession(cfg, hub.llm(), registry, ToolContext(ws_id, call_id, host, call_id, contact_id),
                                  knowledge=(lambda q: host.search_knowledge(q, 3)) if cfg.use_knowledge else None)  # fmt: skip
    tts_name, voice = voice_settings(agent_snapshot, hub)
    pcfg = pipeline_config(agent_snapshot, hub)
    media = tel.media
    pipeline = VoicePipeline(session, hub.stt(), hub.tts(tts_name), SpeechSegmenter(hub.vad(), pcfg.segmenter),
                             media.sink, voice, None, pcfg, ai_slot=hub.ai_slot)  # fmt: skip
    resampler = StreamResampler(16_000, STT_SAMPLE_RATE)
    media.set_audio_handler(lambda pcm: pipeline.push_audio(resampler.process(pcm)))
    analysis: CallAnalysis | None = None
    try:
        await pipeline.start()
        while not pipeline.ended.is_set():
            await pipeline.wait_ended(timeout=1.0)
            if tel.status != CallStatus.ANSWERED:
                await pipeline.stop("remote_hangup")
        analysis = await analyse(hub, session, campaign.objective if campaign else "")
    except ProviderError:
        analysis = CallAnalysis("failed", None, source="rules")
    finally:
        with contextlib.suppress(Exception):
            await provider.hangup(tel, pipeline.end_reason or "normal")
    await persist_conversation(call_id, session, status="failed" if pipeline.error else "completed",
                               end_reason=pipeline.end_reason, analysis=analysis, error=pipeline.error,
                               metrics={"telephony_events": tel.events,
                                        "turns": [t.as_dict() for t in pipeline.turns]})  # fmt: skip
    return call_id
