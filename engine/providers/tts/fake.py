"""Test double for TTS: produces a quiet tone whose length scales with the text."""

from __future__ import annotations

import asyncio

from engine.audio.pcm import tone
from engine.providers.base import ProviderHealth
from engine.providers.tts.base import TTSError, TTSResult, VoiceInfo, VoiceSettings


class FakeTTS:
    name = "fake_tts"
    streaming = False

    def __init__(self, sample_rate: int = 16_000, seconds_per_char: float = 0.01, delay_s: float = 0.0,
                 fail: bool = False) -> None:  # fmt: skip
        self.sample_rate = sample_rate
        self.seconds_per_char = seconds_per_char
        self.delay_s = delay_s
        self.fail = fail
        self.spoken: list[str] = []

    async def health(self) -> ProviderHealth:
        return ProviderHealth(self.name, "unavailable" if self.fail else "ok", "fake")

    async def list_voices(self) -> list[VoiceInfo]:
        return [VoiceInfo(id="fake", name="fake")]

    async def synthesize(self, text: str, voice: VoiceSettings) -> TTSResult:
        if self.fail:
            raise TTSError("fake failure", code="tts_unavailable", retryable=True)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        self.spoken.append(text)
        pcm = tone(220.0, max(0.05, len(text) * self.seconds_per_char), self.sample_rate, amplitude=0.05)
        return TTSResult(pcm=pcm, sample_rate=self.sample_rate, latency_ms=1.0)
