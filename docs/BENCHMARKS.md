# Voice latency benchmarks

Measured with `tests/performance/benchmark_voice.py` on arm64 27.2.1 16 GB, 2026-11-25 00:01 IST.
Model `qwen3:4b-instruct-2507-q4_K_M` (warm; cold load took 19 ms). Caller audio is real speech synthesised by
Piper (`en_GB-alba-medium`); the agent speaks with `en_US-libritts_r-medium`. All components are local.

## Components (milliseconds)

| Measurement | n | p50 | p95 | max |
|---|---|---|---|---|
| Speech-to-text (whisper.cpp base.en, warm, per utterance) | 10 | 160.7 | 180.0 | 189.9 |
| LLM time to first token (warm) | 10 | 124.3 | 681.9 | 1077.0 |
| LLM full reply (≤60 tokens) | 10 | 1022.5 | 1828.6 | 1988.4 |
| TTS, one sentence (Piper) | 9 | 62.8 | 82.0 | 82.3 |

## Full voice pipeline, one session (milliseconds)

Silero VAD → whisper.cpp → Qwen3 4B → Piper, 10 caller turns. "First audio" is measured from the moment the
end of the caller's speech is detected (after the 700 ms end-of-turn silence) to the first agent audio being queued
for playback.

| Measurement | n | p50 | p95 | max |
|---|---|---|---|---|
| End of speech → first agent audio | 10 | 1064.7 | 2036.8 | 2473.4 |
|   of which: speech-to-text | 10 | 172.2 | 198.2 | 199.1 |
|   of which: LLM first token | 10 | 203.2 | 904.7 | 1443.1 |
|   of which: first sentence synthesis | 10 | 108.2 | 195.1 | 237.0 |
| Barge-in: caller speech onset → agent audio cut | 2 | 316.6 | 317.2 | 317.3 |

Turn completion rate: **100%**. The perceived gap after the caller stops talking is the
end-of-turn silence (700 ms, configurable) plus "first agent audio". Barge-in latency includes the deliberate
300 ms talk-over guard that prevents coughs and echo from interrupting the agent.

## Two concurrent sessions, one inference slot

| Measurement | n | p50 | p95 | max |
|---|---|---|---|---|
| End of speech → first agent audio | 8 | 2006.1 | 3562.7 | 3961.2 |

8 of 8 turns completed. Both callers spoke at the same time, so the second
reply waits for the first to finish generating (the queue is visible in the UI as "Waiting for the local model").
This is why the default is `VA_AI_CONCURRENCY=1` with at most two voice sessions on a 16 GB Mac.

## Resources

| | |
|---|---|
| Ollama (model loaded) | 198.8 MB |
| whisper-server | 18.8 MB |
| Piper server | 173.1 MB |
| Benchmark process (2 Silero VAD sessions, pipeline) | 72.4 MB |
| System memory in use | 87.4% |
| CPU during conversation (p50 / p95) | 16.1% / 27.4% |

Notes on these numbers:

* Model weights are held in GPU-wired unified memory (about 3.2 GB for this model according to `ollama ps`).
  That memory is not counted in the Ollama process RSS above.
* Only 2 of the 5 scripted barge-in attempts overlapped the agent's speech long enough to trigger an interruption, so
  that row has a small sample. Barge-in behaviour is also covered deterministically by
  `tests/unit/test_engine_pipeline.py::test_barge_in_interrupts_and_records_partial`.
* LLM first-token p95 is dominated by the first turn of a conversation, where the system prompt is processed for the first
  time. Live browser sessions prime that prompt while the greeting plays (`app/audio/sessions.py`).
* This run overlapped with automated security scans on the same machine, so it is a conservative measurement.

Raw numbers: `docs/BENCHMARKS.json`.
