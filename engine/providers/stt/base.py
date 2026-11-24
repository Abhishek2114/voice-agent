"""Speech-to-text provider interface and shared post-processing."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

from engine.providers.base import ProviderError, ProviderHealth


@dataclass
class STTResult:
    text: str
    language: str | None = None
    audio_seconds: float = 0.0
    latency_ms: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


class STTError(ProviderError):
    code = "stt_error"


class STTProvider(Protocol):
    name: str
    streaming: bool  # True only if partial hypotheses are produced while audio arrives

    async def health(self) -> ProviderHealth: ...

    async def transcribe(self, pcm16: np.ndarray, sample_rate: int = 16_000, language: str = "en") -> STTResult: ...


# Whisper emits these on silence/noise; they must never reach the LLM as user speech.
_NON_SPEECH = re.compile(r"\[(?:[^\]]*)\]|\((?:[^)]*)\)|\*[^*]*\*|♪+")
_HALLUCINATIONS = {
    "",
    "you",
    "thank you.",
    "thanks for watching!",
    "thank you for watching.",
    "bye.",
    ".",
}


def clean_transcript(text: str, audio_seconds: float) -> str:
    """Normalise whisper output and drop well-known non-speech hallucinations."""
    cleaned = _NON_SPEECH.sub(" ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # very short audio that decoded to a stock phrase is almost always noise
    if cleaned.lower() in _HALLUCINATIONS and audio_seconds < 1.2:
        return ""
    if cleaned.lower() in {"", ".", "you"}:
        return ""
    return cleaned
