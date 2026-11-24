# Environment report

Inspection date: **2026-11-24**. This report describes the development machine voice-agent was built and verified on. Re-run `./scripts/setup-mac.sh` on another machine; it prints the same checks.

## Hardware and OS

| Item | Value |
|---|---|
| Machine | MacBook Pro, Apple M1 Pro (8 performance + 2 efficiency cores) |
| Architecture | arm64 (`uname -m`) |
| Unified memory | 16 GB (`hw.memsize` = 17179869184) |
| macOS | 27.2.1 (build 26B131), Darwin 27.2.0 |
| Free disk at start | ~25 GB (data volume ~95 % full), so speech models were kept small |

## Toolchain

| Tool | Version | Status |
|---|---|---|
| Homebrew | 7.0.13 | pre-installed |
| Git | 2.54.0 (Apple) | pre-installed |
| Python (system default) | 3.14.7 (uv-managed) | pre-installed; **not used** for the backend because several native wheels (aiortc, onnxruntime) target ≤ 3.13 |
| Python (backend) | 3.12.14 via `uv venv --python 3.12` | pre-installed through uv |
| uv | 0.12.18 → 0.13.8 | pre-installed |
| Node.js / npm | 22.20.0 / 10.9.3 | pre-installed |
| pnpm | corepack shim broken (`Cannot find module …/pnpm.cjs`) | **not used**; the project uses npm, and the user's global corepack was not modified |
| FFmpeg | 9.0.2 | pre-installed |
| whisper.cpp | 1.9.6 (`brew install whisper-cpp`) | **installed** (missing; MIT; Metal backend confirmed in logs) |
| Piper TTS | piper-tts 1.8.0 (GPL-3.0) in `.tools/piper` | **installed** in an isolated venv and run as a separate process |
| Docker | 29.9.3 client | present; not required |
| Asterisk | not installed | optional; only needed for real SIP/PSTN calling |

## Ollama

* Version **0.40.16**, installed via Homebrew. The server was **not running** at inspection time. It was started with `OLLAMA_HOST=127.0.0.1:11434 ollama serve`, and `lsof` confirmed it listens on `127.0.0.1:11434` only. Ollama was not reinstalled.
* Installed models:

| Model | Size | Runner | Local? | Notes |
|---|---|---|---|---|
| `qwen3:4b-instruct-2507-q4_K_M` | 2.5 GB (3.2 GB in memory) | ggml/Metal | ✅ | **Selected default.** Non-thinking instruct model with tool calling. |
| `qwen3.5:latest` | 7.6 GB | MLX | ✅ | Usable, but leaves little headroom on 16 GB next to STT/TTS/WebRTC. Selectable per agent. |
| `glm-5.3:cloud` | — | remote (`https://ollama.com`) | ❌ | **Cloud proxy model.** voice-agent reports it as *not local* and refuses it unless `VA_ALLOW_REMOTE_LLM_MODELS=true`. There is no silent cloud fallback. |

* Measured on the default model (`/api/chat`, streaming, `think=false`, 80-token cap):
  * cold load + first token: **2.8 s**
  * warm time-to-first-token: **0.07–0.10 s**
  * generation speed: **~41 tokens/s**
* No LLM was downloaded.

## Speech models (downloaded to `data/models/`, git-ignored)

| Model | Size | License | Purpose |
|---|---|---|---|
| `whisper/ggml-base.en-q5_1.bin` | 57 MB | MIT (OpenAI Whisper weights) | default STT |
| `whisper/ggml-small.en-q5_1.bin` | 181 MB | MIT | optional higher-accuracy STT |
| `piper/en_GB-alba-medium.onnx` | 60 MB | model card: dataset CC BY 4.0, fine-tuned from Hi-Fi TTS base | **default voice** |
| `piper/en_US-libritts_r-medium.onnx` | 75 MB | model card: dataset LibriTTS-R, CC BY 4.0, but fine-tuned from `lessac` (Blizzard licence) | optional; not the default because of its lineage |
| `vad/silero_vad.onnx` | 2.1 MB | MIT | voice activity detection |

Voices whose datasets are non-commercial or restricted were not downloaded (`lessac`: Blizzard licence; `ryan`: CC BY-NC-SA; `hi_IN-pratham`: CC BY-NC-SA). See [MODEL-LICENSING.md](MODEL-LICENSING.md).

Speech latency measured on a 4.3 s utterance:

| Component | Latency |
|---|---|
| whisper-cli base.en (includes model load) | 0.33 s |
| whisper-cli small.en (includes model load) | 0.57 s |
| **whisper-server base.en, warm (Metal)** | **0.13 s** |
| Piper CLI (cold start, includes Python start-up) | 0.76 s |
| Piper HTTP server, warm, one sentence | ~0.15 s |

## Compatibility notes and limitations

* The backend needs Python 3.11–3.13. Python 3.14 is excluded until aiortc, onnxruntime and PyAV publish wheels for it.
* Piper ≥ 1.3 is GPL-3.0. voice-agent (MIT) never imports it. Piper runs as `python -m piper.http_server` from its own environment and is reached over HTTP on loopback.
* The machine is memory-constrained, so the default is **one concurrent AI inference** (`VA_AI_CONCURRENCY=1`). Up to **two browser voice sessions** are admitted and queue for the model (`VA_MAX_VOICE_SESSIONS=2`). Telephony/simulation concurrency is configured separately.
* Docker is not used. Everything runs natively for Metal acceleration and low latency.
