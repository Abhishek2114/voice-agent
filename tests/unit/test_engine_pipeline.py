"""Voice pipeline: turns, barge-in, silence/max-duration handling, failures, cleanup."""

from __future__ import annotations

import asyncio
import contextlib

import numpy as np

from engine.audio.pcm import silence, tone
from engine.audio.vad import EnergyVAD, SegmenterConfig, SpeechSegmenter
from engine.conversation.pipeline import PipelineConfig, VoicePipeline
from engine.conversation.prompt import AgentPersona
from engine.conversation.session import ConversationSession, SessionConfig
from engine.conversation.tools import default_registry
from engine.providers.llm.base import LLMError, LLMOptions
from engine.providers.llm.scripted import ScriptedLLM
from engine.providers.stt.fake import FakeSTT
from engine.providers.tts.base import VoiceSettings
from engine.providers.tts.fake import FakeTTS
from engine.streaming.sink import QueueAudioSink

RATE = 16000


class Harness:
    def __init__(self, llm, stt, tts=None, *, greeting="Hello!", speedup=10.0, **pcfg) -> None:
        self.sink = QueueAudioSink(RATE)
        self.events: list[tuple[str, dict]] = []
        cfg = SessionConfig(persona=AgentPersona(name="A", business_name="B"), llm_options=LLMOptions(model="s"),
                            greeting=greeting)  # fmt: skip
        self.session = ConversationSession(cfg, llm, default_registry())
        self.tts = tts or FakeTTS(sample_rate=RATE, seconds_per_char=0.01)
        seg = SpeechSegmenter(EnergyVAD(), SegmenterConfig(end_silence_ms=300, min_speech_ms=100))
        pc = PipelineConfig(**{"barge_in_min_ms": 150, "silence_timeout_s": 30, **pcfg})
        self.pipe = VoicePipeline(self.session, stt, self.tts, seg, self.sink, VoiceSettings(voice="fake"),
                                  self.on_event, pc)  # fmt: skip
        self.speedup = speedup
        self._drain: asyncio.Task | None = None

    def on_event(self, kind, data):
        self.events.append((kind, data))

    async def start(self):
        self._drain = asyncio.create_task(self._drain_loop())
        await self.pipe.start()

    async def _drain_loop(self):
        while True:  # consume the sink faster than real time
            self.sink.read(320)
            await asyncio.sleep(0.02 / self.speedup)

    async def speak(self, seconds=0.5, trailing=0.5):
        audio = np.concatenate([tone(200, seconds, RATE), silence(trailing, RATE)])
        for i in range(0, audio.size, 320):
            self.pipe.push_audio(audio[i : i + 320])
            await asyncio.sleep(0.02 / self.speedup)

    async def wait_for(self, kind, pred=lambda d: True, timeout=5.0):
        async def poll():
            while not any(k == kind and pred(d) for k, d in self.events):
                await asyncio.sleep(0.01)
        await asyncio.wait_for(poll(), timeout)

    async def close(self):
        await self.pipe.stop("test_done")
        if self._drain:
            self._drain.cancel()
            with contextlib.suppress(BaseException):
                await self._drain

    def kinds(self):
        return [k for k, _ in self.events]


async def test_full_turn():
    h = Harness(ScriptedLLM(lambda m: "We host websites. How can I help?"), FakeSTT(["What do you do?"]))
    await h.start()
    await h.wait_for("transcript", lambda d: d["role"] == "assistant" and d["text"] == "Hello!")
    await h.speak()
    await h.wait_for("transcript", lambda d: d["role"] == "assistant" and "host websites" in d["text"])
    user = [d for k, d in h.events if k == "transcript" and d["role"] == "user"]
    assert user[0]["text"] == "What do you do?"
    assert h.pipe.turns[0].first_audio_ms is not None
    assert h.tts.spoken == ["Hello!", "We host websites.", "How can I help?"]
    await h.close()
    assert h.pipe.ended.is_set() and h.pipe.end_reason == "test_done"


async def test_barge_in_interrupts_and_records_partial():
    long = "This is the first sentence of a long answer. " + " ".join(f"Sentence number {i} keeps going." for i in range(30))
    h = Harness(ScriptedLLM(lambda m: long), FakeSTT(["tell me everything", "stop"]),
                FakeTTS(sample_rate=RATE, seconds_per_char=0.05), greeting="", speedup=1.0)  # fmt: skip
    await h.start()
    await h.speak(0.4, 0.4)
    await h.wait_for("state", lambda d: d["state"] == "speaking")
    await asyncio.sleep(0.3)
    await h.speak(0.5, 0.1)  # user talks over the agent
    await h.wait_for("interrupted")
    ev = next(d for k, d in h.events if k == "interrupted")
    assert ev["source"] == "barge_in"
    assert ev["dropped_ms"] > 0
    assistant = [e for e in h.session.transcript if e.role == "assistant"]
    assert assistant and assistant[0].interrupted
    assert len(assistant[0].text) < len(long)
    await h.close()


