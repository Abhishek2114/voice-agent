# REST API

The dashboard is a client of a JSON REST API served by FastAPI on `http://127.0.0.1:8000`. The dashboard reaches it through its same-origin proxy at `http://localhost:3000/api/*`. This page covers authentication, conventions, every route, and the WebRTC signalling used for voice calls.

**Interactive documentation (OpenAPI/Swagger):** <http://127.0.0.1:8000/api/docs>, with the schema at `/api/openapi.json`. Both are disabled when `VA_ENV=production`. The route table below was generated from the running application (`app.openapi()`) and lists all 116 operations.

## Contents

- [Authentication](#authentication)
- [Conventions](#conventions)
- [Errors](#errors)
- [Routes](#routes)
- [Voice sessions: WebRTC signalling](#voice-sessions-webrtc-signalling)
- [Data-channel protocol](#data-channel-protocol)

## Authentication

The API uses **cookie sessions with a CSRF token**. There are no API keys.

1. `POST /api/auth/login` with `{"email": "...", "password": "..."}` (or `POST /api/auth/setup` on first run). The response sets two cookies and returns the CSRF token in the body:
   * `va_session`: the session token (HttpOnly, SameSite=Lax; `Secure` when `VA_COOKIE_SECURE=true`).
   * `va_csrf`: the CSRF token (readable by JavaScript).
2. Send the session cookie with every request.
3. On every **state-changing** request (`POST`, `PUT`, `PATCH`, `DELETE`), also send the header **`x-csrf-token: <value of va_csrf>`**. A missing or wrong token returns `403 csrf_failed`. `GET` requests do not need it.
4. If a browser sends an `Origin` header, it must be an allowed origin (`VA_CORS_ORIGINS`) or the API's own host, otherwise the request gets `403 bad_origin`. The `Host` header must be in `VA_ALLOWED_HOSTS`.

Sessions expire after 72 hours (absolute) or 12 hours of inactivity. `GET /api/auth/status` reports whether setup is needed and whether you are signed in, and returns the CSRF token for an active session.

Example with `curl`:

```bash
curl -s -c jar.txt -H 'content-type: application/json' \
  -d '{"email":"you@example.com","password":"your-password"}' \
  http://127.0.0.1:8000/api/auth/login
CSRF=$(awk '$6=="va_csrf"{print $7}' jar.txt)

curl -s -b jar.txt http://127.0.0.1:8000/api/workspaces
curl -s -b jar.txt -H "x-csrf-token: $CSRF" -H 'content-type: application/json' \
  -d '{"name":"Acme"}' http://127.0.0.1:8000/api/workspaces
```

**Authorisation.** Routes under `/api/workspaces/{workspace_id}/…` require membership of that workspace with at least the role shown in the tables (viewer < editor < admin < owner). If you are not a member, or a referenced resource belongs to another workspace, the response is **404**, not 403. A member whose role is too low gets `403 forbidden`. Routes marked *instance admin* require the installation administrator flag.

## Conventions

* JSON request and response bodies, UTF-8. Uploads use `multipart/form-data` with a `file` field.
* IDs are opaque strings. Timestamps are ISO 8601 in UTC.
* List endpoints that page return `{"items": [...], "total": N}` and accept `page` (1-based). Most use 50 items per page. Some also accept `page_size`; for example, `GET …/calls` defaults to 25 and allows up to 200. See the OpenAPI schema for each endpoint's parameters and filters.
* `/api/*` responses are sent with `Cache-Control: no-store`.
* Request bodies larger than 64 MB are refused (`413`). Upload endpoints have their own smaller limit (`VA_MAX_UPLOAD_MB`, default 10 MB).

## Errors

Every error has the same shape. `detail` is kept for compatibility with FastAPI clients, and `error.code` is a stable machine-readable identifier:

```json
{
  "detail": "Workspace not found",
  "error": { "code": "not_found", "message": "Workspace not found" }
}
```

Some errors add fields inside `error` (for example `duplicate_id` on `409 duplicate_contact`). Validation errors return `422` with code `validation_error` and a list of field errors:

```json
{
  "detail": "name: String should have at least 1 character",
  "error": {
    "code": "validation_error",
    "message": "name: String should have at least 1 character",
    "errors": [{ "loc": ["body", "name"], "msg": "String should have at least 1 character" }]
  }
}
```

Common codes:

| Status | Codes |
|---|---|
| 400 | `bad_request`, `weak_password`, `invalid_phone`, `unsafe_url`, `pstn_disabled`, `pstn_prerequisites`, `no_calling_window`, `public_calls_disabled`, `webrtc_error`, `agent_inactive`, `remote_model_refused`, `invalid_backup` |
| 401 | `unauthenticated`, `invalid_credentials` |
| 403 | `forbidden`, `csrf_failed`, `bad_origin` |
| 404 | `not_found` |
| 409 | `conflict`, `already_setup`, `duplicate_contact` |
| 413 | `too_large` |
| 422 | `validation_error` |
| 429 | `rate_limited` |
| 503 | `capacity`, `llm_unavailable`, `model_not_found`, `stt_unavailable`, `tts_unavailable`, `vad_unavailable`, plus provider error codes such as `llm_timeout` |

## Routes

`{ws}` stands for `{workspace_id}`. "Session" means any signed-in user.

### Health and authentication

| Method | Path | Access | Purpose |
|---|---|---|---|
| GET | `/api/health` | Public | Liveness: `{"status","database"}`. Reveals no data. |
| GET | `/api/auth/status` | Public | Setup required? Signed in? CSRF token |
| POST | `/api/auth/setup` | Public, once | Create the first (administrator) user and workspace |
| POST | `/api/auth/login` | Public, rate limited | Sign in |
| POST | `/api/auth/logout` | Session | Revoke this session |
| POST | `/api/auth/logout-all` | Session | Revoke all of your sessions |
| GET | `/api/auth/me` | Session | Current user, CSRF token, workspaces and roles |

### Users

| Method | Path | Access | Purpose |
|---|---|---|---|
| PATCH | `/api/users/me` | Session | Update your profile |
| POST | `/api/users/me/password` | Session | Change your password (revokes your other sessions) |
| GET | `/api/users/me/sessions` | Session | Your active sessions |
| GET | `/api/users` | Instance admin | List users |
| POST | `/api/users` | Instance admin | Create a user |
| PATCH | `/api/users/{user_id}` | Instance admin | Activate or deactivate a user, set admin flag |

### Workspaces

| Method | Path | Access | Purpose |
|---|---|---|---|
| GET | `/api/workspaces` | Session | Workspaces you belong to |
| POST | `/api/workspaces` | Session | Create a workspace (you become owner) |
| POST | `/api/workspaces/import` | Session | Import a workspace JSON as a new workspace |
| GET | `/api/workspaces/{ws}` | viewer | Workspace profile |
| PATCH | `/api/workspaces/{ws}` | admin | Update profile, timezone, default country, branding |
| DELETE | `/api/workspaces/{ws}` | owner | Soft-delete the workspace |
| POST | `/api/workspaces/{ws}/logo` | admin | Upload logo (PNG/JPEG/WebP, content-sniffed) |
| GET | `/api/workspaces/{ws}/logo` | viewer | Logo image |
| GET | `/api/workspaces/{ws}/members` | viewer | Members and roles |
| POST | `/api/workspaces/{ws}/members` | admin | Add a member |
| PATCH | `/api/workspaces/{ws}/members/{member_id}` | admin | Change a role (the last owner is protected) |
| DELETE | `/api/workspaces/{ws}/members/{member_id}` | admin | Remove a member |
| GET | `/api/workspaces/{ws}/compliance` | viewer | Compliance policy and missing PSTN prerequisites |
| PUT | `/api/workspaces/{ws}/compliance` | admin | Replace the compliance policy (validated, audited) |
| GET | `/api/workspaces/{ws}/export` | admin | Export the workspace as JSON |

### Agents

| Method | Path | Access | Purpose |
|---|---|---|---|
| GET | `/api/workspaces/{ws}/agents` | viewer | List agents |
| POST | `/api/workspaces/{ws}/agents` | editor | Create an agent |
| GET | `/api/workspaces/{ws}/agents/tools` | viewer | Tool catalogue (registered tools) |
| GET | `/api/workspaces/{ws}/agents/{agent_id}` | viewer | Agent detail |
| PUT | `/api/workspaces/{ws}/agents/{agent_id}` | editor | Update (creates a new version) |
| DELETE | `/api/workspaces/{ws}/agents/{agent_id}` | editor | Delete |
| POST | `/api/workspaces/{ws}/agents/{agent_id}/activate?active=true\|false` | editor | Activate or deactivate |
| POST | `/api/workspaces/{ws}/agents/{agent_id}/duplicate` | editor | Duplicate |
| GET | `/api/workspaces/{ws}/agents/{agent_id}/versions` | viewer | Version history |
| POST | `/api/workspaces/{ws}/agents/{agent_id}/versions/{version}/restore` | editor | Restore a version |
| POST | `/api/workspaces/{ws}/agents/{agent_id}/public-link?enabled=true\|false` | editor | Enable/rotate or disable the customer link. Returns the token once. |
| POST | `/api/workspaces/{ws}/agents/{agent_id}/test` | editor | Text test console (real model, dry-run tools) |

### Knowledge base

| Method | Path | Access | Purpose |
|---|---|---|---|
| GET | `/api/workspaces/{ws}/knowledge` | viewer | List documents |
| POST | `/api/workspaces/{ws}/knowledge` | editor | Create a text or FAQ document |
| POST | `/api/workspaces/{ws}/knowledge/upload` | editor | Upload PDF, text, Markdown or CSV (content-sniffed) |
| GET | `/api/workspaces/{ws}/knowledge/search?q=` | viewer | Full-text search (FTS5/BM25) |
| GET | `/api/workspaces/{ws}/knowledge/{doc_id}` | viewer | Document |
| PUT | `/api/workspaces/{ws}/knowledge/{doc_id}` | editor | Update and re-index |
| DELETE | `/api/workspaces/{ws}/knowledge/{doc_id}` | editor | Delete |

### Contacts and imports

| Method | Path | Access | Purpose |
|---|---|---|---|
| GET | `/api/workspaces/{ws}/contacts` | viewer | Search, filter and page contacts |
| POST | `/api/workspaces/{ws}/contacts` | editor | Create (phone normalised, duplicates refused) |
| GET | `/api/workspaces/{ws}/contacts/export` | admin | CSV export (formula-escaped) |
| GET | `/api/workspaces/{ws}/contacts/duplicates` | viewer | Possible duplicates |
| POST | `/api/workspaces/{ws}/contacts/bulk-tags` | editor | Add or remove tags in bulk |
| GET | `/api/workspaces/{ws}/contacts/{contact_id}` | viewer | Contact |
| PATCH | `/api/workspaces/{ws}/contacts/{contact_id}` | editor | Update |
| DELETE | `/api/workspaces/{ws}/contacts/{contact_id}` | editor | Soft delete |
| POST | `/api/workspaces/{ws}/contacts/{contact_id}/erase?keep_suppression=true` | admin | Erasure workflow (see [COMPLIANCE.md](COMPLIANCE.md#erasure-workflow)) |
| GET | `/api/workspaces/{ws}/contacts/{contact_id}/history` | viewer | Calls, consents, callbacks, events, suppression |
| GET | `/api/workspaces/{ws}/imports` | viewer | Import jobs |
| POST | `/api/workspaces/{ws}/imports` | editor | Upload a CSV (returns headers and suggested mapping) |
| POST | `/api/workspaces/{ws}/imports/{job_id}/validate` | editor | Validate with a column mapping and preview |
| POST | `/api/workspaces/{ws}/imports/{job_id}/commit` | editor | Commit the import |
| DELETE | `/api/workspaces/{ws}/imports/{job_id}` | editor | Discard |

### Compliance

| Method | Path | Access | Purpose |
|---|---|---|---|
| GET | `/api/workspaces/{ws}/compliance/consents` | viewer | Consent records |
| POST | `/api/workspaces/{ws}/compliance/consents` | editor | Record consent (purpose, source, evidence, dates) |
| POST | `/api/workspaces/{ws}/compliance/consents/{consent_id}/revoke` | editor | Revoke consent |
| GET | `/api/workspaces/{ws}/compliance/suppression` | viewer | Suppression (do-not-call) list |
| POST | `/api/workspaces/{ws}/compliance/suppression` | editor | Add a number (reason `opt_out` also revokes consent) |
| DELETE | `/api/workspaces/{ws}/compliance/suppression/{entry_id}` | admin | Remove a number |
| POST | `/api/workspaces/{ws}/compliance/dnd-scrub` | admin | Import DND scrub results |
| POST | `/api/workspaces/{ws}/compliance/eligibility` | viewer | Run the eligibility checks for a contact |
| GET | `/api/workspaces/{ws}/compliance/audit` | admin | Workspace audit log |

### Campaigns

| Method | Path | Access | Purpose |
|---|---|---|---|
| GET | `/api/workspaces/{ws}/campaigns` | viewer | List with progress |
| POST | `/api/workspaces/{ws}/campaigns` | editor | Create (`mode`: `SIMULATION` default, or `PSTN` if enabled) |
| GET | `/api/workspaces/{ws}/campaigns/{campaign_id}` | viewer | Detail |
| PUT | `/api/workspaces/{ws}/campaigns/{campaign_id}` | editor | Update |
| DELETE | `/api/workspaces/{ws}/campaigns/{campaign_id}` | editor | Delete |
| POST | `/api/workspaces/{ws}/campaigns/{campaign_id}/contacts` | editor | Add contacts (by IDs or filter) |
| GET | `/api/workspaces/{ws}/campaigns/{campaign_id}/contacts` | viewer | Campaign contacts and their status |
| POST | `/api/workspaces/{ws}/campaigns/{campaign_id}/preview` | viewer | Compliance preview: who is eligible and why not |
| POST | `/api/workspaces/{ws}/campaigns/{campaign_id}/start` | editor | Start (or schedule) |
| POST | `/api/workspaces/{ws}/campaigns/{campaign_id}/pause` | editor | Pause (pending jobs cancelled) |
| POST | `/api/workspaces/{ws}/campaigns/{campaign_id}/resume` | editor | Resume |
| POST | `/api/workspaces/{ws}/campaigns/{campaign_id}/cancel` | editor | Cancel |
| GET | `/api/workspaces/{ws}/campaigns/{campaign_id}/report` | viewer | Outcome report |

### Calls and callbacks

| Method | Path | Access | Purpose |
|---|---|---|---|
| GET | `/api/workspaces/{ws}/calls` | viewer | List and search calls (`mode`, `status`, `outcome`, `agent_id`, …) |
| GET | `/api/workspaces/{ws}/calls/export` | admin | CSV export |
| GET | `/api/workspaces/{ws}/calls/{call_id}` | viewer | Detail: messages, tool log, summary, metrics, recording info |
| DELETE | `/api/workspaces/{ws}/calls/{call_id}` | admin | Delete a call and its recording |
| GET | `/api/workspaces/{ws}/calls/{call_id}/recording` | editor | Recording WAV (audited) |
| POST | `/api/workspaces/{ws}/calls/simulate` | editor | Start a simulated call (`202`) |
| GET | `/api/workspaces/{ws}/callbacks` | viewer | Callback and appointment requests |
| PATCH | `/api/workspaces/{ws}/callbacks/{callback_id}` | editor | Update status |

### Analytics and telephony

| Method | Path | Access | Purpose |
|---|---|---|---|
| GET | `/api/workspaces/{ws}/analytics/overview` | viewer | Dashboard metrics |
| GET | `/api/workspaces/{ws}/analytics/report` | viewer | Report: calls over time, outcomes, durations, agents, campaigns, leads |
| GET | `/api/workspaces/{ws}/telephony/status` | viewer | Available modes, provider health, missing PSTN prerequisites |
| POST | `/api/workspaces/{ws}/telephony/simulate-inbound` | editor | Simulated inbound call (`202`) |

### Voice sessions and public links

| Method | Path | Access | Purpose |
|---|---|---|---|
| POST | `/api/workspaces/{ws}/voice/sessions` | editor | Start a browser voice session: SDP offer in, answer out |
| GET | `/api/workspaces/{ws}/voice/sessions` | viewer | Live sessions in this workspace |
| POST | `/api/workspaces/{ws}/voice/sessions/{call_id}/control` | editor | `{"type":"interrupt"\|"mute"\|"end","muted"?}` |
| GET | `/api/public/talk/{token}` | Public, 60/min per IP | Agent and business name, AI notice, recording flag |
| POST | `/api/public/talk/{token}/sessions` | Public, 5 per 10 min per IP | Start a public voice session |
| POST | `/api/public/talk/{token}/sessions/{call_id}/end` | Public (token) | End a public session |

Public routes return 404 unless `VA_PUBLIC_CALLS_ENABLED=true` and the agent's link is enabled.

### System (installation-wide)

| Method | Path | Access | Purpose |
|---|---|---|---|
| GET | `/api/system/health` | Session | Database, scheduler, LLM/STT/TTS/VAD/telephony health, voice slots, resources |
| GET | `/api/system/diagnostics` | Instance admin | Platform, settings, model files, recent events |
| GET | `/api/system/events?hours=` | Instance admin | System events |
| GET | `/api/system/providers` | Session | Provider configuration (secrets reported only as `has_secrets`) |
| PUT | `/api/system/providers/{kind}` | Instance admin | Configure `llm`, `stt`, `tts`, `telephony` or `audio` (URLs SSRF-checked) |
| GET | `/api/system/models` | Session | Ollama models with local/remote flag |
| POST | `/api/system/models/test` | Session | Short test prompt, reports time to first token |
| POST | `/api/system/models/warmup` | Session | Load a model into memory |
| GET | `/api/system/voices?provider=` | Session | TTS voices |
| POST | `/api/system/voices/preview` | Session | Synthesise a preview (returns `audio/wav`) |
| GET | `/api/system/backups` | Instance admin | List backups and whether a restore is pending |
| POST | `/api/system/backups` | Instance admin | Create a backup (optional `passphrase` form field, ≥ 10 characters) |
| GET | `/api/system/backups/{name}` | Instance admin | Download a backup |
| POST | `/api/system/backups/restore` | Instance admin | Upload, verify and stage a restore |

## Voice sessions: WebRTC signalling

Voice calls use WebRTC between the browser and the backend (aiortc). Signalling is a single HTTP request. ICE is **not trickled**: the client waits for candidate gathering (the dashboard waits up to 2.5 s) and sends a complete offer. **No STUN or TURN servers** are used on either side.

```text
Browser                                               API (/api/workspaces/{ws}/voice/sessions)
  getUserMedia({audio: echoCancellation, noiseSuppression, autoGainControl})
  pc = new RTCPeerConnection({iceServers: []})
  pc.createDataChannel("events")        ── the browser creates the channel
  pc.addTrack(mic)
  offer = pc.createOffer(); setLocalDescription; wait for ICE gathering
  POST {agent_id, sdp, type:"offer", contact_id?}  ───────────►  preflight (model installed and local,
       (cookie + x-csrf-token)                                     STT/TTS/VAD healthy, capacity)
                                                                  create call (mode WEBRTC)
                                                                  accept offer (audio + data channel only)
  ◄────────────────────────────────────  201 {call_id, sdp, type:"answer"}
  setRemoteDescription(answer)
  ◄══════════ Opus audio both ways; JSON on the "events" data channel ══════════►
  agent greeting plays; conversation runs
  end: send {"type":"end"} on the channel, or POST …/control {"type":"end"}
```

Notes:

* The offer may contain only **audio** and **application** (data-channel) media sections. Offers with video are rejected (`400 webrtc_error`).
* `sdp` is limited to 20,000 characters. If the peer connection is not established within 30 seconds, the call ends with reason `connection_timeout`.
* Failure responses use the error format above. Examples: `503 capacity` when all `VA_MAX_VOICE_SESSIONS` slots are busy, `503 model_not_found` when the agent's model is not installed, `400 remote_model_refused`, `400 agent_inactive`.
* Public links use the same flow with `POST /api/public/talk/{token}/sessions` and body `{sdp, type:"offer"}`. No cookie or CSRF token is needed.
* The call record (transcript, summary, outcome, metrics) is available from `GET …/calls/{call_id}` after the session ends.

## Data-channel protocol

All messages are JSON objects with a `type` field. Messages longer than 8 KB, and messages that are not JSON objects, are ignored.

### Server → browser events

| `type` | Fields | Meaning |
|---|---|---|
| `state` | `state`: `listening` \| `thinking` \| `speaking` \| `waiting` \| `ended`; optional `detail` | Pipeline state changed. `waiting` (with `detail: "waiting for local AI capacity"`) means another conversation holds the AI slot. |
| `user_speech` | `active` (bool); `duration_ms` when `active` is false | VAD detected the start or end of user speech |
| `transcript` | `role`: `user` \| `assistant`; `text`; `final: true`; optional `metrics` (for example `stt_ms`, `llm_ttft_ms`, `tts_first_ms`, `first_audio_ms`, `llm_total_ms`); optional `interrupted: true` | A finished user turn (after STT) or a finished agent reply. An interrupted reply contains only the sentences that were actually played. |
| `agent_delta` | `text` | Streamed LLM text as it is generated (before sentence splitting and speech) |
| `interrupted` | `source`: `barge_in` \| `user` \| `new_turn`; `dropped_ms`; `latency_ms` | Agent audio was stopped and generation cancelled |
| `tool` | `tool`, `arguments`, `ok`, `message`, `effects`, `data` | The agent used a tool |
| `error` | `code`, `message`, `retryable`, `stage` (`stt`, `response`, `tts`), `recoverable` | A provider error. Non-recoverable errors are followed by the end of the call. |
| `ended` | `reason` (for example `user_ended`, `user_goodbye`, `agent_ended`, `silence_timeout`, `max_duration`, `max_turns`, `provider_error`, `connection_timeout`) | The conversation is over |
| `muted` | `muted` (bool) | Acknowledges a mute change |
| `pong` | `t` (echoed) | Reply to `ping` |

Events sent before the data channel opens are buffered (up to 500) and flushed when it opens.

### Browser → server control messages

| `type` | Fields | Effect |
|---|---|---|
| `interrupt` | — | Stop agent audio immediately and cancel the reply in progress |
| `mute` | `muted` (bool) | Stop or resume feeding microphone audio to the VAD (state is reset while muted). Answered with a `muted` event. |
| `end` | — | End the call (reason `user_ended`) |
| `ping` | `t` (any value) | Answered with `pong` carrying the same `t` |

`interrupt`, `mute` and `end` can also be sent over HTTP: `POST /api/workspaces/{ws}/voice/sessions/{call_id}/control`.
