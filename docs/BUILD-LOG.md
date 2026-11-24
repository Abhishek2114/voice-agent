# Build log

A running record of what was built, how it was verified, and what remains. The newest entries are at the bottom.

## Phase 0: Inspect (done)

* Inspected hardware, OS, toolchain, Ollama and its models. See [ENVIRONMENT-REPORT.md](ENVIRONMENT-REPORT.md).
* Started Ollama bound to `127.0.0.1` (it was not running). Found that `glm-5.3:cloud` is a remote proxy model.
* Benchmarked `qwen3:4b-instruct-2507-q4_K_M`: 0.1 s warm time-to-first-token, 41 tok/s. Selected it as the default. No LLM was downloaded.
* Installed the one missing core tool, whisper.cpp (Homebrew). Installed Piper into an isolated GPL environment.
* Downloaded only small, permissively licensed speech models. Checked each voice's model card before downloading.

## Phase 1: Foundation (done)

* Monorepo layout: `engine/` (transport-independent conversation engine and providers), `backend/` (FastAPI app, scheduler, Alembic), `apps/dashboard/` (Next.js), `scripts/`, `docs/`, `tests/`.
* SQLite in WAL mode with foreign keys on. The Alembic migration `0001` creates 28 tables, an FTS5 index with sync triggers, and role seeding. Upgrade, downgrade and re-upgrade were verified, and `alembic check` is clean.
* Auth: Argon2id hashes. Sessions use a random 256-bit token in an HttpOnly cookie, with only its SHA-256 stored. Absolute and idle expiry are enforced. Double-submit CSRF is applied inside the auth dependency. Login is rate limited per account and per IP. Trusted-host and Origin checks guard against DNS rebinding.
* Workspaces with roles (owner/admin/editor/viewer), enforced in the backend. A workspace the user can't access returns 404, so its existence isn't revealed.

## Phase 2: AI agent system (done)

* Ollama adapter: model discovery with local/remote detection, remote-model refusal, first-token and inter-chunk timeouts, structured errors, JSON mode, tool calls, warm-up.
* Agent builder API: validation, version history with restore, duplicate, activate/deactivate, a public link token, and a text test console (tools run in dry-run mode).
* Conversation session: isolated per-call memory, guardrailed system prompt, knowledge retrieved per turn, and a tool loop over a registry of validated, permission-controlled tools: `end_call`, `request_callback`, `request_appointment`, `record_opt_out`, `search_knowledge`, `record_interest`, `capture_lead`.
* **Lead capture** (requested mid-build): during any real conversation the agent collects name, phone, email and requirement, and an explicit yes or no on follow-up. It creates or deduplicates a contact (`status=lead`, tag `ai-lead`), links the call, and records `inquiry_followup` consent only when the person agreed. Simulated calls never create real leads.

## Phase 3: Voice system (done; real models verified)

* Silero VAD v5 through onnxruntime (no torch), plus a speech segmenter with pre-roll, minimum speech length and end-of-turn silence.
* whisper.cpp STT (warm whisper-server, or CLI per utterance) and Piper TTS over HTTP. macOS `say` is available as an alternative local TTS.
* `VoicePipeline`: streaming LLM output, sentence-pipelined TTS, barge-in (drop queued audio and cancel generation), a manual interrupt, mute, a silence re-prompt then hang-up, maximum duration, an AI-capacity semaphore with a "waiting" state, and fallback speech on provider failure. Transcripts record only the sentences that were actually heard.
* WebRTC transport (aiortc): browser Opus in and out, a data channel for events and control, and no STUN/TURN servers (media stays local).
* **Verified with real models** (`tests/integration/test_real_providers.py`). Piper-synthesized caller speech goes through Silero, whisper.cpp, Qwen3-4B and Piper. Measured: STT 101 ms, LLM first token 3.8 s (cold model load), TTS first sentence 158 ms. The agent answered from the business knowledge.
* **Verified over real WebRTC**: an aiortc peer standing in for the browser negotiates through the API, streams audio, and receives synthesized agent audio and events (`tests/integration/test_webrtc_voice_session.py`).

## Phase 4: Business features (backend done)

