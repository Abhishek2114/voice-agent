"""macOS built-in speech synthesis (``/usr/bin/say``) — local, zero-download alternative.

Voices are provided by the operating system; use is subject to Apple's macOS licence
(local use on the Mac). Text is passed on stdin, never on the command line.
"""

from __future__ import annotations

import asyncio
import os
import platform
import re
import shutil
import tempfile
import time
from pathlib import Path

from engine.audio.pcm import read_wav
from engine.providers.base import ProviderHealth
from engine.providers.tts.base import TTSError, TTSResult, VoiceInfo, VoiceSettings

_VOICE_LINE = re.compile(r"^(?P<name>.+?)\s{2,}(?P<lang>[a-z]{2}_[A-Z]{2})\s+#")
_SAFE_VOICE = re.compile(r"^[\w .()'-]{1,64}$")


class MacSayTTS:
    name = "macos_say"
    streaming = False

    def __init__(self, timeout_s: float = 30.0) -> None:
        self.timeout_s = timeout_s

    def _available(self) -> bool:
        return platform.system() == "Darwin" and shutil.which("say") is not None

    async def health(self) -> ProviderHealth:
        if not self._available():
            return ProviderHealth(self.name, "unavailable", "macOS `say` not available on this system")
        return ProviderHealth(self.name, "ok", "macOS speech synthesis")

    async def list_voices(self) -> list[VoiceInfo]:
        if not self._available():
            return []
        proc = await asyncio.create_subprocess_exec(
            "say", "-v", "?", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        voices = []
        for line in out.decode(errors="replace").splitlines():
            m = _VOICE_LINE.match(line)
            if m and m.group("lang").startswith(("en_", "hi_")):
                voices.append(VoiceInfo(id=m.group("name").strip(), name=m.group("name").strip(), language=m.group("lang")))
        return voices

    async def synthesize(self, text: str, voice: VoiceSettings) -> TTSResult:
        if not self._available():
            raise TTSError("macOS `say` unavailable", code="tts_unavailable")
        text = text.strip()
        if not text:
            raise TTSError("empty text", code="tts_bad_request")
        if voice.voice and not _SAFE_VOICE.match(voice.voice):
            raise TTSError("invalid voice name", code="tts_bad_request")
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        args = ["say", "-o", path, "--file-format=WAVE", "--data-format=LEI16@22050", "-f", "-"]
        if voice.voice:
            args[1:1] = ["-v", voice.voice]
        if voice.speed and voice.speed != 1.0:
            args[1:1] = ["-r", str(int(175 * voice.speed))]
        t0 = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
            try:
                _, err = await asyncio.wait_for(proc.communicate(text.encode()), timeout=self.timeout_s)
            except TimeoutError as exc:
                proc.kill()
                raise TTSError("say timed out", code="tts_timeout") from exc
            if proc.returncode != 0:
                raise TTSError(f"say failed: {err.decode(errors='replace')[:200]}", code="tts_error")
            pcm, rate = read_wav(Path(path).read_bytes())
        finally:
            Path(path).unlink(missing_ok=True)
        return TTSResult(pcm=pcm, sample_rate=rate, latency_ms=round((time.perf_counter() - t0) * 1000, 1))
