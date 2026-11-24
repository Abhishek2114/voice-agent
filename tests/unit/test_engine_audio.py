"""Audio primitives, VAD and turn segmentation."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from engine.audio.pcm import read_wav, resample, rms_dbfs, silence, tone, wav_bytes
from engine.audio.recorder import CallRecorder
from engine.audio.vad import (
    EnergyVAD,
    SegmenterConfig,
    SegmentEventType,
    SileroVAD,
    SpeechSegmenter,
)
from engine.streaming.sink import QueueAudioSink

ROOT = Path(__file__).resolve().parents[2]
SILERO = ROOT / "data/models/vad/silero_vad.onnx"


def test_wav_roundtrip():
    pcm = tone(440, 0.25, 16000)
    out, rate = read_wav(wav_bytes(pcm, 16000))
    assert rate == 16000
    assert np.array_equal(out, pcm)


def test_resample_lengths():
    pcm = tone(300, 1.0, 22050)
    out = resample(pcm, 22050, 16000)
    assert abs(out.size - 16000) <= 64
    assert resample(pcm, 22050, 22050) is pcm


def test_rms_levels():
    assert rms_dbfs(silence(0.1, 16000)) < -100
    assert rms_dbfs(tone(440, 0.1, 16000, amplitude=0.5)) > -12


def _segmenter(**kw) -> SpeechSegmenter:
    return SpeechSegmenter(EnergyVAD(), SegmenterConfig(**kw))


def test_segmenter_detects_one_utterance():
    seg = _segmenter(end_silence_ms=500)
    audio = np.concatenate([silence(0.5, 16000), tone(200, 0.8, 16000), silence(1.0, 16000)])
    events = []
    for i in range(0, audio.size, 480):  # 30 ms chunks, like a transport would deliver
        events += seg.feed(audio[i : i + 480])
    kinds = [e.type for e in events]
    assert kinds == [SegmentEventType.SPEECH_START, SegmentEventType.SPEECH_END]
    end = events[-1]
    assert end.audio is not None
    # utterance includes pre-roll + speech + trailing silence, but not the whole input
    assert 0.8 <= end.audio.size / 16000 <= 1.7


def test_segmenter_ignores_short_clicks():
    seg = _segmenter(min_speech_ms=200)
    audio = np.concatenate([silence(0.3, 16000), tone(200, 0.06, 16000), silence(1.0, 16000)])
    assert seg.feed(audio) == []


def test_segmenter_caps_utterance_length():
    seg = _segmenter(max_utterance_ms=1000, end_silence_ms=5000)
    events = seg.feed(tone(200, 3.0, 16000))
    assert SegmentEventType.SPEECH_END in [e.type for e in events]


@pytest.mark.skipif(not SILERO.exists(), reason="Silero model not downloaded")
def test_silero_vad_rejects_silence_and_tone():
    vad = SileroVAD(SILERO)
    probs = [vad.probability(f) for f in silence(1.0, 16000)[: 512 * 30].reshape(-1, 512)]
    assert max(probs) < 0.3


@pytest.mark.speech
@pytest.mark.skipif(not (SILERO.exists() and shutil.which("say")), reason="needs Silero model and macOS say")
def test_silero_vad_detects_real_speech(tmp_path):
    wav = tmp_path / "s.wav"
    subprocess.run(["say", "-o", str(wav), "--file-format=WAVE", "--data-format=LEI16@16000",
                    "Hello, I would like to know more about your hosting plans."], check=True)  # fmt: skip
    pcm, rate = read_wav(wav.read_bytes())
    pcm = resample(pcm, rate, 16000)
    seg = SpeechSegmenter(SileroVAD(SILERO))
    events = seg.feed(np.concatenate([silence(0.5, 16000), pcm, silence(1.5, 16000)]))
    assert [e.type for e in events] == [SegmentEventType.SPEECH_START, SegmentEventType.SPEECH_END]


def test_sink_markers_and_clear():
    sink = QueueAudioSink(16000)
    sink.enqueue(tone(300, 0.1, 16000), 16000, marker=1)
    sink.enqueue(tone(300, 0.1, 16000), 16000, marker=2)
    assert sink.playing
    out = sink.read(800)
    assert out.size == 800 and 1 in sink.started_markers and 2 not in sink.started_markers
    dropped = sink.clear()
    assert dropped == 3200 - 800
    assert not sink.playing
    assert np.all(sink.read(160) == 0)


def test_recorder_writes_private_stereo_wav(tmp_path):
    rec = CallRecorder()
    rec.add_user(tone(200, 0.5, 16000))
    rec.add_agent(tone(400, 0.5, 48000), 48000)
    path = tmp_path / "r" / "call.wav"
    size = rec.write(path)
    assert size > 0
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    import wave

    with wave.open(str(path)) as wf:
        assert wf.getnchannels() == 2 and wf.getframerate() == 16000
