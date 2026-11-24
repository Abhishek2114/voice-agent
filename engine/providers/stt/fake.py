"""Test double for STT: returns queued transcripts. Never exposed in the UI."""

from __future__ import annotations

import collections

import numpy as np

from engine.audio.pcm import duration_seconds
from engine.providers.base import ProviderHealth
from engine.providers.stt.base import STTError, STTResult


class FakeSTT:
    name = "fake_stt"
    streaming = False

    def __init__(self, transcripts: list[str] | None = None, *, fail: bool = False) -> None:
        self.queue: collections.deque[str] = collections.deque(transcripts or [])
        self.fail = fail
        self.received: list[np.ndarray] = []

    async def health(self) -> ProviderHealth:
        return ProviderHealth(self.name, "unavailable" if self.fail else "ok", "fake")

    async def transcribe(self, pcm16: np.ndarray, sample_rate: int = 16_000, language: str = "en") -> STTResult:
        self.received.append(pcm16)
        if self.fail:
            raise STTError("fake failure", code="stt_unavailable", retryable=True)
        text = self.queue.popleft() if self.queue else ""
        return STTResult(text=text, audio_seconds=duration_seconds(pcm16, sample_rate), latency_ms=1.0)
