"""Post-call analysis: summary + outcome classification.

Deterministic signals (tool effects, absence of user speech) take precedence over the
LLM. If the LLM is unavailable the analysis reports an error instead of guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from engine.conversation.session import TranscriptEntry
from engine.providers.base import ProviderError
from engine.providers.llm.base import ChatMessage, LLMOptions, LLMProvider

OUTCOMES = [
    "interested",
    "not_interested",
    "callback_requested",
    "information_provided",
    "appointment_requested",
    "wrong_number",
    "no_answer",
    "failed",
    "opted_out",
]

ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": OUTCOMES},
        "summary": {"type": "string"},
        "sentiment": {"type": "string", "enum": ["positive", "neutral", "negative"]},
        "key_points": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["outcome", "summary", "sentiment"],
}


@dataclass
class CallAnalysis:
    outcome: str | None
    summary: str | None
    sentiment: str | None = None
    key_points: list[str] = field(default_factory=list)
    source: str = "llm"  # llm | rules | none
    error: str | None = None


def render_transcript(entries: list[TranscriptEntry]) -> str:
    lines = []
    for e in entries:
        if e.role == "tool":
            lines.append(f"[action] {e.text}")
        elif e.role in ("user", "assistant"):
            who = "Agent" if e.role == "assistant" else "Caller"
            lines.append(f"{who}: {e.text}{' (interrupted)' if e.interrupted else ''}")
    return "\n".join(lines)


def rule_outcome(entries: list[TranscriptEntry], effects: list[str]) -> str | None:
    if "opted_out" in effects:
        return "opted_out"
    if "wrong_number" in effects:
        return "wrong_number"
    if not any(e.role == "user" and e.text.strip() for e in entries):
        return "no_answer"
    return None


def hinted_outcome(effects: list[str]) -> str | None:
    if "appointment_requested" in effects:
        return "appointment_requested"
    if "callback_requested" in effects:
        return "callback_requested"
    if "interest:interested" in effects or "lead_captured" in effects:
        return "interested"
    if "interest:not_interested" in effects:
        return "not_interested"
    return None


async def analyze_call(
    llm: LLMProvider | None,
    options: LLMOptions | None,
    entries: list[TranscriptEntry],
    effects: list[str],
    objective: str = "",
) -> CallAnalysis:
    forced = rule_outcome(entries, effects)
    if forced == "no_answer":
        return CallAnalysis("no_answer", "The other party did not speak during the call.", "neutral", source="rules")
    transcript = render_transcript(entries)
    if llm is None or options is None:
        return CallAnalysis(forced or hinted_outcome(effects), None, source="rules" if forced else "none",
                            error="No LLM configured for analysis")  # fmt: skip
    prompt = (
        "Analyse this call transcript between an AI agent and a caller. Write a factual 1-3 sentence "
        "summary (do not invent details that are not in the transcript) and classify the outcome as one of: "
        + ", ".join(OUTCOMES)
        + ". Bookings/appointments are only ever *requested*, never completed."
        + (f"\nCall objective: {objective}" if objective else "")
        + f"\n\nTranscript:\n{transcript[:12000]}"
    )
    opts = LLMOptions(**{**options.__dict__, "temperature": 0.1, "max_tokens": 300})
    try:
        data = await llm.chat_json([ChatMessage("user", prompt)], opts, ANALYSIS_SCHEMA)
    except ProviderError as exc:
        return CallAnalysis(forced or hinted_outcome(effects), None, source="rules" if forced else "none",
                            error=f"Analysis unavailable: {exc}")  # fmt: skip
    outcome = data.get("outcome") if data.get("outcome") in OUTCOMES else None
    hint = hinted_outcome(effects)
    # deterministic effects override the model where they are unambiguous
    final = forced or (hint if hint in ("appointment_requested", "callback_requested") else None) or outcome or hint
    return CallAnalysis(
        outcome=final,
        summary=str(data.get("summary") or "").strip()[:2000] or None,
        sentiment=data.get("sentiment") if data.get("sentiment") in ("positive", "neutral", "negative") else None,
        key_points=[str(k)[:300] for k in (data.get("key_points") or [])][:8],
        source="llm",
    )
