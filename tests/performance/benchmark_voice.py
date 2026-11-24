"""Voice latency / resource benchmark against the REAL local stack.

Requires Ollama, whisper-server and Piper running (./scripts/start.sh). Usage:

    backend/.venv/bin/python tests/performance/benchmark_voice.py [--turns 10] [--out docs/BENCHMARKS.md]

Measures (p50 / p95 / max):
  * STT latency (whisper-server, warm) per utterance
  * LLM time-to-first-token and total generation time (warm model)
  * TTS latency for the first sentence of each reply
  * End of caller speech -> first agent audio queued (full VoicePipeline, Silero VAD)
  * Barge-in latency: caller speech onset -> agent audio cut (includes the configured
    300 ms talk-over guard)
  * One session vs two concurrent sessions sharing one AI-inference slot
  * Resident memory of Ollama / whisper-server / Piper / this process, and CPU
Caller audio is real speech synthesised by Piper with a different voice than the agent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import statistics
import sys
import time
from pathlib import Path

import numpy as np
import psutil

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]

from engine.audio.pcm import resample, silence
from engine.audio.vad import SegmenterConfig, SileroVAD, SpeechSegmenter
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

MODEL = "qwen3:4b-instruct-2507-q4_K_M"
AGENT_VOICE = VoiceSettings(voice="en_US-libritts_r-medium", speaker="0")
CALLER_VOICE = VoiceSettings(voice="en_GB-alba-medium")
QUESTIONS = [
    "Hello. What kind of hosting do you offer?",
    "Do you provide free SSL certificates?",
    "Can you move my existing website for me?",
    "How do I contact your support team?",
    "Is a VPS better for an online store?",
    "Do you include daily backups?",
    "Can I upgrade my plan later on?",
    "What is business email hosting?",
    "Do you offer WordPress hosting?",
    "Thanks. Can someone call me back tomorrow?",
    "How long does a migration usually take?",
    "Do you have servers for small businesses?",
]
KNOWLEDGE = (
    "Demo Hosting provides shared web hosting, WordPress hosting, business email and managed VPS servers. "
    "Free SSL is included. Free website migration is available. Support is by ticket and email. Daily backups are "
    "included. Plans can be upgraded later. Prices must be confirmed by the sales team."
)


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * p
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def summarize(values: list[float]) -> dict[str, float]:
    return {"n": len(values), "p50": round(pct(values, 0.5), 1), "p95": round(pct(values, 0.95), 1),
            "max": round(max(values), 1) if values else float("nan"),
            "mean": round(statistics.fmean(values), 1) if values else float("nan")}  # fmt: skip


def proc_rss(name_part: str) -> float:
    total = 0
    for p in psutil.process_iter(["name", "cmdline", "memory_info"]):
        try:
            cmd = " ".join(p.info["cmdline"] or []) + " " + (p.info["name"] or "")
            if name_part in cmd and p.pid != psutil.Process().pid:
                total += p.info["memory_info"].rss
        except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError):
            continue
    return round(total / 1024**2, 1)


async def caller_audio(tts: PiperHttpTTS, text: str) -> np.ndarray:
    res = await tts.synthesize(text, CALLER_VOICE)
    return resample(res.pcm, res.sample_rate, 16000)


def make_pipeline(llm, stt, tts, vad_path, slot, events):
    persona = AgentPersona(name="Asha", business_name="Demo Hosting", business_knowledge=KNOWLEDGE)
    cfg = SessionConfig(persona=persona, llm_options=LLMOptions(model=MODEL, max_tokens=90), greeting="")
    session = ConversationSession(cfg, llm, default_registry())
    sink = QueueAudioSink(16000)
    pipe = VoicePipeline(session, stt, tts, SpeechSegmenter(SileroVAD(vad_path), SegmenterConfig()), sink, AGENT_VOICE,
                         lambda k, d: events.append((time.monotonic(), k, d)),
                         PipelineConfig(silence_timeout_s=120, max_duration_s=900), ai_slot=slot)  # fmt: skip
    return pipe, sink


async def drive_turn(pipe: VoicePipeline, sink: QueueAudioSink, audio: np.ndarray, timeout: float = 60) -> bool:
    """Stream caller audio in real time, then consume agent audio until the reply finishes."""
    before = len(pipe.turns)
    for i in range(0, audio.size, 320):
        pipe.push_audio(audio[i : i + 320])
        sink.read(320)
        await asyncio.sleep(0.02)
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        sink.read(320)
        pipe.push_audio(np.zeros(320, np.int16))
        await asyncio.sleep(0.02)
        if len(pipe.turns) > before and pipe.state == "listening" and not sink.playing:
            return pipe.turns[-1].first_audio_ms is not None
    return False


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", type=int, default=10)
    ap.add_argument("--out", default=str(ROOT / "docs" / "BENCHMARKS.md"))
    args = ap.parse_args()
    llm = OllamaProvider()
    stt = WhisperServerSTT()
    tts = PiperHttpTTS()
    vad_path = ROOT / "data/models/vad/silero_vad.onnx"
    psutil.cpu_percent(interval=None)

    print("warming up model…")
    load_ms = await llm.warm_up(MODEL)
    clips = [await caller_audio(tts, q) for q in QUESTIONS]

    # ---- component micro-benchmarks
    stt_ms, ttft_ms, llm_total_ms, tts_ms = [], [], [], []
    for clip in clips[: args.turns]:
        r = await stt.transcribe(clip, 16000)
        stt_ms.append(r.latency_ms)
    for q in QUESTIONS[: args.turns]:
        t0 = time.perf_counter()
        first = None
        async for ch in llm.stream_chat([ChatMessage("system", "Reply in one short sentence."), ChatMessage("user", q)],
                                        LLMOptions(model=MODEL, max_tokens=60)):  # fmt: skip
            if ch.text and first is None:
                first = (time.perf_counter() - t0) * 1000
        ttft_ms.append(first or 0.0)
        llm_total_ms.append((time.perf_counter() - t0) * 1000)
    for s in ["Yes, we offer shared hosting and managed VPS servers.", "Free SSL certificates are included.",
              "A member of our team will call you back tomorrow."] * 3:  # fmt: skip
        tts_ms.append((await tts.synthesize(s, AGENT_VOICE)).latency_ms)

    # ---- full pipeline, one session
    print("single-session conversation…")
    events: list = []
    slot = asyncio.Semaphore(1)
    pipe, sink = make_pipeline(llm, stt, tts, vad_path, slot, events)
    await pipe.start()
    completed = 0
    cpu_samples = []
    for clip in clips[: args.turns]:
        ok = await drive_turn(pipe, sink, np.concatenate([silence(0.3, 16000), clip, silence(1.0, 16000)]))
        completed += int(ok)
        cpu_samples.append(psutil.cpu_percent(interval=None))
    first_audio = [t.first_audio_ms for t in pipe.turns if t.first_audio_ms is not None]
    e2e_stt = [t.stt_ms for t in pipe.turns if t.stt_ms is not None]
    e2e_ttft = [t.llm_ttft_ms for t in pipe.turns if t.llm_ttft_ms is not None]
    e2e_tts = [t.tts_first_ms for t in pipe.turns if t.tts_first_ms is not None]

    # ---- barge-in: talk over a long reply
    print("barge-in…")
    barge = []
    for _ in range(5):
        long_q = await caller_audio(tts, "Please explain every hosting plan you have in detail, one by one.")
        for i in range(0, long_q.size + 16000 * 1, 320):
            chunk = long_q[i : i + 320] if i < long_q.size else np.zeros(320, np.int16)
            pipe.push_audio(chunk)
            sink.read(320)
            await asyncio.sleep(0.02)
        t0 = time.monotonic()
        while pipe.state != "speaking" and time.monotonic() - t0 < 30:
            sink.read(320)
            pipe.push_audio(np.zeros(320, np.int16))
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.6)
        n = len(pipe.interrupt_latencies_ms)
        interrupt = await caller_audio(tts, "Sorry, wait, stop for a second.")
        for i in range(0, interrupt.size, 320):
            pipe.push_audio(interrupt[i : i + 320])
            sink.read(320)
            await asyncio.sleep(0.02)
            if len(pipe.interrupt_latencies_ms) > n:
                break
        if len(pipe.interrupt_latencies_ms) > n:
            barge.append(pipe.interrupt_latencies_ms[-1])
        await drive_turn(pipe, sink, silence(1.5, 16000), timeout=40)
    await pipe.stop("benchmark")

    # ---- two concurrent sessions, one inference slot
    print("two concurrent sessions…")
    ev2: list = []
    slot2 = asyncio.Semaphore(1)
    pa, sa = make_pipeline(llm, stt, tts, vad_path, slot2, ev2)
    pb, sb = make_pipeline(llm, stt, tts, vad_path, slot2, ev2)
    await pa.start()
    await pb.start()
    pair_turns = min(4, args.turns)
    for clip in clips[:pair_turns]:
        a = np.concatenate([silence(0.3, 16000), clip, silence(1.0, 16000)])
        await asyncio.gather(drive_turn(pa, sa, a), drive_turn(pb, sb, a))
    concurrent_first = [t.first_audio_ms for p in (pa, pb) for t in p.turns if t.first_audio_ms is not None]
    concurrent_ok = sum(1 for p in (pa, pb) for t in p.turns if t.first_audio_ms is not None)
    await pa.stop("benchmark")
    await pb.stop("benchmark")

    mem = {"ollama_mb": proc_rss("ollama"), "whisper_server_mb": proc_rss("whisper-server"),
           "piper_mb": proc_rss("piper.http_server"), "benchmark_process_mb": round(psutil.Process().memory_info().rss / 1024**2, 1),
           "system_used_percent": psutil.virtual_memory().percent}  # fmt: skip
    results = {
        "date": time.strftime("%Y-%m-%d %H:%M %Z"),
        "machine": f"{platform.machine()} {platform.mac_ver()[0]} {round(psutil.virtual_memory().total / 1024**3)} GB",
        "model": MODEL, "model_load_ms": load_ms, "turns": args.turns,
        "stt_ms": summarize(stt_ms), "llm_ttft_ms": summarize(ttft_ms), "llm_total_ms": summarize(llm_total_ms),
        "tts_first_sentence_ms": summarize(tts_ms),
        "pipeline": {"first_audio_ms": summarize(first_audio), "stt_ms": summarize(e2e_stt), "llm_ttft_ms": summarize(e2e_ttft),
                     "tts_first_ms": summarize(e2e_tts), "completion_rate": round(completed / args.turns, 3)},
        "barge_in_ms": summarize(barge),
        "two_sessions": {"first_audio_ms": summarize(concurrent_first), "completed_turns": concurrent_ok, "attempted_turns": pair_turns * 2},
        "cpu_percent_during_conversation": summarize(cpu_samples),
        "memory": mem,
    }  # fmt: skip
    print(json.dumps(results, indent=2))
    Path(args.out).write_text(render(results))
    (Path(args.out).with_suffix(".json")).write_text(json.dumps(results, indent=2))
    print(f"wrote {args.out}")


def row(name: str, s: dict[str, float]) -> str:
    return f"| {name} | {s['n']} | {s['p50']} | {s['p95']} | {s['max']} |"


def render(r: dict) -> str:
    p = r["pipeline"]
    return f"""# Voice latency benchmarks

