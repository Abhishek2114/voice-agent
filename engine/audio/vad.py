"""Voice activity detection and turn segmentation.

* :class:`SileroVAD` runs the Silero VAD v5 ONNX model through onnxruntime (no torch).
* :class:`EnergyVAD` is a dependency-free RMS detector used in CI and as an explicit,
  user-selectable fallback (never silently substituted).
* :class:`SpeechSegmenter` turns per-frame probabilities into speech-start /
  speech-end events with pre-roll, minimum speech duration and end-of-turn silence.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import numpy as np

from engine.audio.pcm import STT_SAMPLE_RATE, ensure_int16, pcm16_to_float, rms_dbfs

FRAME_SAMPLES = 512  # 32 ms @ 16 kHz — the frame size Silero v5 expects
FRAME_MS = FRAME_SAMPLES * 1000 // STT_SAMPLE_RATE


class VAD(Protocol):
    name: str

    def probability(self, frame: np.ndarray) -> float: ...

    def reset(self) -> None: ...


class VADUnavailableError(RuntimeError):
    pass


class SileroVAD:
    """Silero VAD v5 (MIT) through onnxruntime. Stateful: one instance per audio stream."""

    name = "silero"
    _CONTEXT = 64

    def __init__(self, model_path: str | Path) -> None:
        path = Path(model_path)
        if not path.is_file():
            raise VADUnavailableError(f"Silero VAD model not found at {path}")
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 3
        self._session = ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])
        self._sr = np.array(STT_SAMPLE_RATE, dtype=np.int64)
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, self._CONTEXT), dtype=np.float32)

    def probability(self, frame: np.ndarray) -> float:
        if frame.size != FRAME_SAMPLES:
            raise ValueError(f"Silero VAD expects {FRAME_SAMPLES}-sample frames at 16 kHz")
        x = pcm16_to_float(ensure_int16(frame)).reshape(1, -1)
        x = np.concatenate([self._context, x], axis=1)
        out, self._state = self._session.run(None, {"input": x, "state": self._state, "sr": self._sr})
        self._context = x[:, -self._CONTEXT :]
        return float(out[0][0])


class EnergyVAD:
    """Simple RMS energy detector. Maps dBFS into a pseudo-probability."""

    name = "energy"

    def __init__(self, threshold_dbfs: float = -42.0, knee_db: float = 6.0) -> None:
        self.threshold = threshold_dbfs
        self.knee = knee_db

    def reset(self) -> None:
        return None

    def probability(self, frame: np.ndarray) -> float:
        level = rms_dbfs(frame)
        z = (level - self.threshold) / self.knee
        return float(1.0 / (1.0 + np.exp(-z)))


class SegmentEventType(StrEnum):
    SPEECH_START = "speech_start"
    SPEECH_END = "speech_end"


@dataclass
class SegmentEvent:
    type: SegmentEventType
    audio: np.ndarray | None = None  # set on SPEECH_END
    duration_ms: int = 0


@dataclass
class SegmenterConfig:
    threshold: float = 0.5
    neg_threshold: float = 0.35
    min_speech_ms: int = 160
    end_silence_ms: int = 700
    pre_roll_ms: int = 300
    max_utterance_ms: int = 30_000


@dataclass
class SpeechSegmenter:
    vad: VAD
    config: SegmenterConfig = field(default_factory=SegmenterConfig)

    def __post_init__(self) -> None:
        self._pending = np.zeros(0, dtype=np.int16)
        pre_frames = max(1, self.config.pre_roll_ms // FRAME_MS)
        self._pre_roll: collections.deque[np.ndarray] = collections.deque(maxlen=pre_frames)
        self._candidate: list[np.ndarray] = []
        self._utterance: list[np.ndarray] = []
        self._speech_run = 0
        self._silence_run = 0
        self.in_speech = False
        self.last_probability = 0.0

    def reset(self) -> None:
        self.vad.reset()
        self.__post_init__()

    def feed(self, samples: np.ndarray) -> list[SegmentEvent]:
        """Feed 16 kHz mono PCM of any length; returns events produced."""
        self._pending = np.concatenate([self._pending, ensure_int16(samples)])
        events: list[SegmentEvent] = []
        while self._pending.size >= FRAME_SAMPLES:
            frame = self._pending[:FRAME_SAMPLES]
            self._pending = self._pending[FRAME_SAMPLES:]
            ev = self._process_frame(frame)
            if ev is not None:
                events.append(ev)
        return events

    def _process_frame(self, frame: np.ndarray) -> SegmentEvent | None:
        cfg = self.config
        p = self.vad.probability(frame)
        self.last_probability = p
        if not self.in_speech:
            if p >= cfg.threshold:
                self._speech_run += 1
                self._candidate.append(frame)
                if self._speech_run * FRAME_MS >= cfg.min_speech_ms:
                    self.in_speech = True
                    self._utterance = [*self._pre_roll, *self._candidate]
                    self._candidate = []
                    self._pre_roll.clear()
                    self._silence_run = 0
                    return SegmentEvent(SegmentEventType.SPEECH_START)
            else:
                for f in self._candidate:
                    self._pre_roll.append(f)
                self._candidate = []
                self._speech_run = 0
                self._pre_roll.append(frame)
            return None

        self._utterance.append(frame)
        self._silence_run = self._silence_run + 1 if p < cfg.neg_threshold else 0
        too_long = len(self._utterance) * FRAME_MS >= cfg.max_utterance_ms
        if self._silence_run * FRAME_MS >= cfg.end_silence_ms or too_long:
            audio = np.concatenate(self._utterance)
            self.in_speech = False
            self._utterance = []
            self._speech_run = 0
            self._silence_run = 0
            return SegmentEvent(
                SegmentEventType.SPEECH_END, audio=audio, duration_ms=audio.size * 1000 // STT_SAMPLE_RATE
            )
        return None


def create_vad(kind: str, model_path: str | Path | None = None) -> VAD:
    if kind == "silero":
        if model_path is None:
            raise VADUnavailableError("Silero VAD requires a model path")
        return SileroVAD(model_path)
    if kind == "energy":
        return EnergyVAD()
    raise VADUnavailableError(f"unknown VAD kind: {kind}")