* Contacts: CRUD, search, filters, pagination, tags, custom fields, duplicate detection, phone normalization (libphonenumber), history, CSV export with formula escaping, and an erasure workflow.
* CSV import: upload, sniff, parse, auto-map columns, validate, dedupe within the file and against existing contacts, preview, then commit with a report. Formula-like cells are neutralized. Consent is recorded only when purpose, source, date and evidence are all present. The raw upload is deleted after commit.
* Campaigns: create, select contacts, compliance preview, start/pause/resume/cancel, calling window, retry rules, maximum attempts, concurrency limit, reports.
* Persistent scheduler: a singleton heartbeat lock, a dedupe key per attempt, atomic job claims, recovery after restart (interrupted calls are marked), auto-pause after repeated provider failures (these don't consume contact attempts), and retention cleanup.
* Knowledge base: FAQs and text, Markdown, CSV and PDF uploads with content sniffing, chunking, and FTS5 BM25 retrieval scoped by workspace.
* Calls: list, filter and search, detail with messages and tool log, recording playback (audited, editor role or above), export, callbacks. Post-call summary and outcome come from the LLM, with deterministic overrides; if analysis fails the call shows an error rather than a guessed outcome.
* Analytics: overview, calls over time, outcomes, duration distribution, agent and campaign performance, leads captured. Everything is computed from stored records.

## Phase 5: Telephony (done as integration-ready)

* Deterministic simulator for outbound and inbound calls with scripted or LLM-driven callee personas.
* Asterisk ARI adapter (originate, Stasis events, `externalMedia` RTP bridge) and an RTP codec. Tested against a mocked ARI server only. Real PSTN calling is disabled unless `VA_REAL_TELEPHONY_ENABLED=true`, a provider is configured and enabled, and all workspace PSTN attestations are recorded.

## Test status at this checkpoint

```
backend/.venv/bin/python -m pytest backend/tests tests -q
141 passed in 30.56s   (includes 4 real-model tests; they skip when services are down)
ruff check: clean
```

Bugs found and fixed by tests so far:

* An interrupted reply was recorded as fully spoken.
* Short sentences were over-merged.
* A lost update on the campaign failure counter under concurrency (now an atomic SQL increment).
* Async lazy-load errors on new contacts.
* An Alembic env that couldn't run inside an event loop.
* A campaign window with no overlap silently skipped every contact (now rejected at start).
* `None` notes on a new lead.
* A missing `force` parameter in the provider protocol.

## Phase 4b: Dashboard (done)

* Next.js 16 / React 19 / Tailwind 4 / shadcn (Base UI). Fonts are bundled locally, with no Google requests.
  The chart palette was validated for colour-blind separation and contrast in both themes.
* Pages:
  * sign-in, first-run setup
  * overview with a live system strip
  * voice playground with two-channel meters, live transcript, mute/interrupt/end and per-turn latencies
  * agents: list, builder, test chat, simulations, versions, customer link
  * contacts, contact detail with consent and live eligibility, CSV import wizard
  * campaigns with live progress and compliance preview
  * calls and callbacks; call detail with transcript, summary, response times, audited recording
  * knowledge, analytics with a table view, compliance, settings (9 sections)
  * public "talk to us" page
* Simulated calls carry a hatched texture everywhere they appear.
* Bugs fixed:
  * Tailwind loader lost when `next.config.ts` was rewritten
  * extra exports from page modules
  * state set inside effects (refactored to loader/editor components)
  * mutable render variable in the nav

## Phase 6: Security and QA (done)

* Work was parallelised with subagents: documentation; frontend tests + CI + Makefile; security scans and triage.
* Security fixes from the scans:
  * `.next*/` build output (containing generated keys) is now git-ignored
  * WebRTC offers with non-audio media are rejected, keeping FFmpeg's video decoders unreachable
  * RTP drops packets from any peer except the latched address and SSRC
  * backup restore caps the uncompressed size and checks manifest paths
* All 46 mypy errors and 36 ruff findings were fixed.
* Fixed from browser testing:
  * a doubled greeting when the AI disclosure was prepended
  * a slow first turn, now avoided by priming the system prompt while the greeting plays
  * Playwright started its web servers before `globalSetup` and deleted the database; the E2E scheduler wasn't
    started
  * phone redaction also matched IP addresses
* Results: pytest 147/147, Vitest 87/87, Playwright 8/8 + screenshot capture (including a live voice conversation in Chrome),
  ruff/mypy/tsc clean, production build passes. See [TESTING.md](TESTING.md).
* Benchmarks ([BENCHMARKS.md](BENCHMARKS.md)): end of speech to first agent audio p50 1.06 s, p95 2.04 s;
  STT about 0.17 s; 100 % turn completion.

## Phase 7: Release preparation (done)

* README with real screenshots, docs set, MIT LICENSE, CONTRIBUTING, SECURITY, CHANGELOG, `.env.example`,
  `.gitignore`, Makefile, GitHub Actions (backend, frontend, security).
* `git init` done. **Nothing has been committed or pushed**: that is left for the owner to do after review.

## Known limitations

* Real PSTN calling (Asterisk ARI) is integration-ready but has not been verified with a live carrier. It needs an
  Asterisk server, a SIP trunk, a registered caller ID and the workspace's TRAI prerequisites.
* STT runs per turn (not streaming). TTS is pipelined by sentence (not streaming below sentence level).
* English speech models only by default. Hindi needs a multilingual whisper model and a suitably licensed Piper
  voice.
* The FFmpeg bundled in PyAV has open CVEs with no fixed release yet. The attack surface is limited to Opus audio
  from authenticated users, or from public-link visitors if enabled (off by default).
* Customer links work only when the user exposes the dashboard over HTTPS themselves.
* Browser automation was verified in Chrome only.

## External credentials still required (only for optional features)

* Real telephony: an Asterisk ARI username and password, a SIP trunk with a provider, a registered calling number
  series (140/160), and the entity/telemarketer registration with an A2P declaration.
* Nothing else. All AI runs locally with no API keys.
