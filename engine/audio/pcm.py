"""PCM helpers: conversion, resampling and WAV encoding.

All engine-internal audio is mono signed 16-bit PCM held in ``numpy.int16`` arrays.
"""

from __future__ import annotations

import io
import wave

import numpy as np

STT_SAMPLE_RATE = 16_000


def ensure_int16(samples: np.ndarray) -> np.ndarray:
    if samples.dtype == np.int16:
        return samples
    if np.issubdtype(samples.dtype, np.floating):
        return float_to_pcm16(samples)
    return samples.astype(np.int16)


def float_to_pcm16(samples: np.ndarray) -> np.ndarray:
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16)


def pcm16_to_float(samples: np.ndarray) -> np.ndarray:
    return samples.astype(np.float32) / 32768.0


def to_mono(samples: np.ndarray, channels: int) -> np.ndarray:
    """Down-mix interleaved PCM to mono."""
    if channels <= 1:
        return samples
    frames = samples.reshape(-1, channels).astype(np.int32)
    return frames.mean(axis=1).astype(np.int16)


def resample(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """One-shot resample of a complete mono int16 buffer.

    Uses libswresample through PyAV when available (high quality), otherwise a
    linear-interpolation fallback that is adequate for speech.
    """
    samples = ensure_int16(samples)
    if src_rate == dst_rate or samples.size == 0:
        return samples
    try:
        resampler = StreamResampler(src_rate, dst_rate)
        out = resampler.process(samples)
        tail = resampler.flush()
        return np.concatenate([out, tail]) if tail.size else out
    except Exception:  # noqa: BLE001 - any PyAV failure falls back to numpy resampling  # pragma: no cover
        return resample_linear(samples, src_rate, dst_rate)


def resample_linear(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate or samples.size == 0:
        return ensure_int16(samples)
    duration = samples.size / src_rate
    n_out = max(1, round(duration * dst_rate))
    x_old = np.linspace(0.0, duration, num=samples.size, endpoint=False)
    x_new = np.linspace(0.0, duration, num=n_out, endpoint=False)
    return np.interp(x_new, x_old, samples.astype(np.float32)).astype(np.int16)


class StreamResampler:
    """Stateful resampler for continuous streams (keeps filter history between chunks)."""

    def __init__(self, src_rate: int, dst_rate: int) -> None:
        import av  # local import keeps module importable without PyAV for pure-numpy users

        self.src_rate = src_rate
        self.dst_rate = dst_rate
        self._passthrough = src_rate == dst_rate
        self._resampler = None if self._passthrough else av.AudioResampler(
            format="s16", layout="mono", rate=dst_rate
        )
        self._pts = 0

    def process(self, samples: np.ndarray) -> np.ndarray:
        samples = ensure_int16(samples)
        if self._passthrough or samples.size == 0:
            return samples
        import av

        frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = self.src_rate
        frame.pts = self._pts
        self._pts += samples.size
        assert self._resampler is not None
        return self._collect(self._resampler.resample(frame))

    def flush(self) -> np.ndarray:
        if self._passthrough or self._resampler is None:
            return np.zeros(0, dtype=np.int16)
        return self._collect(self._resampler.resample(None))

    @staticmethod
    def _collect(frames: list) -> np.ndarray:
        if not frames:
            return np.zeros(0, dtype=np.int16)
        return np.concatenate([f.to_ndarray().reshape(-1) for f in frames]).astype(np.int16)


def wav_bytes(samples: np.ndarray, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(ensure_int16(samples).tobytes())
    return buf.getvalue()


def read_wav(data: bytes) -> tuple[np.ndarray, int]:
    """Decode a PCM WAV file into mono int16 samples."""
    with wave.open(io.BytesIO(data), "rb") as wf:
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    if width != 2:
        raise ValueError(f"unsupported WAV sample width: {width * 8} bit")
    samples = np.frombuffer(raw, dtype="<i2").astype(np.int16)
    return to_mono(samples, channels), rate


def rms_dbfs(samples: np.ndarray) -> float:
    if samples.size == 0:
        return -120.0
    f = pcm16_to_float(ensure_int16(samples))
    rms = float(np.sqrt(np.mean(np.square(f))))
    return 20.0 * np.log10(max(rms, 1e-6))


def duration_seconds(samples: np.ndarray, sample_rate: int) -> float:
    return samples.size / float(sample_rate) if sample_rate else 0.0


def silence(seconds: float, sample_rate: int) -> np.ndarray:
    return np.zeros(int(seconds * sample_rate), dtype=np.int16)


def tone(freq: float, seconds: float, sample_rate: int, amplitude: float = 0.3) -> np.ndarray:
    t = np.arange(int(seconds * sample_rate)) / sample_rate
    return float_to_pcm16(amplitude * np.sin(2 * np.pi * freq * t))
