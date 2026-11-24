"""Call recorder: captures caller and agent audio as a 2-channel 16 kHz WAV
(left = caller, right = agent). Only used when recording is enabled and disclosed."""

from __future__ import annotations

import os
import wave
from pathlib import Path

import numpy as np

from engine.audio.pcm import StreamResampler, ensure_int16

RECORD_RATE = 16_000


class CallRecorder:
    def __init__(self, max_seconds: float = 3600.0) -> None:
        self.max_samples = int(max_seconds * RECORD_RATE)
        self._user: list[np.ndarray] = []
        self._agent: list[np.ndarray] = []
        self._user_n = 0
        self._agent_n = 0
        self._agent_resamplers: dict[int, StreamResampler] = {}
        self.enabled = True

    def add_user(self, pcm16_16k: np.ndarray) -> None:
        if self.enabled and self._user_n < self.max_samples:
            self._user.append(ensure_int16(pcm16_16k).copy())
            self._user_n += pcm16_16k.size

    def add_agent(self, pcm: np.ndarray, sample_rate: int) -> None:
        if not self.enabled or self._agent_n >= self.max_samples:
            return
        if sample_rate != RECORD_RATE:
            rs = self._agent_resamplers.setdefault(sample_rate, StreamResampler(sample_rate, RECORD_RATE))
            pcm = rs.process(pcm)
        self._agent.append(ensure_int16(pcm).copy())
        self._agent_n += pcm.size

    def duration_seconds(self) -> float:
        return max(self._user_n, self._agent_n) / RECORD_RATE

    def write(self, path: Path) -> int:
        """Write the recording with owner-only permissions; returns bytes written."""
        user = np.concatenate(self._user) if self._user else np.zeros(0, np.int16)
        agent = np.concatenate(self._agent) if self._agent else np.zeros(0, np.int16)
        n = max(user.size, agent.size)
        stereo = np.zeros((n, 2), dtype=np.int16)
        stereo[: user.size, 0] = user
        stereo[: agent.size, 1] = agent
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh, wave.open(fh, "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(RECORD_RATE)
            wf.writeframes(stereo.tobytes())
        return path.stat().st_size
