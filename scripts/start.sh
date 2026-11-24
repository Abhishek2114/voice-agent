#!/usr/bin/env bash
# Start voice-agent: (Ollama if not running) → whisper-server → Piper → API → scheduler → dashboard.
# Every service binds to 127.0.0.1. Usage: ./scripts/start.sh [--dev]
set -euo pipefail
source "$(dirname "$0")/lib.sh"

MODE="prod"
[[ "${1:-}" == "--dev" ]] && MODE="dev"

[[ -x "$VENV/bin/python" ]] || { err "Backend environment missing. Run ./scripts/setup-mac.sh first."; exit 1; }
[[ -d "$ROOT/apps/dashboard/node_modules" ]] || { err "Dashboard dependencies missing. Run ./scripts/setup-mac.sh first."; exit 1; }

cd "$ROOT/backend"
export PYTHONPATH="$ROOT/backend:$ROOT"

# 0. apply a staged restore while nothing is running
if [[ -f "$ROOT/data/restore-pending/READY" ]]; then
  if pid_alive api || pid_alive scheduler; then
    warn "A restore is staged; stop the app first (./scripts/stop.sh) to apply it."
  else
    info "Applying staged backup restore…"
    "$VENV/bin/python" -m app.system.backup apply-pending
  fi
fi

# 1. Ollama (never exposed publicly; started only if not already running)
if curl -fsS -m 2 "$OLLAMA_URL/api/version" >/dev/null 2>&1; then
  ok "Ollama reachable at $OLLAMA_URL"
elif command -v ollama >/dev/null 2>&1; then
  info "Starting Ollama on 127.0.0.1:11434"
  OLLAMA_HOST=127.0.0.1:11434 start_service ollama "ollama serve" "$LOG_DIR/ollama.log" -- ollama serve
  wait_http "$OLLAMA_URL/api/version" 40 && ok "Ollama started" || warn "Ollama did not become ready (see logs/ollama.log)"
else
  warn "Ollama not installed — local AI conversations will be unavailable (explicit errors, no cloud fallback)."
fi

# 2. whisper.cpp server (speech-to-text, Metal accelerated)
if [[ -f "$MODELS/whisper/$WHISPER_MODEL" ]] && command -v whisper-server >/dev/null 2>&1; then
  if port_in_use "$WHISPER_PORT" && ! pid_alive whisper; then
    warn "Port $WHISPER_PORT busy (not ours) — skipping whisper-server"
  else
    start_service whisper "whisper-server" "$LOG_DIR/whisper.log" -- \
      whisper-server -m "$MODELS/whisper/$WHISPER_MODEL" --host 127.0.0.1 --port "$WHISPER_PORT" -t 4
    wait_http "http://127.0.0.1:$WHISPER_PORT/health" 60 && ok "whisper-server ready ($WHISPER_MODEL)" \
      || warn "whisper-server not ready (see logs/whisper.log)"
  fi
else
  warn "Speech-to-text unavailable: whisper-server or $WHISPER_MODEL missing (./scripts/setup-mac.sh --models)"
fi

# 3. Piper TTS server (GPL-3.0 — separate process, never imported by voice-agent)
if [[ -x "$PIPER_VENV/bin/python" && -f "$MODELS/piper/$PIPER_VOICE.onnx" ]]; then
  if port_in_use "$PIPER_PORT" && ! pid_alive piper; then
    warn "Port $PIPER_PORT busy (not ours) — skipping Piper"
  else
    start_service piper "piper.http_server" "$LOG_DIR/piper.log" -- \
      "$PIPER_VENV/bin/python" -m piper.http_server --host 127.0.0.1 --port "$PIPER_PORT" \
      -m "$MODELS/piper/$PIPER_VOICE.onnx" --data-dir "$MODELS/piper"
    wait_http "http://127.0.0.1:$PIPER_PORT/voices" 60 && ok "Piper ready ($PIPER_VOICE)" || warn "Piper not ready (see logs/piper.log)"
  fi
else
  warn "Piper TTS unavailable (./scripts/setup-mac.sh --models). macOS 'say' can be selected as the TTS provider."
fi

# 4. API
if port_in_use "$API_PORT" && ! pid_alive api; then
  err "Port $API_PORT is in use by another process"; exit 1
fi
start_service api "uvicorn app.main:app" "$LOG_DIR/api.log" -- \
  "$VENV/bin/uvicorn" app.main:app --host 127.0.0.1 --port "$API_PORT" --no-server-header --log-level info
wait_http "http://127.0.0.1:$API_PORT/api/health" 80 && ok "API ready on http://127.0.0.1:$API_PORT" \
  || { err "API failed to start (see logs/api.log)"; exit 1; }

# 5. scheduler (persistent campaign queue)
start_service scheduler "app.scheduler.main" "$LOG_DIR/scheduler.log" -- "$VENV/bin/python" -m app.scheduler.main
ok "Scheduler started"

# 6. dashboard
cd "$ROOT/apps/dashboard"
if port_in_use "$WEB_PORT" && ! pid_alive web; then
  err "Port $WEB_PORT is in use by another process"; exit 1
fi
if [[ "$MODE" == "prod" && -f .next/BUILD_ID ]]; then
  start_service web "next start" "$LOG_DIR/web.log" -- npx --no-install next start -H 127.0.0.1 -p "$WEB_PORT"
else
  [[ "$MODE" == "prod" ]] && warn "No production build found; starting the dev server (run 'make build' for production)"
  start_service web "next dev" "$LOG_DIR/web.log" -- npx --no-install next dev -H 127.0.0.1 -p "$WEB_PORT"
fi
wait_http "http://127.0.0.1:$WEB_PORT/login" 120 && ok "Dashboard ready" || warn "Dashboard still starting (see logs/web.log)"

echo
echo "  Dashboard:  http://localhost:$WEB_PORT"
echo "  API:        http://127.0.0.1:$API_PORT/api/docs"
echo "  Logs:       $LOG_DIR"
echo "  Stop:       ./scripts/stop.sh"
