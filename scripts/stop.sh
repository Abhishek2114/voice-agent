#!/usr/bin/env bash
# Stop only the processes this application started (verified via .run/*.pid signatures).
set -euo pipefail
source "$(dirname "$0")/lib.sh"

for svc in web scheduler api piper whisper ollama; do
  stop_service "$svc"
done
ok "voice-agent stopped (processes not started by voice-agent were left untouched)"
