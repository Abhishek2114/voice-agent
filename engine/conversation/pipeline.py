"""Transport-independent real-time voice pipeline.

    transport → push_audio() → VAD segmenter → (end of turn) → STT → ConversationSession
    → LLM token stream → sentence splitter → TTS (per sentence) → QueueAudioSink → transport

Streaming behaviour, stated precisely:
* LLM output is streamed token-by-token.
* TTS is **not** streamed below sentence level: each sentence is synthesised in full,
  then queued. Sentence *n+1* is synthesised while sentence *n* plays, so the first
  audio starts after the first sentence is ready, not after the whole reply.
* STT runs once per user turn after end-of-speech is detected (not streaming).

Barge-in: when the user speaks for ``barge_in_min_ms`` while the agent is speaking,
queued audio is dropped and the in-flight LLM/TTS work is cancelled.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np

from engine.audio.vad import SegmenterConfig, SegmentEventType, SpeechSegmenter
from engine.conversation.prompt import is_hindi
from engine.conversation.session import ConversationSession
from engine.providers.base import ProviderError
from engine.providers.stt.base import STTProvider
from engine.providers.tts.base import TTSProvider, VoiceSettings
from engine.streaming.sink import QueueAudioSink

log = logging.getLogger(__name__)

EventCallback = Callable[[str, dict[str, Any]], Awaitable[None] | None]


@dataclass
class PipelineConfig:
    segmenter: SegmenterConfig = field(default_factory=SegmenterConfig)
    barge_in: bool = True
    barge_in_min_ms: int = 300
    max_duration_s: float = 600.0
    silence_timeout_s: float = 20.0
    language: str = "en"  # "auto" lets the speech model detect the caller's language per turn
    hindi_voice: str = ""  # voice for Devanagari sentences, so replies are spoken in the caller's language
    watchdog_interval_s: float = 0.2


@dataclass
class TurnMetrics:
    stt_ms: float | None = None
    llm_ttft_ms: float | None = None
    tts_first_ms: float | None = None
    first_audio_ms: float | None = None  # end of user speech → first agent audio queued
    llm_total_ms: float | None = None
    interrupted: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


class VoicePipeline:
    def __init__(
        self,
        session: ConversationSession,
        stt: STTProvider,
        tts: TTSProvider,
        segmenter: SpeechSegmenter,
        sink: QueueAudioSink,
        voice: VoiceSettings,
        on_event: EventCallback | None = None,
        config: PipelineConfig | None = None,
        ai_slot: asyncio.Semaphore | None = None,
    ) -> None:
        self.session = session
        self.stt = stt
        self.tts = tts
        self.segmenter = segmenter
        self.sink = sink
        self.voice = voice
        self.on_event = on_event
        self.config = config or PipelineConfig()
        self.ai_slot = ai_slot
        self.state = "idle"
        self.muted = False
        self.ended = asyncio.Event()
        self.end_reason: str | None = None
        self.error: ProviderError | None = None
        self.turns: list[TurnMetrics] = []
        self.interrupt_latencies_ms: list[float] = []
        self._utterances: asyncio.Queue[tuple[np.ndarray, float]] = asyncio.Queue()
        self._response: asyncio.Task[None] | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._marker = 0
        self._speech_started_at: float | None = None
        self._last_activity = time.monotonic()
        self._reprompted = False
        self._stopping = False
        self._started_at = time.monotonic()

    # ================================================================ lifecycle
    async def start(self) -> None:
        self._started_at = time.monotonic()
        self._tasks = [
            asyncio.create_task(self._turn_worker(), name=f"turns-{self.session.id}"),
            asyncio.create_task(self._watchdog(), name=f"watchdog-{self.session.id}"),
        ]
        greeting = self.session.greeting_text()
        if greeting:
            self._start_response(self._speak(greeting))
        else:
            await self._set_state("listening")

    async def stop(self, reason: str = "ended") -> None:
        if self.ended.is_set() or self._stopping:
            return
        self._stopping = True
        self.end_reason = reason
        current = asyncio.current_task()
        if self._response and self._response is not current and not self._response.done():
            self._response.cancel()
            with contextlib.suppress(BaseException):
                await self._response
        for t in self._tasks:
            if t is not current and not t.done():
                t.cancel()
        for t in self._tasks:
            if t is not current:
                with contextlib.suppress(BaseException):
                    await t
        self.sink.clear()
        self.state = "ended"
        self.ended.set()
        await self._emit("state", {"state": "ended"})
        await self._emit("ended", {"reason": reason})

    async def wait_ended(self, timeout: float | None = None) -> bool:
        try:
            await asyncio.wait_for(self.ended.wait(), timeout)
            return True
        except TimeoutError:
            return False

    def set_muted(self, muted: bool) -> None:
        self.muted = muted
        if muted:
            self.segmenter.reset()
            self._speech_started_at = None

    # ================================================================ input
    def push_audio(self, pcm16_16k: np.ndarray) -> None:
        """Feed 16 kHz mono PCM from the transport (any chunk size). Non-blocking."""
        if self.ended.is_set() or self._stopping or self.muted:
            return
        now = time.monotonic()
        for ev in self.segmenter.feed(pcm16_16k):
            if ev.type is SegmentEventType.SPEECH_START:
                self._speech_started_at = now
                self._last_activity = now
                self._reprompted = False
                self._fire(self._emit("user_speech", {"active": True}))
            elif ev.type is SegmentEventType.SPEECH_END and ev.audio is not None:
                self._speech_started_at = None
                self._last_activity = now
                self._utterances.put_nowait((ev.audio, now))
                self._fire(self._emit("user_speech", {"active": False, "duration_ms": ev.duration_ms}))
        if (
            self.config.barge_in
            and self._speech_started_at is not None
            and self.segmenter.in_speech
            and self.agent_speaking
            and (now - self._speech_started_at) * 1000 >= self.config.barge_in_min_ms
        ):
            onset = self._speech_started_at
            self._speech_started_at = None  # one interrupt per speech onset
            self._fire(self.interrupt("barge_in", onset=onset))

    @property
    def agent_speaking(self) -> bool:
        return self.sink.playing or self.state == "speaking"

    async def interrupt(self, source: str = "user", onset: float | None = None) -> None:
        """Stop agent audio immediately and cancel in-flight generation."""
        t0 = time.monotonic()
        dropped = self.sink.clear()
        if self._response and not self._response.done():
            self._response.cancel()
            with contextlib.suppress(BaseException):
                await self._response
        latency = (time.monotonic() - (onset or t0)) * 1000
        self.interrupt_latencies_ms.append(round(latency, 1))
        await self._emit("interrupted", {"source": source, "dropped_ms": round(dropped / self.sink.sample_rate * 1000),
                                         "latency_ms": round(latency, 1)})  # fmt: skip
        if self.session.end_requested:
            await self.stop(self.session.end_reason or "ended")
            return
        await self._set_state("listening")

    # ================================================================ turns
    async def _turn_worker(self) -> None:
        while not self.ended.is_set():
            audio, ended_at = await self._utterances.get()
            if self.ended.is_set():
                return
            # a new user turn supersedes any response still being generated
            if self._response and not self._response.done():
                await self.interrupt("new_turn")
            await self._set_state("thinking")
            try:
                result = await self.stt.transcribe(audio, 16_000, self.config.language)
            except ProviderError as exc:
                log.warning("STT failed: %s", exc)
                await self._emit("error", {**exc.to_dict(), "stage": "stt", "recoverable": True})
                await self._set_state("listening")
                continue
            if not result.text:
                await self._set_state("listening")
                continue
            metrics = TurnMetrics(stt_ms=result.latency_ms)
            await self._emit("transcript", {"role": "user", "text": result.text, "final": True,
                                            "metrics": {"stt_ms": result.latency_ms}})  # fmt: skip
            self._start_response(self._respond(result.text, ended_at, metrics))
            await asyncio.wait({self._response}) if self._response else None

    def _start_response(self, coro: Awaitable[None]) -> None:
        self._response = asyncio.create_task(coro)  # type: ignore[arg-type]

    async def _respond(self, text: str, speech_end: float, metrics: TurnMetrics) -> None:
        self.turns.append(metrics)
        spoken: list[tuple[int, str]] = []
        sentences: asyncio.Queue[str | None] = asyncio.Queue()
        llm_metrics: dict[str, Any] = {}

        async def synthesize_loop() -> None:
            while True:
                sentence = await sentences.get()
                if sentence is None:
                    return
                t0 = time.monotonic()
                res = await self.tts.synthesize(sentence, self._voice_for(sentence))
                marker = self._next_marker()
                self.sink.enqueue(res.pcm, res.sample_rate, marker)
                spoken.append((marker, sentence))
                if metrics.first_audio_ms is None:
                    metrics.tts_first_ms = round((time.monotonic() - t0) * 1000, 1)
                    metrics.first_audio_ms = round((time.monotonic() - speech_end) * 1000, 1)
                    await self._set_state("speaking")

        acquired = False
        tts_task: asyncio.Task[None] | None = None
        try:
            if self.ai_slot is not None:
                if self.ai_slot.locked():
                    await self._emit("state", {"state": "waiting", "detail": "waiting for local AI capacity"})
                await self.ai_slot.acquire()
                acquired = True
            tts_task = asyncio.create_task(synthesize_loop())
            async for ev in self.session.respond(text):
                if ev.type == "delta":
                    await self._emit("agent_delta", {"text": ev.text})
                elif ev.type == "sentence":
                    sentences.put_nowait(ev.text)
                elif ev.type == "tool":
                    await self._emit("tool", ev.data)
                elif ev.type == "done":
                    llm_metrics = ev.data
                if tts_task.done() and tts_task.exception():
                    raise tts_task.exception()  # type: ignore[misc]
            sentences.put_nowait(None)
            await tts_task
            metrics.llm_ttft_ms = llm_metrics.get("llm_ttft_ms")
            metrics.llm_total_ms = llm_metrics.get("llm_total_ms")
            if acquired and self.ai_slot is not None:
                self.ai_slot.release()
                acquired = False
            # commit only after playback: an interruption during playback records
            # just the sentences that were actually heard (see CancelledError below)
            await self.sink.wait_idle()
            full = " ".join(s for _, s in spoken)
            self.session.add_assistant(full, metrics=metrics.as_dict())
            await self._emit("transcript", {"role": "assistant", "text": full, "final": True,
                                            "metrics": metrics.as_dict()})  # fmt: skip
            self._last_activity = time.monotonic()
            if self.session.end_requested:
                await self.stop(self.session.end_reason or "ended")
                return
            await self._set_state("listening")
        except asyncio.CancelledError:
            metrics.interrupted = True
            started = self.sink.started_markers
            delivered = " ".join(s for m, s in spoken if m in started)
            self.session.add_assistant(delivered, interrupted=True, metrics=metrics.as_dict())
            if delivered:
                await self._emit("transcript", {"role": "assistant", "text": delivered, "final": True,
                                                "interrupted": True})  # fmt: skip
            raise
        except ProviderError as exc:
            log.warning("response failed: %s", exc)
            self.error = exc
            await self._emit("error", {**exc.to_dict(), "stage": "response", "recoverable": False})
            self.session.request_end("provider_error")
            fallback = self.session.fallback("llm_error")
            if acquired and self.ai_slot is not None:
                self.ai_slot.release()
                acquired = False
            with contextlib.suppress(ProviderError):
                res = await self.tts.synthesize(fallback, self._voice_for(fallback))
                self.sink.enqueue(res.pcm, res.sample_rate, self._next_marker())
                self.session.add_assistant(fallback, metrics={"fallback": "llm_error"})
                await self._emit("transcript", {"role": "assistant", "text": fallback, "final": True})
                await self.sink.wait_idle(timeout=15)
            await self.stop("provider_error")
        finally:
            if tts_task and not tts_task.done():
                tts_task.cancel()
                with contextlib.suppress(BaseException):
                    await tts_task
            if acquired and self.ai_slot is not None:
                self.ai_slot.release()

    def _voice_for(self, text: str) -> VoiceSettings:
        # shortcut: Hindi is the only second voice; add a language->voice map for more languages
        if self.config.hindi_voice and is_hindi(text):
            return replace(self.voice, voice=self.config.hindi_voice, speaker=None)
        return self.voice

    async def _speak(self, text: str, *, then_stop: str | None = None) -> None:
        """Speak a fixed utterance (greeting, re-prompt, goodbye)."""
        try:
            res = await self.tts.synthesize(text, self._voice_for(text))
        except ProviderError as exc:
            await self._emit("error", {**exc.to_dict(), "stage": "tts", "recoverable": then_stop is None})
            self.session.add_assistant(text, metrics={"tts_failed": True})
            await self._emit("transcript", {"role": "assistant", "text": text, "final": True})
            if then_stop:
                await self.stop(then_stop)
            else:
                await self._set_state("listening")
            return
        marker = self._next_marker()
        self.sink.enqueue(res.pcm, res.sample_rate, marker)
        await self._set_state("speaking")
        try:
            await self.sink.wait_idle()
        except asyncio.CancelledError:
            heard = text if marker in self.sink.started_markers else ""
            self.session.add_assistant(heard, interrupted=True)
            if heard:  # an interrupted greeting/re-prompt still belongs in the live transcript
                await self._emit("transcript", {"role": "assistant", "text": heard, "final": True, "interrupted": True})
            raise
        self.session.add_assistant(text)
        await self._emit("transcript", {"role": "assistant", "text": text, "final": True})
        self._last_activity = time.monotonic()
        if then_stop:
            await self.stop(then_stop)
        else:
            await self._set_state("listening")

    # ================================================================ watchdog
    async def _watchdog(self) -> None:
        cfg = self.config
        while not self.ended.is_set():
            await asyncio.sleep(cfg.watchdog_interval_s)
            now = time.monotonic()
            busy = (self._response is not None and not self._response.done()) or self.sink.playing
            if now - self._started_at >= cfg.max_duration_s and not self._stopping:
                if self._response and not self._response.done():
                    self._response.cancel()
                    with contextlib.suppress(BaseException):
                        await self._response
                self.session.request_end("max_duration")
                self._start_response(self._speak(self.session.fallback("max_duration"), then_stop="max_duration"))
                await asyncio.wait({self._response}) if self._response else None
                return
            if busy or self.segmenter.in_speech or not self._utterances.empty() or self.state != "listening":
                continue
            if now - self._last_activity >= cfg.silence_timeout_s:
                self._last_activity = now
                if not self._reprompted:
                    self._reprompted = True
                    self._start_response(self._speak(self.session.fallback("silence")))
                else:
                    self.session.request_end("silence_timeout")
                    self._start_response(self._speak(self.session.fallback("goodbye"), then_stop="silence_timeout"))

    # ================================================================ utils
    def _next_marker(self) -> int:
        self._marker += 1
        return self._marker

    async def _set_state(self, state: str) -> None:
        if self.state == state or self.ended.is_set():
            return
        self.state = state
        await self._emit("state", {"state": state})

    async def _emit(self, kind: str, data: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            res = self.on_event(kind, data)
            if inspect.isawaitable(res):
                await res
        except Exception:  # event consumers must never break the audio path
            log.exception("event callback failed")

    def _fire(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.append(task)
        task.add_done_callback(lambda t: self._tasks.remove(t) if t in self._tasks else None)
