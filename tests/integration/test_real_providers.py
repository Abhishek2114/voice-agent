"""Real local providers: Ollama + whisper.cpp + Piper + Silero VAD.

Skipped automatically when the services are not running (e.g. in CI). Run locally after
``./scripts/start.sh`` with ``make test-real``.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import httpx
import numpy as np
import pytest

from engine.audio.pcm import resample, silence
from engine.audio.vad import SileroVAD, SpeechSegmenter
from engine.conversation.pipeline import PipelineConfig, VoicePipeline
from engine.conversation.prompt import AgentPersona
from engine.conversation.session import ConversationSession, SessionConfig
from engine.conversation.tools import default_registry
from engine.providers.llm.base import ChatMessage, LLMOptions
from engine.providers.llm.ollama import OllamaProvider
from engine.providers.stt.whisper_cpp import WhisperServerSTT
from engine.providers.tts.base import VoiceSettings
from engine.providers.tts.piper_http import PiperHttpTTS
from engine.streaming.sink import QueueAudioSink

ROOT = Path(__file__).resolve().parents[2]
MODEL = os.environ.get("VA_DEFAULT_LLM_MODEL", "qwen3:4b-instruct-2507-q4_K_M")
OLLAMA = "http://127.0.0.1:11434"
WHISPER = "http://127.0.0.1:8178"
PIPER = "http://127.0.0.1:5005"
VOICE = VoiceSettings(voice="en_GB-alba-medium")


def _up(url: str) -> bool:
    try:
        return httpx.get(url, timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


services = pytest.mark.skipif(
    not (_up(f"{OLLAMA}/api/version") and _up(f"{WHISPER}/health") and _up(f"{PIPER}/voices")),
    reason="local Ollama / whisper-server / Piper not running",
)
pytestmark = [pytest.mark.ollama, pytest.mark.speech, services]


async def test_ollama_lists_models_and_flags_cloud():
    models = await OllamaProvider(OLLAMA).list_models(force=True)
    names = {m.name: m for m in models}
    assert MODEL in names and names[MODEL].is_local
    for m in models:
        if m.name.endswith(":cloud"):
            assert not m.is_local


async def test_ollama_refuses_cloud_model():
    from engine.providers.llm.base import LLMError

    models = await OllamaProvider(OLLAMA).list_models(force=True)
    cloud = [m.name for m in models if not m.is_local]
    if not cloud:
        pytest.skip("no cloud model registered in this Ollama")
    with pytest.raises(LLMError) as e:
        async for _ in OllamaProvider(OLLAMA).stream_chat([ChatMessage("user", "hi")], LLMOptions(model=cloud[0])):
            pass
    assert e.value.code == "remote_model_refused"


async def test_tts_then_stt_roundtrip():
    text = "I would like to know about your hosting plans for a small business."
    tts = await PiperHttpTTS(PIPER).synthesize(text, VOICE)
    assert tts.sample_rate == 22050 and tts.seconds > 2
    stt = await WhisperServerSTT(WHISPER).transcribe(resample(tts.pcm, tts.sample_rate, 16000), 16000)
    words = set(stt.text.lower().replace(",", "").replace(".", "").split())
    assert {"hosting", "plans", "business"} <= words, stt.text


async def test_full_voice_pipeline_with_real_models():
    """Synthesised caller speech → Silero → whisper.cpp → Qwen (Ollama) → Piper → audio out."""
    tts = PiperHttpTTS(PIPER)
    caller = await tts.synthesize("Hello. Do you offer website hosting?", VOICE)
    mic = np.concatenate([silence(0.6, 16000), resample(caller.pcm, caller.sample_rate, 16000), silence(1.2, 16000)])
    persona = AgentPersona(name="Asha", business_name="Demo Hosting",
                           business_knowledge="Demo Hosting provides shared web hosting and managed VPS servers.")  # fmt: skip
    cfg = SessionConfig(persona=persona, llm_options=LLMOptions(model=MODEL, max_tokens=80), greeting="")
    session = ConversationSession(cfg, OllamaProvider(OLLAMA), default_registry())
    sink = QueueAudioSink(16000)
    events: list[tuple[str, dict]] = []
    pipe = VoicePipeline(session, WhisperServerSTT(WHISPER), tts, SpeechSegmenter(SileroVAD(ROOT / "data/models/vad/silero_vad.onnx")),
                         sink, VOICE, lambda k, d: events.append((k, d)), PipelineConfig(silence_timeout_s=60))  # fmt: skip
    await pipe.start()
    for i in range(0, mic.size, 320):  # real-time-ish 20 ms frames
        pipe.push_audio(mic[i : i + 320])
        await asyncio.sleep(0.004)
    t0 = time.monotonic()
    played = 0
    while time.monotonic() - t0 < 60:
        played += int(np.abs(sink.read(1600)).max() > 500)
        if any(k == "transcript" and d["role"] == "assistant" for k, d in events):
            break
        await asyncio.sleep(0.02)
    await pipe.stop("test")
    user = [d["text"] for k, d in events if k == "transcript" and d["role"] == "user"]
    agent = [d["text"] for k, d in events if k == "transcript" and d["role"] == "assistant"]
    assert user and "hosting" in user[0].lower(), events
    assert agent and len(agent[0]) > 10
    assert played > 0
    m = pipe.turns[0]
    print(f"\nreal pipeline: stt={m.stt_ms}ms ttft={m.llm_ttft_ms}ms tts_first={m.tts_first_ms}ms "
          f"first_audio={m.first_audio_ms}ms :: {agent[0]!r}")  # fmt: skip
    assert m.first_audio_ms is not None and m.first_audio_ms < 8000
