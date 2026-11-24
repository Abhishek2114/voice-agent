"""whisper.cpp adapters (MIT, https://github.com/ggml-org/whisper.cpp).

* :class:`WhisperServerSTT` talks to a warm ``whisper-server`` on localhost
  (``POST /inference``). This is the default: the model stays loaded on the Metal GPU.
* :class:`WhisperCliSTT` runs ``whisper-cli`` per utterance (slower — reloads the
  model every time) and needs no extra service.

Both are *non-streaming*: transcription starts after the end-of-turn is detected.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
from pathlib import Path

import httpx
import numpy as np

from engine.audio.pcm import STT_SAMPLE_RATE, duration_seconds, resample, wav_bytes
from engine.providers.base import ProviderHealth
from engine.providers.stt.base import STTError, STTResult, clean_transcript


class WhisperServerSTT:
    name = "whisper_cpp_server"
    streaming = False

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8178",
        *,
        timeout_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._transport = transport

    async def health(self) -> ProviderHealth:
        t0 = time.perf_counter()
        try:
            async with httpx.AsyncClient(base_url=self.base_url, transport=self._transport, timeout=3.0) as c:
                r = await c.get("/health")
            if r.status_code == 200:
                return ProviderHealth(
                    self.name, "ok", "whisper-server ready", latency_ms=round((time.perf_counter() - t0) * 1000, 1)
                )
            return ProviderHealth(self.name, "degraded", f"whisper-server responded {r.status_code} (model loading?)")
        except httpx.HTTPError as exc:
            return ProviderHealth(
                self.name,
                "unavailable",
                f"whisper-server not reachable at {self.base_url} ({exc.__class__.__name__}). "
                "Run ./scripts/start.sh or start whisper-server manually.",
            )

    async def transcribe(self, pcm16: np.ndarray, sample_rate: int = 16_000, language: str = "en") -> STTResult:
        audio = resample(pcm16, sample_rate, STT_SAMPLE_RATE)
        seconds = duration_seconds(audio, STT_SAMPLE_RATE)
        data = {"response_format": "json", "temperature": "0.0", "language": language or "en"}
        files = {"file": ("utterance.wav", wav_bytes(audio, STT_SAMPLE_RATE), "audio/wav")}
        t0 = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url, transport=self._transport, timeout=self.timeout_s
            ) as c:
                r = await c.post("/inference", data=data, files=files)
        except httpx.TimeoutException as exc:
            raise STTError("whisper-server timed out", code="stt_timeout", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise STTError(
                f"whisper-server unavailable at {self.base_url}", code="stt_unavailable", retryable=True
            ) from exc
        if r.status_code != 200:
            raise STTError(f"whisper-server HTTP {r.status_code}: {r.text[:200]}", code="stt_error")
        try:
            text = str(r.json().get("text", ""))
        except ValueError as exc:
            raise STTError("whisper-server returned invalid JSON", code="stt_bad_response") from exc
        return STTResult(
            text=clean_transcript(text, seconds),
            language=language,
            audio_seconds=seconds,
            latency_ms=round((time.perf_counter() - t0) * 1000, 1),
        )


class WhisperCliSTT:
    name = "whisper_cpp_cli"
    streaming = False

    def __init__(self, model_path: str | Path, *, binary: str = "whisper-cli", timeout_s: float = 60.0) -> None:
        self.model_path = Path(model_path)
        self.binary = binary
        self.timeout_s = timeout_s

    async def health(self) -> ProviderHealth:
        exe = shutil.which(self.binary)
        if not exe:
            return ProviderHealth(self.name, "unavailable", f"{self.binary} not found on PATH (brew install whisper-cpp)")
        if not self.model_path.is_file():
            return ProviderHealth(self.name, "unavailable", f"model not found: {self.model_path.name}")
        return ProviderHealth(self.name, "ok", f"{self.binary} with {self.model_path.name}")

    async def transcribe(self, pcm16: np.ndarray, sample_rate: int = 16_000, language: str = "en") -> STTResult:
        exe = shutil.which(self.binary)
        if not exe or not self.model_path.is_file():
            raise STTError("whisper-cli or model missing", code="stt_unavailable")
        audio = resample(pcm16, sample_rate, STT_SAMPLE_RATE)
        seconds = duration_seconds(audio, STT_SAMPLE_RATE)
        fd, path = tempfile.mkstemp(suffix=".wav")
        t0 = time.perf_counter()
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(wav_bytes(audio, STT_SAMPLE_RATE))
            proc = await asyncio.create_subprocess_exec(
                exe, "-m", str(self.model_path), "-f", path, "-nt", "-np", "-l", language or "en",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )  # fmt: skip
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_s)
            except TimeoutError as exc:
                proc.kill()
                raise STTError("whisper-cli timed out", code="stt_timeout", retryable=True) from exc
            if proc.returncode != 0:
                raise STTError(f"whisper-cli exited with {proc.returncode}", code="stt_error")
        finally:
            Path(path).unlink(missing_ok=True)
        return STTResult(
            text=clean_transcript(out.decode(errors="replace"), seconds),
            language=language,
            audio_seconds=seconds,
            latency_ms=round((time.perf_counter() - t0) * 1000, 1),
        )
