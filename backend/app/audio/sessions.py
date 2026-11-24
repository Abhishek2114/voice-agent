"""Live browser voice sessions (WebRTC ↔ VoicePipeline ↔ local AI providers)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from engine.audio.recorder import CallRecorder
from engine.audio.vad import SpeechSegmenter
from engine.conversation.outcome import CallAnalysis
from engine.conversation.pipeline import VoicePipeline
from engine.conversation.session import ConversationSession
from engine.conversation.tools import ToolContext
from engine.providers.base import ProviderError
from engine.streaming.sink import QueueAudioSink
from engine.streaming.webrtc import WebRTCTransport

from app.agents.runtime import pipeline_config, session_config, voice_settings
from app.ai.hub import get_hub
from app.calls.service import DbToolHost, analyse, create_call, persist_conversation, registry
from app.compliance.policy import policy_for
from app.core.config import get_settings
from app.core.errors import APIError
from app.database.base import utcnow
from app.database.models import Agent, Call, Contact, Workspace
from app.database.session import sessionmaker

log = logging.getLogger(__name__)


@dataclass
class LiveSession:
    call_id: str
    workspace_id: str
    user_id: str | None
    channel: str
    transport: WebRTCTransport
    pipeline: VoicePipeline
    session: ConversationSession
    recorder: CallRecorder | None
    started: float = field(default_factory=time.monotonic)
    runner: asyncio.Task[None] | None = None
    finalized: asyncio.Event = field(default_factory=asyncio.Event)


class VoiceSessionManager:
    def __init__(self) -> None:
        self.sessions: dict[str, LiveSession] = {}
        self._lock = asyncio.Lock()

    @property
    def active(self) -> int:
        return len(self.sessions)

    async def preflight(self, agent: Agent) -> None:
        """Fail fast with an explicit error rather than starting a call that cannot work."""
        hub = get_hub()
        opts = hub.llm_options(agent.llm)
        try:
            models = {m.name: m for m in await hub.llm().list_models()}
        except ProviderError as exc:
            raise APIError(503, "llm_unavailable", f"Local LLM unavailable: {exc}") from exc
        m = models.get(opts.model)
        if m is None:
            raise APIError(503, "model_not_found", f"Model '{opts.model}' is not installed in Ollama")
        if not m.is_local and not get_settings().allow_remote_llm_models:
            raise APIError(400, "remote_model_refused", f"Model '{opts.model}' is not local; choose a local model")
        provider, _ = voice_settings(agent, hub)
        stt_h, tts_h = await asyncio.gather(hub.stt().health(), hub.tts(provider).health())
        for h, code in ((stt_h, "stt_unavailable"), (tts_h, "tts_unavailable")):
            if h.status == "unavailable":
                raise APIError(503, code, h.detail)
        try:
            hub.vad()
        except Exception as exc:
            raise APIError(503, "vad_unavailable", f"Voice activity detection unavailable: {exc}") from exc

    async def start(self, *, ws: Workspace, agent: Agent, sdp: str, sdp_type: str, user_id: str | None,
                    channel: str = "dashboard", contact: Contact | None = None) -> tuple[str, Any]:  # fmt: skip
        settings = get_settings()
        if not agent.is_active:
            raise APIError(400, "agent_inactive", "This agent is deactivated")
        async with self._lock:
            if self.active >= settings.max_voice_sessions:
                raise APIError(503, "capacity", f"All {settings.max_voice_sessions} voice slots are busy. Try again shortly.")
            await self.preflight(agent)
            hub = get_hub()
            cfg = session_config(agent, ws, hub, channel="voice", contact=contact)
            async with sessionmaker()() as db:
                call = await create_call(db, ws_id=ws.id, agent=agent, mode="WEBRTC", direction="inbound",
                                         channel=channel, contact=contact, status="connecting",
                                         provider="webrtc", llm_model=cfg.llm_options.model)  # fmt: skip
                await db.commit()
                call_id = call.id
            policy = policy_for(ws.compliance)
            recorder = CallRecorder(max_seconds=agent.max_duration_seconds + 30) if policy.recording_enabled else None
            sink = QueueAudioSink(48_000)
            host = DbToolHost(ws.id, call_id, "WEBRTC", contact.id if contact else None, ws.default_country, channel)
            session = ConversationSession(cfg, hub.llm(), registry, ToolContext(ws.id, call_id, host, call_id,
                                          contact.id if contact else None),
                                          knowledge=(lambda q: host.search_knowledge(q, 3)) if cfg.use_knowledge else None,
                                          session_id=call_id)  # fmt: skip
            provider, voice = voice_settings(agent, hub)
            pcfg = pipeline_config(agent, hub)
            transport_ref: dict[str, WebRTCTransport] = {}

            def on_event(kind: str, data: dict[str, Any]) -> None:
                t = transport_ref.get("t")
                if t is not None:
                    t.send_event(kind, data)

            pipeline = VoicePipeline(session, hub.stt(), hub.tts(provider), SpeechSegmenter(hub.vad(), pcfg.segmenter),
                                     sink, voice, on_event, pcfg, ai_slot=hub.ai_slot)  # fmt: skip

            async def on_control(msg: dict[str, Any]) -> None:
                await self.control(call_id, msg)

            async def on_closed(reason: str) -> None:
                if not pipeline.ended.is_set():
                    await pipeline.stop(reason)

            transport = WebRTCTransport(sink, recorder=recorder, on_control=on_control, on_closed=on_closed)
            transport_ref["t"] = transport
            transport.set_audio_handler(pipeline.push_audio)
            try:
                answer = await transport.accept_offer(sdp, sdp_type)
            except Exception as exc:
                await transport.close("bad_offer")
                async with sessionmaker()() as db:
                    c = await db.get(Call, call_id)
                    if c:
                        c.status, c.error_code, c.error_detail, c.ended_at = "failed", "webrtc_error", str(exc)[:500], utcnow()
                        await db.commit()
                raise APIError(400, "webrtc_error", "Could not negotiate the WebRTC session") from exc
            # While the greeting plays, load the model AND pre-process this agent's system prompt so
            # Ollama's prompt-prefix cache is hot for the first real turn.
            task = asyncio.create_task(_prime_prompt(hub.llm(), session))
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            live = LiveSession(call_id, ws.id, user_id, channel, transport, pipeline, session, recorder)
            self.sessions[call_id] = live
            live.runner = asyncio.create_task(self._run(live, objective=""))
            return call_id, answer

    async def _run(self, live: LiveSession, objective: str) -> None:
        pipeline, transport = live.pipeline, live.transport
        try:
            try:
                await asyncio.wait_for(transport.connected.wait(), timeout=30)
            except TimeoutError:
                await pipeline.stop("connection_timeout")
            if not pipeline.ended.is_set():
                async with sessionmaker()() as db:
                    c = await db.get(Call, live.call_id)
                    if c:
                        c.status, c.answered_at = "in_progress", utcnow()
                        await db.commit()
                await pipeline.start()
                await pipeline.ended.wait()
        except Exception:
            log.exception("voice session crashed")
            if not pipeline.ended.is_set():
                await pipeline.stop("internal_error")
        finally:
            await transport.close(pipeline.end_reason or "ended")
            await self._finalize(live, objective)

    async def _finalize(self, live: LiveSession, objective: str) -> None:
        pipeline = live.pipeline
        analysis: CallAnalysis | None = None
        status = "failed" if pipeline.end_reason in ("provider_error", "internal_error", "connection_timeout") else "completed"
        try:
            if live.session.transcript:
                analysis = await analyse(get_hub(), live.session, objective)
        except Exception as exc:  # analysis must never lose the transcript
            analysis = CallAnalysis(None, None, source="none", error=f"analysis failed: {exc.__class__.__name__}")
        metrics = {
            "turns": [t.as_dict() for t in pipeline.turns],
            "interrupt_latencies_ms": pipeline.interrupt_latencies_ms,
            "frames_in": live.transport.frames_in,
        }
        try:
            await persist_conversation(live.call_id, live.session, status=status, end_reason=pipeline.end_reason,
                                       analysis=analysis, error=pipeline.error, metrics=metrics,
                                       recorder=live.recorder)  # fmt: skip
        except Exception:
            log.exception("failed to persist call %s", live.call_id)
        self.sessions.pop(live.call_id, None)
        live.finalized.set()

    def get(self, call_id: str, workspace_id: str | None = None) -> LiveSession:
        live = self.sessions.get(call_id)
        if live is None or (workspace_id is not None and live.workspace_id != workspace_id):
            raise APIError(404, "not_found", "Live session not found")
        return live

    async def control(self, call_id: str, msg: dict[str, Any]) -> None:
        live = self.sessions.get(call_id)
        if live is None:
            return
        kind = msg.get("type")
        if kind == "interrupt":
            await live.pipeline.interrupt("user")
        elif kind == "mute":
            live.pipeline.set_muted(bool(msg.get("muted")))
            live.transport.send_event("muted", {"muted": live.pipeline.muted})
        elif kind == "end":
            await live.pipeline.stop("user_ended")
        elif kind == "ping":
            live.transport.send_event("pong", {"t": msg.get("t")})

    async def end(self, call_id: str, workspace_id: str, reason: str = "user_ended") -> None:
        live = self.get(call_id, workspace_id)
        await live.pipeline.stop(reason)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(live.finalized.wait(), timeout=60)

    async def shutdown(self) -> None:
        for live in list(self.sessions.values()):
            await live.pipeline.stop("server_shutdown")
        for live in list(self.sessions.values()):
            if live.runner:
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(live.runner, timeout=30)


async def _prime_prompt(llm: Any, session: ConversationSession) -> None:
    from engine.providers.llm.base import ChatMessage, LLMOptions

    opts = session.config.llm_options
    primer = LLMOptions(**{**opts.__dict__, "max_tokens": 1})
    msgs = [ChatMessage("system", session.system_prompt), ChatMessage("user", "Hello")]
    async for _ in llm.stream_chat(msgs, primer):
        pass


manager = VoiceSessionManager()
