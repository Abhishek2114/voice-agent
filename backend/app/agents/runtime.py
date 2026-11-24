"""Turns a stored Agent (+ workspace policy) into engine configuration."""

from __future__ import annotations

import re
from typing import Any

from engine.audio.vad import SegmenterConfig
from engine.conversation.pipeline import PipelineConfig
from engine.conversation.prompt import AgentPersona
from engine.conversation.session import DEFAULT_END_PHRASES, DEFAULT_FALLBACKS, SessionConfig
from engine.conversation.tools import DEFAULT_TOOLS
from engine.providers.tts.base import VoiceSettings

from app.ai.hub import ProviderHub
from app.compliance.policy import policy_for
from app.database.models import Agent, Campaign, Contact, Workspace

_AI_WORDS = re.compile(r"\b(ai|a\.i\.|artificial intelligence|virtual assistant|automated)\b", re.IGNORECASE)


def agent_tools(agent: Agent) -> list[str]:
    if not agent.tools:
        return list(DEFAULT_TOOLS)
    return [t.tool_name for t in agent.tools if t.enabled]


def compose_greeting(agent: Agent, ws: Workspace, *, channel: str) -> str:
    """Greeting with mandatory AI-identity and recording disclosures prepended when required."""
    policy = policy_for(ws.compliance)
    parts: list[str] = []
    greeting = (agent.greeting or "").strip()
    needs_disclosure = policy.ai_disclosure_required and not _AI_WORDS.search(greeting)
    if needs_disclosure and channel == "phone":
        # phone calls: the policy disclosure comes first, before anything else is said
        parts.append(policy.ai_disclosure_text.replace("{business}", ws.name))
        if greeting:
            parts.append(greeting)
    elif needs_disclosure and greeting:
        # browser calls: keep the agent's own opening, disclose in the very next sentence
        first, *rest = re.split(r"(?<=[.!?])\s+", greeting, maxsplit=1)
        parts.append(first)
        parts.append(f"I'm {agent.name}, an AI assistant for {ws.name}.")
        parts.extend(r for r in rest if r.strip())
    elif needs_disclosure:
        parts.append(f"Hi, you're speaking with {agent.name}, an AI assistant for {ws.name}.")
    elif greeting:
        parts.append(greeting)
    if policy.recording_enabled:
        parts.append(policy.recording_disclosure_text)
    return " ".join(parts) or f"Hello, this is {agent.name}, an AI assistant for {ws.name}. How can I help?"


def session_config(agent: Agent, ws: Workspace, hub: ProviderHub, *, channel: str,
                   campaign: Campaign | None = None, contact: Contact | None = None) -> SessionConfig:  # fmt: skip
    rules = agent.end_rules or {}
    persona = AgentPersona(
        name=agent.name, business_name=ws.name, personality=agent.personality,
        instructions=agent.system_instructions, objectives=[str(o) for o in (agent.objectives or [])],
        business_knowledge=agent.business_knowledge, language=agent.language,
        channel={"phone": "phone", "simulation": "text-simulation"}.get(channel, "voice"),
        campaign_objective=campaign.objective if campaign else "",
        contact_name=contact.first_name if contact and contact.first_name else None,
    )  # fmt: skip
    fallbacks = {**DEFAULT_FALLBACKS, **{k: str(v) for k, v in (agent.fallback_responses or {}).items() if v}}
    return SessionConfig(
        persona=persona,
        llm_options=hub.llm_options(agent.llm),
        greeting=compose_greeting(agent, ws, channel="phone" if channel in ("phone", "simulation") else "voice"),
        tools_allowed=agent_tools(agent),
        fallbacks=fallbacks,
        end_phrases=list(rules.get("end_phrases") or DEFAULT_END_PHRASES),
        max_turns=int(rules.get("max_turns") or 40),
        use_knowledge=agent.use_knowledge_base,
    )


def voice_settings(agent: Agent, hub: ProviderHub) -> tuple[str, VoiceSettings]:
    t = agent.tts or {}
    provider = str(t.get("provider") or hub.provider_name("tts", "piper"))
    voice = str(t.get("voice") or (hub.default_voice() if provider == "piper" else ""))
    speaker = t.get("speaker")
    return provider, VoiceSettings(voice=voice, speaker=speaker if speaker not in ("", None) else None,
                                   speed=float(t.get("speed") or 1.0))  # fmt: skip


def pipeline_config(agent: Agent, hub: ProviderHub) -> PipelineConfig:
    a = hub.audio()
    stt = agent.stt or {}
    return PipelineConfig(
        segmenter=SegmenterConfig(
            threshold=float(a["vad_threshold"]), neg_threshold=max(0.05, float(a["vad_threshold"]) - 0.15),
            min_speech_ms=int(a["min_speech_ms"]), end_silence_ms=int(stt.get("end_silence_ms") or a["end_silence_ms"]),
            pre_roll_ms=int(a["pre_roll_ms"]),
        ),
        barge_in=bool(a["barge_in"]),
        barge_in_min_ms=int(a["barge_in_min_ms"]),
        max_duration_s=float(agent.max_duration_seconds),
        silence_timeout_s=float(agent.silence_timeout_seconds),
        language=str(stt.get("language") or agent.language.split("-")[0] or hub.stt_language()),
    )  # fmt: skip


def snapshot(agent: Agent) -> dict[str, Any]:
    return {
        "name": agent.name, "description": agent.description, "personality": agent.personality,
        "system_instructions": agent.system_instructions, "greeting": agent.greeting,
        "objectives": agent.objectives, "business_knowledge": agent.business_knowledge, "language": agent.language,
        "llm": agent.llm, "stt": agent.stt, "tts": agent.tts, "max_duration_seconds": agent.max_duration_seconds,
        "silence_timeout_seconds": agent.silence_timeout_seconds, "end_rules": agent.end_rules,
        "fallback_responses": agent.fallback_responses, "use_knowledge_base": agent.use_knowledge_base,
        "tools": agent_tools(agent),
    }  # fmt: skip
