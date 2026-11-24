"""Piper TTS through its own local HTTP server (``python -m piper.http_server``).

Piper >= 1.3 is GPL-3.0. voice-agent (MIT) never imports it; Piper runs as a separate
local process installed into ``.tools/piper`` and is reached over HTTP on 127.0.0.1.
Endpoints used: ``POST /synthesize``, ``GET /voices``.
"""

from __future__ import annotations

import time

import httpx

from engine.audio.pcm import read_wav
from engine.providers.base import ProviderHealth
from engine.providers.tts.base import TTSError, TTSResult, VoiceInfo, VoiceSettings


class PiperHttpTTS:
    name = "piper"
    streaming = False

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:5005",
        *,
        timeout_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._transport = transport

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, transport=self._transport, timeout=timeout)

    async def health(self) -> ProviderHealth:
        t0 = time.perf_counter()
        try:
            async with self._client(3.0) as c:
                r = await c.get("/voices")
                r.raise_for_status()
                voices = list(r.json().keys())
        except (httpx.HTTPError, ValueError) as exc:
            return ProviderHealth(
                self.name,
                "unavailable",
                f"Piper server not reachable at {self.base_url} ({exc.__class__.__name__}). Run ./scripts/start.sh.",
            )
        return ProviderHealth(
            self.name,
            "ok" if voices else "degraded",
            f"{len(voices)} voice(s) installed",
            latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            extra={"voices": voices},
        )

    async def list_voices(self) -> list[VoiceInfo]:
        try:
            async with self._client(5.0) as c:
                r = await c.get("/voices")
                r.raise_for_status()
                data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TTSError("Piper server unavailable", code="tts_unavailable", retryable=True) from exc
        voices = []
        for vid, cfg in data.items():
            speaker_map = cfg.get("speaker_id_map") or {}
            voices.append(
                VoiceInfo(
                    id=vid,
                    name=vid,
                    language=(cfg.get("language") or {}).get("code") or (cfg.get("espeak") or {}).get("voice"),
                    num_speakers=int(cfg.get("num_speakers") or 1),
                    speakers=list(speaker_map.keys())[:50],
                )
            )
        return voices

    async def synthesize(self, text: str, voice: VoiceSettings) -> TTSResult:
        text = text.strip()
        if not text:
            raise TTSError("empty text", code="tts_bad_request")
        body: dict[str, object] = {"text": text, "voice": voice.voice}
        if voice.speaker is not None and str(voice.speaker).isdigit():
            body["speaker_id"] = int(voice.speaker)
        elif voice.speaker:
            body["speaker"] = str(voice.speaker)
        if voice.speed and voice.speed > 0:
            body["length_scale"] = round(1.0 / voice.speed, 3)
        t0 = time.perf_counter()
        try:
            async with self._client(self.timeout_s) as c:
                r = await c.post("/synthesize", json=body)
        except httpx.TimeoutException as exc:
            raise TTSError("Piper timed out", code="tts_timeout", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise TTSError(
                f"Piper server unavailable at {self.base_url}", code="tts_unavailable", retryable=True
            ) from exc
        if r.status_code != 200:
            raise TTSError(f"Piper HTTP {r.status_code}", code="tts_error")
        try:
            pcm, rate = read_wav(r.content)
        except Exception as exc:
            raise TTSError("Piper returned invalid audio", code="tts_bad_response") from exc
        return TTSResult(pcm=pcm, sample_rate=rate, latency_ms=round((time.perf_counter() - t0) * 1000, 1))
