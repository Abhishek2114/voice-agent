# Architecture

This document describes how voice-agent is put together: the three main components, the real-time voice pipeline, the scheduler, the data model and the extension points. Everything described here is in the repository. Where a behaviour has limits (for example, TTS is not streamed below sentence level), the limit is stated.

## Contents

- [Components and module boundaries](#components-and-module-boundaries)
- [System diagram](#system-diagram)
- [The voice pipeline](#the-voice-pipeline)
- [Conversation state machine](#conversation-state-machine)
- [Barge-in and interruption](#barge-in-and-interruption)
- [Watchdog: silence and maximum duration](#watchdog-silence-and-maximum-duration)
- [Capacity limits](#capacity-limits)
- [Providers and how to add one](#providers-and-how-to-add-one)
- [Call modes](#call-modes)
- [Agent tools and lead capture](#agent-tools-and-lead-capture)
- [Public "talk to us" links](#public-talk-to-us-links)
- [Campaign scheduler](#campaign-scheduler)
- [Data model](#data-model)
- [Processes and ports](#processes-and-ports)

## Components and module boundaries

| Directory | What it is | Depends on |
|---|---|---|
| `engine/` | Transport-independent conversation engine: audio utilities, VAD and turn segmentation, the voice pipeline, conversation sessions, tools, post-call analysis, provider interfaces and adapters, and the WebRTC/RTP transports. | numpy, onnxruntime, httpx, aiortc/PyAV. **It never imports the backend (`app`).** |
| `backend/app/` | FastAPI application: authentication, workspaces and roles, agents, contacts, CSV import, knowledge base, compliance, campaigns, calls, analytics, system settings, backups. It also contains the campaign scheduler (`app/scheduler`), which runs as a separate process. | `engine`, SQLAlchemy (async) + SQLite, Alembic |
| `apps/dashboard/` | Next.js 16 dashboard (React 19, Tailwind CSS 4, shadcn/ui on Base UI, TanStack Query). It calls the API through a same-origin `/api/*` rewrite to `http://127.0.0.1:8000`, so cookies stay first-party. It also holds the browser side of voice calls (`src/lib/voice-client.ts`) and the public `/talk/[token]` page. | The backend's HTTP API only |
| `scripts/` | `setup-mac.sh`, `start.sh`, `stop.sh`, `health-check.sh`, `backup.sh` and the shared `lib.sh`. | Homebrew, uv, npm |
| `demo/shrotihost/` | An importable sample workspace (fictional data). | — |

The rules behind the boundaries:

* **The engine knows nothing about HTTP, the database or tenants.** The backend supplies everything that touches persistent state through small interfaces: a `ToolHost` (`engine/conversation/tools.py`) for tool side effects, a knowledge callback for retrieval, and event callbacks for transcripts and state.
* **The same pipeline serves every audio transport.** `VoicePipeline` only sees 16 kHz PCM going in (`push_audio`) and a `QueueAudioSink` coming out. The browser transport (`engine/streaming/webrtc.py`) and the RTP transport for Asterisk (`engine/providers/telephony/rtp.py`) both implement that surface.
* **The backend owns tenancy.** Every tenant-owned table has a `workspace_id` column. Every query filters on it, and resources that belong to another workspace return 404.

## System diagram

```text
                                     ┌──────────────── this Mac (services bind to 127.0.0.1) ─────────────────┐
 Browser                             │                                                                        │
 ┌─────────────────────────┐  HTTP   │  ┌───────────────────────┐   /api/* rewrite   ┌─────────────────────┐ │
 │ Next.js dashboard UI    │────────────►│ next start / next dev │──────────────────► │ FastAPI (uvicorn)   │ │
 │  - REST via /api/*      │  :3000  │  │ :3000                 │                    │ :8000               │ │
 │  - voice-client.ts      │         │  └───────────────────────┘                    │  auth, workspaces,  │ │
 │                         │  SDP offer/answer over POST /api/.../voice/sessions      │  CRM, campaigns,    │ │
 │  mic ──► Opus ─────────────────── WebRTC media (no STUN/TURN) ──────────────────► │  compliance, calls  │ │
 │  speaker ◄── Opus ◄───────────────────────────────────────────────────────────────│  VoiceSessionManager│ │
 │  "events" data channel ◄──────── JSON events / control ─────────────────────────► │                     │ │
 └─────────────────────────┘         │                                               └──────────┬──────────┘ │
                                     │                                                          │            │
                                     │    ┌─────────────── engine.VoicePipeline (per call) ─────┴──────────┐ │
                                     │    │ aiortc track ─► resample 16 kHz ─► Silero VAD segmenter        │ │
                                     │    │      (end of turn) ─► whisper.cpp STT (one request per turn)   │ │
                                     │    │      ─► ConversationSession ─► Ollama /api/chat (streamed)     │ │
                                     │    │           ▲ tools (ToolRegistry ─► DbToolHost)                 │ │
                                     │    │           ▲ knowledge (SQLite FTS5, BM25, per turn)            │ │
                                     │    │      ─► SentenceSplitter ─► Piper /synthesize (per sentence)   │ │
                                     │    │      ─► QueueAudioSink (48 kHz) ─► aiortc track ─► browser     │ │
                                     │    └───────────────────────────────────────────────────────────────┘ │
                                     │                                                                        │
                                     │  whisper-server :8178    Piper http_server :5005    Ollama :11434      │
                                     │                                                                        │
                                     │  ┌────────────────────────┐        ┌───────────────────────────────┐  │
                                     │  │ Scheduler process      │◄──────►│ SQLite (WAL) data/database/   │  │
                                     │  │ python -m              │        │ + data/recordings, uploads,   │  │
                                     │  │ app.scheduler.main     │        │   backups, models             │  │
                                     │  └──────────┬─────────────┘        └───────────────────────────────┘  │
                                     │             │ SIMULATION: text-mode simulated calls (no network)       │
                                     │             │ PSTN (off by default): Asterisk ARI + RTP externalMedia  │
                                     └─────────────┼──────────────────────────────────────────────────────────┘
                                                   ▼
                                       Your Asterisk server ─► your SIP trunk (optional, operator-supplied)
```

## The voice pipeline

The pipeline lives in `engine/conversation/pipeline.py`. One `VoicePipeline` instance is created per live call (browser or PSTN). Its stages:

1. **Transport in.** `WebRTCTransport` (`engine/streaming/webrtc.py`, built on aiortc) accepts only offers whose media sections are audio plus the data channel. Offers with video are rejected before negotiation (`check_offer`). It receives the browser's microphone track. aiortc decodes the Opus audio. PyAV's `AudioResampler` converts it to 16 kHz mono s16 PCM, which goes to `VoicePipeline.push_audio()`. For PSTN calls, `RtpMedia` receives slin16 RTP from Asterisk and the same method is used.
2. **VAD and turn segmentation.** `SpeechSegmenter` (`engine/audio/vad.py`) runs Silero VAD v5 through onnxruntime (no PyTorch) on 512-sample (32 ms) frames. It emits `SPEECH_START` once speech has lasted `min_speech_ms` (default 160 ms), keeps a pre-roll (default 300 ms), and emits `SPEECH_END` with the utterance audio after `end_silence_ms` of silence (default 700 ms). An utterance is cut at 30 s. An energy-based VAD (`EnergyVAD`) exists for tests and as an explicit, user-selectable fallback. It is never substituted silently.
3. **Speech-to-text (per turn, not streaming).** A turn worker takes each finished utterance and sends it once to whisper.cpp. The default is `WhisperServerSTT`, which posts to a warm `whisper-server` at `/inference`. `WhisperCliSTT`, which runs `whisper-cli` per utterance, is an alternative. **There are no partial transcripts.** Transcription starts after end-of-turn is detected. Known Whisper hallucinations on silence (for example "Thank you." on very short audio, or `[BLANK_AUDIO]`) are removed by `clean_transcript`.
4. **Conversation session.** `ConversationSession` (`engine/conversation/session.py`) keeps isolated per-call memory. It never shares history between calls, even when they use the same agent and model. For each user turn it:
   * appends the user text, detects end phrases ("goodbye", "that's all", …) and enforces `max_turns` (default 40);
   * **retrieves knowledge for that turn** from the workspace knowledge base, using SQLite FTS5 with BM25 ranking (`backend/app/knowledge/service.py`), and adds the top 3 snippets as an extra system message;
   * streams a chat completion from Ollama (`/api/chat`, `stream: true`) with the agent's permitted tools. For models that advertise `thinking`, it sends `think: false`;
   * executes any tool calls through the `ToolRegistry`, feeds the results back, and generates again. There are at most two tool rounds; on the last round tools are withheld, which forces a spoken answer.
5. **Sentence splitting.** LLM text arrives token by token. `SentenceSplitter` (`engine/conversation/text.py`) yields complete sentences as soon as they end. `speechify` strips markdown, URLs, emoji and `<think>` blocks before anything is spoken.
6. **Text-to-speech (sentence-pipelined, not streamed below sentence level).** Each sentence is synthesised **in full** by Piper (`POST /synthesize` on the local Piper HTTP server) or macOS `say`, then queued. Sentence *n+1* is synthesised while sentence *n* plays. The first audio therefore starts once the first sentence is ready, not after the whole reply. Neither TTS adapter streams audio within a sentence (`streaming = False`).
7. **Transport out.** `QueueAudioSink` (`engine/streaming/sink.py`) holds the queued audio at 48 kHz for WebRTC. `SinkAudioTrack` pulls 20 ms frames in real time, returns silence when the queue is idle, and aiortc encodes them as Opus. Each queued sentence carries a marker, so the pipeline knows which sentences actually *started* playing.

After a reply has finished playing, the session records exactly what was spoken. When the call ends, `VoiceSessionManager._finalize` (`backend/app/audio/sessions.py`) runs post-call analysis (`engine/conversation/outcome.py`: an LLM summary and outcome classification, with deterministic overrides from tool effects). It then persists the conversation, messages, transcript and recording (only if recording is enabled) through `persist_conversation` (`backend/app/calls/service.py`). If analysis fails, the call stores an analysis error rather than a guessed outcome.

Before a call starts, `VoiceSessionManager.preflight` checks that Ollama is reachable, the selected model is installed and local, STT and TTS are healthy, and the VAD model loads. A call that cannot work fails fast with an explicit error code (`llm_unavailable`, `model_not_found`, `remote_model_refused`, `stt_unavailable`, `tts_unavailable`, `vad_unavailable`). While the greeting plays, the backend asks Ollama to load the model (`warm_up`) so the first reply avoids a cold model load where possible.

## Conversation state machine

The pipeline reports its state to the client in `state` events:

```text
            start()                       end of user turn
  idle ───────────────► speaking ─────► listening ─────────────► thinking
            (greeting)     ▲   │ playback done      ▲                 │
                           │   └────────────────────┘                 │ AI slot busy
                           │                                          ▼
                           │  first sentence queued              (state "waiting",
                           └──────────────────────────────────────  then thinking)
  any state ── stop(reason) ──► ended
```

| State | Meaning |
|---|---|
| `listening` | Waiting for the user to speak. |
| `thinking` | A user turn ended. STT and then the LLM are running. |
| `waiting` | Sent as `{"type":"state","state":"waiting","detail":"waiting for local AI capacity"}` when the AI semaphore is held by another conversation. The reply starts as soon as the slot frees up. |
| `speaking` | At least one synthesised sentence is queued or playing. |
| `ended` | The call is over. An `ended` event with a `reason` follows (for example `user_ended`, `user_goodbye`, `agent_ended`, `silence_timeout`, `max_duration`, `max_turns`, `provider_error`, `connection_timeout`, `remote_track_ended`). |

If a provider fails in the middle of a reply (for example Ollama stops responding), the pipeline emits an `error` event, speaks the agent's `llm_error` fallback line if TTS still works, and ends the call with reason `provider_error`.

## Barge-in and interruption

* **Barge-in.** While the agent is speaking, if the segmenter has detected speech for at least `barge_in_min_ms` (default 300 ms, configurable under **Settings → Speech and audio**), the pipeline calls `interrupt("barge_in")`. Each speech onset triggers at most one interrupt.
* **Manual interrupt.** The client can send `{"type":"interrupt"}` on the data channel, or `POST …/voice/sessions/{call_id}/control`.
* **What an interrupt does.** It clears the outbound audio queue immediately, cancels the in-flight LLM/TTS task, emits an `interrupted` event with `dropped_ms` and `latency_ms`, and returns to `listening`. The transcript keeps **only the sentences that actually started playing**, marked as interrupted. A new user turn also supersedes any reply still being generated.
* Barge-in can be disabled globally. Browser echo cancellation is requested on the microphone (`echoCancellation: true`), but loudspeaker playback can still trigger self-interruption. Headphones avoid this.

## Watchdog: silence and maximum duration

A watchdog task checks the pipeline every 200 ms:

* **Maximum duration.** When `max_duration_seconds` (per agent, default 600) is reached, any reply in progress is cancelled. The agent speaks its `max_duration` fallback line and the call ends with reason `max_duration`.
* **Silence.** While `listening` with no speech, no queued utterance and no reply in progress, the watchdog measures inactivity. After `silence_timeout_seconds` (per agent, default 20) it speaks the `silence` re-prompt ("Are you still there?"). If another timeout passes in silence, it speaks the `goodbye` line and ends the call with reason `silence_timeout`.

## Capacity limits

| Limit | Setting | Default | Scope |
|---|---|---|---|
| Concurrent AI inference | `VA_AI_CONCURRENCY` | 1 | An `asyncio.Semaphore` (`ProviderHub.ai_slot`) **per process**. The API process and the scheduler process each have their own. A voice reply holds the slot from the start of LLM generation until the last sentence has been *synthesised*, and releases it before playback finishes. Simulated-call turns, post-call analysis and the model test endpoint also take the slot. |
| Concurrent browser voice sessions | `VA_MAX_VOICE_SESSIONS` | 2 | API process. Extra sessions are refused with `503 capacity`. Public-link sessions count toward the same limit. |
| Concurrent campaign calls | `VA_TELEPHONY_CONCURRENCY` | 4 | Scheduler process, across all campaigns. Each campaign also has `max_concurrent_calls` (1–20). |

Because the semaphore is per process, a browser conversation and a running campaign simulation can use Ollama at the same moment. On a 16 GB machine, keep campaign concurrency low while testing voice calls.

## Providers and how to add one

Provider interfaces are `typing.Protocol` classes, so adapters need no common base class:

| Kind | Interface | Adapters in the repository |
|---|---|---|
| LLM | `engine/providers/llm/base.py`: `LLMProvider` (`health`, `list_models`, `stream_chat`, `chat_json`) | `OllamaProvider` (`ollama.py`); `ScriptedLLM` for tests |
| STT | `engine/providers/stt/base.py`: `STTProvider` (`health`, `transcribe`; `streaming` flag) | `WhisperServerSTT`, `WhisperCliSTT` (`whisper_cpp.py`); `FakeSTT` |
| TTS | `engine/providers/tts/base.py`: `TTSProvider` (`health`, `list_voices`, `synthesize`; `streaming` flag) | `PiperHttpTTS` (`piper_http.py`), `MacSayTTS` (`macos_say.py`); `FakeTTS` |
| Telephony | `engine/providers/telephony/base.py`: `TelephonyProvider` (`validate_config`, `health`, `dial`, `hangup`, `on_inbound`; `is_real` flag) | `SimulatedTelephonyProvider` (`simulator.py`), `AsteriskAriProvider` (`asterisk_ari.py`) |
| VAD | `engine/audio/vad.py`: `VAD` (`probability`, `reset`) | `SileroVAD`, `EnergyVAD` |

Shared types live in `engine/providers/base.py`. `ProviderHealth` has a status of `ok`, `degraded`, `unavailable` or `disabled`, plus an `is_local` flag. `ProviderError` has a stable `code` and a `retryable` flag, and both are shown in the API and UI.

Providers are built in one place: `ProviderHub` in `backend/app/ai/hub.py`. It reads the global `provider_configs` rows, decrypts provider secrets, builds adapters lazily, and exposes `llm()`, `stt()`, `tts(name)`, `vad()`, `telephony(real)` and an aggregated `health()`. Tests inject fakes through `hub.overrides`.

To add a provider:

1. Implement the protocol in `engine/providers/<kind>/<name>.py`. Raise `ProviderError` subclasses (`LLMError`, `STTError`, `TTSError`, `TelephonyError`) with meaningful codes. Report `is_local` honestly in `health()`.
2. Register it in `backend/app/ai/hub.py`. For STT, TTS and telephony, add the name to `STT_PROVIDERS`, `TTS_PROVIDERS` or `TELEPHONY_PROVIDERS` and add a branch to the matching builder. The settings endpoint (`PUT /api/system/providers/{kind}`) derives its allowed values from these tuples. **For an LLM provider**, `hub.llm()` currently builds `OllamaProvider` directly, and `ALLOWED["llm"]` in `backend/app/system/router.py` only allows `"ollama"`, so both need changing.
3. Keep the project's policies. Endpoints should be local. Any `base_url` saved through settings passes the SSRF check in `backend/app/core/ssrf.py`. Models or services that are not local must be reported as such and refused by default. Never add a silent cloud fallback.
4. Add unit tests next to the existing ones in `tests/unit/test_provider_adapters.py` (HTTP adapters are tested with `respx`/mock transports).

## Call modes

Every call row has a `mode` column constrained to one of three values. The modes are never mixed.

| Mode | How it runs | Effects on real data |
|---|---|---|
| `WEBRTC` | A live voice conversation from the dashboard voice playground (channel `dashboard`) or a customer link (channel `public_link`). Real audio and models. | Tools write real records. Opt-outs suppress the number, leads create contacts, callbacks are stored, and the contact's CRM status is updated from the outcome. |
| `SIMULATION` | Text only. No audio, STT or TTS. `SimulatedTelephonyProvider` decides answered/busy/failed/no-answer deterministically from a seed. A scripted persona or a local-LLM role-play plays the callee, and the agent runs the real `ConversationSession` and tools. Duration is estimated from word count. Used by campaigns, the "simulate call" button and simulated inbound calls. | `DbToolHost` does **not** apply opt-outs or create leads in simulation; they appear only in the call's tool log. Callback requests are stored with `is_simulated = true`. Simulated outcomes never change a real contact's status. |
| `PSTN` | A real outbound phone call through Asterisk ARI (`backend/app/calls/pstn.py`). Audio goes through the same `VoicePipeline` over RTP. **Disabled by default and not production-verified**; see [TELEPHONY.md](TELEPHONY.md). | Same as `WEBRTC`. |

## Agent tools and lead capture

The LLM can only *request* tools from a fixed registry (`default_registry()` in `engine/conversation/tools.py`):

| Tool | Effect |
|---|---|
| `end_call` | Ends the call after the next sentence (reason enum includes `wrong_number`). |
| `request_callback` | Stores a callback request for a human. Never a confirmed booking. |
| `request_appointment` | Stores an appointment *request*. The model is told explicitly that nothing is booked. |
| `record_opt_out` | Suppresses the caller's number, sets the contact to `do_not_call`, revokes their consent records, and ends the call. |
| `search_knowledge` | Searches the workspace knowledge base. |
| `record_interest` | Records an interest level as a call effect. |
| `capture_lead` | Saves the person as a lead (below). |

`ToolRegistry.execute` refuses unknown tools and tools the agent has not been granted. It validates arguments against a strict schema: unknown keys are rejected, and types, maximum lengths and enums are checked. Handler exceptions become a generic failure message. No tool runs shell commands, fetches URLs, or completes a booking, payment or refund. Agents get `end_call`, `request_callback`, `record_opt_out`, `search_knowledge`, `record_interest` and `capture_lead` by default. `request_appointment` must be enabled per agent.

**Lead capture flow.** The system prompt tells the agent to collect a name, phone number (read back to confirm) and requirement, and to ask whether the team may follow up. It then calls `capture_lead(name, phone?, email?, company?, requirement?, consent_to_contact?)`:

1. The tool handler refuses to save a lead without a phone number or email.
2. `DbToolHost.capture_lead` (`backend/app/calls/service.py`) normalises the phone number with libphonenumber, using the workspace's default country.
3. It reuses the call's contact if there is one. Otherwise it deduplicates within the workspace by E.164 number or email. If nothing matches, it creates a contact with `status = "lead"`, tag `ai-lead`, and source `public_link` or `conversation`. Existing fields are not overwritten. The requirement is appended to the contact notes with a date.
4. **Only if `consent_to_contact` is exactly `true`**, it creates a `ConsentRecord` with purpose `inquiry_followup`, source `verbal_call`, evidence that references the call ID, and an expiry of `inquiry_followup_days` (default 7) from the workspace policy.
5. It links the call to the contact, writes a `lead.captured` audit entry, and tells the model whether the number looked invalid (so it can read it back) and whether consent was given.

In `SIMULATION` mode the host returns a placeholder result and writes nothing. In the agent builder's text test console, a dry-run host is used and nothing is saved either.

## Public "talk to us" links

A customer-facing browser call page at `/talk/{token}`:

* **Off by default, at two levels.** The installation must set `VA_PUBLIC_CALLS_ENABLED=true`, and an editor must enable the link for a specific agent (`POST …/agents/{agent_id}/public-link?enabled=true`).
* Enabling generates a random token (`secrets.token_urlsafe(24)`) and stores only its SHA-256. Re-enabling rotates the token. Disabling clears it.
* `GET /api/public/talk/{token}` returns the agent name, business name, brand colour, a short description, an AI notice and whether recording is on. It is rate limited to 60 requests per minute per IP.
* `POST /api/public/talk/{token}/sessions` starts a WebRTC session without signing in (channel `public_link`). It is rate limited to 5 sessions per 10 minutes per IP and counts toward `VA_MAX_VOICE_SESSIONS`.
* An unknown, disabled or rotated token, a deactivated agent, or a deleted workspace all return 404.
* All services bind to localhost, so to give customers a link you must put the dashboard behind your own HTTPS reverse proxy. Add its hostname to `VA_ALLOWED_HOSTS` and `VA_CORS_ORIGINS`, and set `VA_COOKIE_SECURE=true`. Browsers only allow microphone access on HTTPS or localhost.

## Campaign scheduler

`backend/app/scheduler/worker.py` is a persistent job queue in SQLite. It runs as its own process (`python -m app.scheduler.main`), started by `scripts/start.sh`. Every 2 seconds it ticks: activate, enqueue, dispatch, complete.

* **Singleton lock.** At start-up the scheduler writes a heartbeat into `system_state` (key `scheduler`). A second scheduler refuses to start (exit code 2) while another holds the lock with a heartbeat less than 20 seconds old. The heartbeat is refreshed on every tick and is shown in system health.
* **Restart recovery.** After taking the lock, the scheduler re-queues every job left `running` by another worker. Any call those jobs had in progress is marked `failed` with `end_reason = interrupted_by_restart`, and the campaign contact goes back to `queued`.
* **Activation.** Campaigns in `scheduled` status start when `schedule_start_at` passes. Campaigns past `schedule_end_at` are completed, and their remaining contacts and jobs are cancelled. A running campaign completes when it has no pending, queued or in-progress contacts.
* **Fail-closed eligibility.** Before a job is created, each campaign contact goes through `check_eligibility` (see [COMPLIANCE.md](COMPLIANCE.md)). If only the calling window blocks it, `next_attempt_at` is set to the next window opening. If frequency caps block it, the contact is retried a day later. Any other failure marks it `skipped` (or `exhausted` for attempts) with the reasons and writes a `compliance.call_blocked` audit entry.
* **No duplicate jobs.** Each job has a unique `dedupe_key` (`cc:{campaign_contact_id}:attempt:{n}`) and is inserted with `INSERT … ON CONFLICT DO NOTHING`.
* **Atomic claims.** Jobs are claimed with `UPDATE scheduled_jobs SET status='running', locked_by=… WHERE id=? AND status='pending'`. A job only runs if exactly one row changed.
* **Concurrency.** Per-campaign `max_concurrent_calls` limits how many jobs are enqueued. `VA_TELEPHONY_CONCURRENCY` limits how many are dispatched at once.
* **Retries.** `no_answer`, `busy` and `failed` calls are retried after `retry_delay_minutes` until `max_attempts` is reached. After that the contact is `exhausted`.
* **Auto-pause.** If a call fails with a provider error (for example Ollama is down), the contact's attempt is **not** used up and the contact is retried in 2 minutes. The campaign's `consecutive_failures` counter is incremented atomically in SQL. After 3 consecutive provider failures the campaign is paused, with a reason and a system event. A successful job resets the counter. A running `PSTN` campaign is paused if real telephony has been disabled.
* **Retention.** Roughly every 30 minutes the scheduler deletes recordings, transcripts and messages older than each workspace's retention policy.
* **Pause and cancel** (from the API) cancel pending jobs, so nothing queued is dialled after a pause.

## Data model

The ORM models are in `backend/app/database/models.py`. The schema is created by the Alembic migration `backend/app/database/migrations/versions/0001_initial_schema.py`. SQLite runs in WAL mode with foreign keys enabled. The migration creates 28 tables plus an FTS5 virtual table.

| Area | Tables |
|---|---|
| Identity | `users`, `sessions` (stores the SHA-256 of the session token, the CSRF token, expiry, last-seen time and revocation), `roles` |
| Tenancy | `workspaces` (profile, timezone, default country, `compliance` policy JSON, `settings`), `workspace_memberships` (role per user per workspace) |
| Agents | `agents` (instructions, greeting, knowledge, LLM/STT/TTS settings, limits, public-link token hash, version), `agent_versions` (snapshots for restore), `agent_tools` (permitted tools) |
| CRM and compliance | `contacts` (E.164 phone, status, DND status and check date, `is_fictional`), `contact_custom_fields`, `consent_records`, `suppression_entries`, `import_jobs` |
| Campaigns | `campaigns`, `campaign_contacts` (per-contact status, attempts, next attempt, skip reason), `scheduled_jobs` (unique `dedupe_key`, lock fields) |
| Calls | `calls` (mode, channel, direction, status, timing, outcome, summary, sentiment, error, metrics), `conversations` (system prompt, tool log, effects), `messages` (per-turn entries with metrics and an interrupted flag), `transcripts`, `recordings` (relative path, SHA-256), `callback_requests` |
| Knowledge | `knowledge_documents`, `knowledge_chunks`, and the FTS5 table `knowledge_fts` (kept in sync by triggers and filtered by `workspace_id`) |
| System | `provider_configs` (global provider settings; secrets in `secrets_encrypted`), `audit_logs`, `system_events`, `system_state` (scheduler heartbeat) |

Files live under `data/`. Recordings are stored as `data/recordings/<workspace_id>/<call_id>.wav`. Uploads and backups have their own directories, and models are in `data/models/`.

## Processes and ports

`scripts/start.sh` starts these processes, each bound to `127.0.0.1`, and records their PIDs in `.run/`. `scripts/stop.sh` stops only processes whose PID file signature matches.

| Process | Port | Started by |
|---|---|---|
| Next.js dashboard (`next start`, or `next dev` with `--dev`) | 3000 | `start.sh` |
| FastAPI API (`uvicorn app.main:app`) | 8000 | `start.sh` |
| Scheduler (`python -m app.scheduler.main`) | none | `start.sh` |
| whisper.cpp `whisper-server` | 8178 | `start.sh` |
| Piper `python -m piper.http_server` (from `.tools/piper`) | 5005 | `start.sh` |
| Ollama | 11434 | Your existing Ollama. `start.sh` launches `ollama serve` on 127.0.0.1 only if Ollama is not already reachable. |

WebRTC media also uses ephemeral UDP ports that aiortc picks for its ICE host candidates. No STUN or TURN servers are configured. When real telephony is enabled, RTP from Asterisk uses the configured port range (default 40000–40100).