async def test_manual_interrupt_button():
    h = Harness(ScriptedLLM(lambda m: "x"), FakeSTT([]), FakeTTS(sample_rate=RATE, seconds_per_char=0.2),
                greeting="A long greeting that keeps talking for a while.", speedup=1.0)  # fmt: skip
    await h.start()
    await h.wait_for("state", lambda d: d["state"] == "speaking")
    await h.pipe.interrupt("user")
    assert not h.sink.playing
    assert h.pipe.state == "listening"
    await h.close()


async def test_silence_reprompt_then_end():
    h = Harness(ScriptedLLM(lambda m: "x"), FakeSTT([]), greeting="Hi", silence_timeout_s=0.3)
    await h.start()
    await h.wait_for("ended", timeout=5)
    texts = [d["text"] for k, d in h.events if k == "transcript"]
    assert texts == ["Hi", "Are you still there?", "Thank you for your time. Goodbye!"]
    assert h.pipe.end_reason == "silence_timeout"
    await h.close()


async def test_max_duration_ends_call():
    h = Harness(ScriptedLLM(lambda m: "x"), FakeSTT([]), greeting="", max_duration_s=0.3)
    await h.start()
    await h.wait_for("ended", timeout=5)
    assert h.pipe.end_reason == "max_duration"
    await h.close()


async def test_llm_failure_speaks_fallback_and_ends():
    h = Harness(ScriptedLLM(fail_with=LLMError("down", code="llm_unavailable")), FakeSTT(["hi"]), greeting="")
    await h.start()
    await h.speak()
    await h.wait_for("ended", timeout=5)
    err = next(d for k, d in h.events if k == "error")
    assert err["code"] == "llm_unavailable" and err["recoverable"] is False
    assert h.pipe.end_reason == "provider_error"
    assert "technical problem" in h.tts.spoken[-1]
    await h.close()


async def test_stt_failure_is_recoverable():
    h = Harness(ScriptedLLM(lambda m: "ok"), FakeSTT(fail=True), greeting="")
    await h.start()
    await h.speak()
    await h.wait_for("error")
    assert not h.pipe.ended.is_set()
    await h.close()


async def test_agent_end_call_stops_pipeline():
    h = Harness(ScriptedLLM(lambda m: "Okay, goodbye!"), FakeSTT(["that's all, bye"]), greeting="")
    await h.start()
    await h.speak()
    await h.wait_for("ended", timeout=5)
    assert h.pipe.end_reason == "user_goodbye"
    await h.close()


async def test_muted_audio_is_ignored():
    stt = FakeSTT(["should not be heard"])
    h = Harness(ScriptedLLM(lambda m: "x"), stt, greeting="")
    await h.start()
    h.pipe.set_muted(True)
    await h.speak()
    await asyncio.sleep(0.2)
    assert stt.received == []
    await h.close()


async def test_stop_is_idempotent_and_cleans_tasks():
    h = Harness(ScriptedLLM(lambda m: "x"), FakeSTT([]))
    await h.start()
    await h.pipe.stop("a")
    await h.pipe.stop("b")
    assert h.pipe.end_reason == "a"
    assert all(t.done() for t in h.pipe._tasks)
    assert h.kinds().count("ended") == 1
    await h.close()


async def test_ai_slot_backpressure():
    slot = asyncio.Semaphore(1)
    await slot.acquire()  # another conversation holds the only inference slot
    h = Harness(ScriptedLLM(lambda m: "Done."), FakeSTT(["hi"]), greeting="")
    h.pipe.ai_slot = slot
    await h.start()
    await h.speak()
    await h.wait_for("state", lambda d: d["state"] == "waiting")
    slot.release()
    await h.wait_for("transcript", lambda d: d["role"] == "assistant")
    await h.close()


async def test_interrupted_greeting_still_reaches_transcript():
    h = Harness(ScriptedLLM(lambda m: "ok"), FakeSTT(["hi"]), FakeTTS(sample_rate=RATE, seconds_per_char=0.2),
                greeting="A long greeting that keeps on talking for quite a while.", speedup=1.0)  # fmt: skip
    await h.start()
    await h.wait_for("state", lambda d: d["state"] == "speaking")
    await asyncio.sleep(0.3)
    await h.speak(0.5, 0.1)
    await h.wait_for("interrupted")
    await h.wait_for("transcript", lambda d: d["role"] == "assistant" and d.get("interrupted"))
    await h.close()