Measured with `tests/performance/benchmark_voice.py` on {r['machine']}, {r['date']}.
Model `{r['model']}` (warm; cold load took {r['model_load_ms']:.0f} ms). Caller audio is real speech synthesised by
Piper (`en_GB-alba-medium`); the agent speaks with `en_US-libritts_r-medium`. All components are local.

## Components (milliseconds)

| Measurement | n | p50 | p95 | max |
|---|---|---|---|---|
{row('Speech-to-text (whisper.cpp base.en, warm, per utterance)', r['stt_ms'])}
{row('LLM time to first token (warm)', r['llm_ttft_ms'])}
{row('LLM full reply (≤60 tokens)', r['llm_total_ms'])}
{row('TTS, one sentence (Piper)', r['tts_first_sentence_ms'])}

## Full voice pipeline, one session (milliseconds)

Silero VAD → whisper.cpp → Qwen3 4B → Piper, {r['turns']} caller turns. "First audio" is measured from the moment the
end of the caller's speech is detected (after the 700 ms end-of-turn silence) to the first agent audio being queued
for playback.

| Measurement | n | p50 | p95 | max |
|---|---|---|---|---|
{row('End of speech → first agent audio', p['first_audio_ms'])}
{row('  of which: speech-to-text', p['stt_ms'])}
{row('  of which: LLM first token', p['llm_ttft_ms'])}
{row('  of which: first sentence synthesis', p['tts_first_ms'])}
{row('Barge-in: caller speech onset → agent audio cut', r['barge_in_ms'])}

