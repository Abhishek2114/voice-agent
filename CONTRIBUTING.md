# Contributing to voice-agent

Thank you for helping. This guide covers the development setup, the code layout, how to run the checks, the rules every change must follow, and how to submit work.

## Development setup

You need an Apple Silicon Mac (Intel works without Metal acceleration) with Homebrew, Node.js 20+, uv and Ollama (with at least one local model). See [docs/INSTALLATION.md](docs/INSTALLATION.md) for details.

```bash
git clone <your fork url> voice-agent && cd voice-agent
./scripts/setup-mac.sh --models     # backend venv (Python 3.12), Piper, npm packages, DB, speech models
./scripts/start.sh --dev            # everything on 127.0.0.1, dashboard with hot reload (or: make dev)
./scripts/health-check.sh
```

* Dashboard: http://localhost:3000 · API docs: http://127.0.0.1:8000/api/docs · Logs: `logs/`
* The backend environment is `backend/.venv`. The setup script adds the repository root to its import path, so both `app` and `engine` import.
* Use **npm** for the dashboard (there is a `package-lock.json`). Python 3.14 is not supported.
* To run only the API with auto-reload while developing:

  ```bash
  cd backend && PYTHONPATH="$PWD:$PWD/.." .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
  ```

## Code layout

```text
engine/              transport-independent conversation engine (never imports the backend)
  audio/             PCM utilities, Silero/energy VAD + speech segmenter, call recorder
  conversation/      VoicePipeline, ConversationSession, prompt, sentence splitter, tools, post-call analysis
  providers/         llm/ stt/ tts/ telephony/ — Protocol interfaces (base.py) and adapters
  streaming/         QueueAudioSink, WebRTC transport (aiortc)
backend/
  app/               FastAPI application, one package per area:
    auth/ users/ workspaces/ agents/ contacts/ imports/ knowledge/ compliance/
    campaigns/ calls/ audio/ (voice sessions) telephony/ analytics/ system/
    ai/hub.py        builds provider adapters from settings and stored configuration
    scheduler/       persistent campaign scheduler (separate process)
    core/            config, errors, security, SSRF guard, rate limiter, logging, audit, file sniffing
    database/        models, session, Alembic migrations
  tests/             API-level tests
apps/dashboard/      Next.js 16 dashboard (src/app, src/components, src/lib), vitest tests, Playwright e2e/
tests/               unit/ (engine), integration/ (real models, WebRTC), security/
scripts/             setup, start, stop, health check, backup
demo/shrotihost/     sample workspace (fictional data)
docs/                architecture, installation, security, compliance, telephony, API, licensing
```

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) explains how the pieces fit together.

## Running tests, lint and type checks

| Task | Make | Direct command |
|---|---|---|
| All tests | `make test` | — |
| Backend, engine and security tests | `make test-backend` | `backend/.venv/bin/python -m pytest backend/tests tests` |
| Dashboard unit tests | `make test-frontend` | `cd apps/dashboard && npm test` |
| Real-model tests (needs `./scripts/start.sh`) | `make test-real` | `backend/.venv/bin/python -m pytest tests/integration/test_real_providers.py -m "ollama or speech" -s` |
| End-to-end (Chrome, isolated instance on :8100/:3100) | `make test-e2e` | `cd apps/dashboard && npx playwright test` |
| Lint | `make lint` | `backend/.venv/bin/ruff check backend/app engine tests backend/tests`; `cd apps/dashboard && npm run lint` |
| Type check | `make typecheck` | `cd backend && .venv/bin/mypy app ../engine`; `cd apps/dashboard && npx tsc --noEmit` |
| Migrations match models | `make migrations-check` | — |
| Security scans | `make security` | bandit, pip-audit, npm audit, gitleaks |
| Format Python | `make format` | `ruff format` + `ruff check --fix` |

Notes:

* The pytest configuration (`pytest.ini`) gives each test an isolated temporary data directory and a freshly migrated SQLite database, and injects fake providers (`conftest.py`). The normal suite never touches your real `data/` and does not need Ollama.
* Tests marked `ollama` or `speech` use real local services and skip themselves when those are not running. CI runs everything else (see `.github/workflows/ci.yml`).
* The end-to-end suite starts its own API and dashboard against a temporary data directory, and uses the shared local AI services.

Every pull request should pass `make lint`, `make typecheck` and `make test`. If you change the database models, add an Alembic migration and check it with `make migrations-check`.

## Conventions and rules

### Style

