#!/usr/bin/env bash
# Isolated E2E backend: prepare data, then run the scheduler and API together.
set -euo pipefail
DATA="$1"; PORT="$2"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
"$HERE/prepare.sh" "$DATA"
cd "$ROOT/backend"
"$ROOT/backend/.venv/bin/python" -m app.scheduler.main &
SCHED=$!
trap 'kill "$SCHED" 2>/dev/null || true' EXIT INT TERM
"$ROOT/backend/.venv/bin/uvicorn" app.main:app --host 127.0.0.1 --port "$PORT"
