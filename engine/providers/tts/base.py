"""Text-to-speech provider interface."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

from engine.providers.base import ProviderError, ProviderHealth


@dataclass
class VoiceSettings:
    voice: str
    speaker: str | int | None = None
    speed: float = 1.0  # >1 faster


@dataclass
class VoiceInfo:
    id: str
    name: str
    language: str | None = None
    num_speakers: int = 1
    speakers: list[str] = field(default_factory=list)
    license_note: str | None = None


@dataclass
class TTSResult:
    pcm: np.ndarray
    sample_rate: int
    latency_ms: float
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def seconds(self) -> float:
        return self.pcm.size / float(self.sample_rate) if self.sample_rate else 0.0


class TTSError(ProviderError):
    code = "tts_error"


class TTSProvider(Protocol):
    name: str
    # False: each call synthesises the complete text before returning audio.
    # The engine pipelines at sentence granularity, it does not stream sub-sentence audio.
    streaming: bool

    async def health(self) -> ProviderHealth: ...

    async def list_voices(self) -> list[VoiceInfo]: ...

    async def synthesize(self, text: str, voice: VoiceSettings) -> TTSResult: ...