Turn completion rate: **{p['completion_rate'] * 100:.0f}%**. The perceived gap after the caller stops talking is the
end-of-turn silence (700 ms, configurable) plus "first agent audio". Barge-in latency includes the deliberate
300 ms talk-over guard that prevents coughs and echo from interrupting the agent.

## Two concurrent sessions, one inference slot

| Measurement | n | p50 | p95 | max |
|---|---|---|---|---|
{row('End of speech → first agent audio', r['two_sessions']['first_audio_ms'])}

{r['two_sessions']['completed_turns']} of {r['two_sessions']['attempted_turns']} turns completed. Both callers spoke at the same time, so the second
reply waits for the first to finish generating (the queue is visible in the UI as "Waiting for the local model").
This is why the default is `VA_AI_CONCURRENCY=1` with at most two voice sessions on a 16 GB Mac.

## Resources

| | |
|---|---|
| Ollama (model loaded) | {r['memory']['ollama_mb']} MB |
| whisper-server | {r['memory']['whisper_server_mb']} MB |
| Piper server | {r['memory']['piper_mb']} MB |
| Benchmark process (2 Silero VAD sessions, pipeline) | {r['memory']['benchmark_process_mb']} MB |
| System memory in use | {r['memory']['system_used_percent']}% |
| CPU during conversation (p50 / p95) | {r['cpu_percent_during_conversation']['p50']}% / {r['cpu_percent_during_conversation']['p95']}% |

Raw numbers: `docs/BENCHMARKS.json`.
"""


if __name__ == "__main__":
    asyncio.run(main())