* **Python**: ruff for linting and formatting (line length 120; rules in `backend/pyproject.toml`), type hints throughout, mypy clean. Async all the way down in request handlers.
* **TypeScript**: ESLint (Next.js config) and `tsc --noEmit` clean. Follow the existing component patterns (shadcn/ui on Base UI, TanStack Query for data, React Hook Form + Zod for forms).
* Keep user-facing text plain and specific. Errors should say what happened and what to do next.

### Workspace scoping (mandatory)

Every tenant-owned table has a `workspace_id`. In every backend change:

* Take the workspace from the URL through a role dependency (`ws_viewer`, `ws_editor`, `ws_admin`, `ws_owner` in `app/auth/deps.py`). **Never** trust a `workspace_id` from a request body.
* **Every query filters on `workspace_id`.** When loading by primary key, check `obj.workspace_id == ctx.id`.
* A resource from another workspace must return **404** (`not_found(...)`), never 403 and never the data. This avoids revealing that it exists.
* Add a test to `tests/security/test_workspace_isolation.py` for any new resource type.

### Agent tools

* New tools must be **registered in `default_registry()`** (`engine/conversation/tools.py`) with a **strict JSON schema**: an object, explicit `properties`, `maxLength` on strings, `enum` where values are fixed, and a `required` list. The registry rejects unknown arguments.
* A tool may only record a request or change data inside the call's workspace through the `ToolHost` interface. Implement that in `DbToolHost` (`backend/app/calls/service.py`) and make it **write nothing real in `SIMULATION` mode**. Also implement the agent test console's dry-run host.
* Tools must not run shell commands, fetch URLs, send messages, or complete bookings, payments or refunds. Results must tell the model that requests await human follow-up.

### Local-only AI and network access

* **No cloud AI calls.** LLM, STT and TTS must run locally. Never add a silent cloud fallback. If a provider can be remote, report `is_local=False` and refuse it by default.
* **No network fetches of user-supplied URLs.** The only configurable outbound endpoints are provider URLs, which must pass `validate_provider_url` (`app/core/ssrf.py`).
* New services bind to `127.0.0.1`.

### Security and compliance

* State-changing routes get CSRF protection automatically through `current_session`. Do not add routes that bypass the auth dependencies.
* Validate all input with Pydantic models (lengths, enums, patterns). Content-sniff uploads (`app/core/files.py`). Escape CSV exports (`app/contacts/csv_safety.py`).
* Write an audit entry (`app/core/audit.py`) for security- or compliance-relevant actions, in the same transaction.
* Do not log personal data or secrets. The redaction filter is a safety net, not a licence.
* Compliance checks must **fail closed**. Simulated activity must never change real contacts, suppression or consent.
* State limitations honestly in code and docs (for example "not production-verified").

### Providers

To add an STT, TTS, telephony or LLM provider, follow [docs/ARCHITECTURE.md#providers-and-how-to-add-one](docs/ARCHITECTURE.md#providers-and-how-to-add-one), and add adapter tests with mocked HTTP to `tests/unit/test_provider_adapters.py`.

## Commits and pull requests

* Branch from `main` and keep each pull request focused on one change.
* Write commit messages in the imperative mood with a short summary line (≤ 72 characters), for example `Add per-campaign retry jitter`, and explain *why* in the body when it is not obvious.
* In the pull request description, say what changed and why, how you tested it (commands and results, including real-model or e2e runs where relevant), and any migrations, new settings (`VA_*`, which also go into `.env.example` and docs), or security and compliance implications.
* Include screenshots for UI changes (light and dark).
* Update the relevant docs in `docs/` and add an entry under "Unreleased" in [CHANGELOG.md](CHANGELOG.md).
* Do not commit `.env`, `data/`, model files, logs, or build output. `.gitignore` covers these, so check `git status` before committing.
* Report security issues privately, not in a pull request. See [SECURITY.md](SECURITY.md).

## Licensing of contributions

voice-agent is licensed under the [MIT License](LICENSE). By submitting a contribution, you agree that it is licensed under the MIT License, and you confirm that you have the right to submit it.

* Do not copy code from sources with incompatible licences.
* **Piper TTS is GPL-3.0** and must stay a **separate process** reached over HTTP. Do not import `piper` from the backend or engine, and do not vendor it. See [docs/MODEL-LICENSING.md](docs/MODEL-LICENSING.md).
* Do not commit model weights. If you add a model or voice to the setup script, check its licence (and its training data's licence; non-commercial licences are not acceptable as defaults) and document it in `docs/MODEL-LICENSING.md`.
* New dependencies must have licences compatible with MIT distribution. Note any that do not in the pull request.
