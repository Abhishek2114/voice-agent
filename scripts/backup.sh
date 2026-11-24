#!/usr/bin/env bash
# Create a consistent backup (database + recordings + uploads) in data/backups/.
#   ./scripts/backup.sh                 unencrypted
#   VA_BACKUP_PASSPHRASE=... ./scripts/backup.sh   AES-256-GCM encrypted
#   ./scripts/backup.sh restore FILE    verify + stage a restore (applied on next start)
set -euo pipefail
source "$(dirname "$0")/lib.sh"
cd "$ROOT/backend"
export PYTHONPATH="$ROOT/backend:$ROOT"
pass_arg=()
[[ -n "${VA_BACKUP_PASSPHRASE:-}" ]] && pass_arg=(--passphrase-env VA_BACKUP_PASSPHRASE)

case "${1:-create}" in
  create) "$VENV/bin/python" -m app.system.backup create "${pass_arg[@]}" ;;
  list) "$VENV/bin/python" -m app.system.backup list ;;
  verify) "$VENV/bin/python" -m app.system.backup verify "$2" "${pass_arg[@]}" ;;
  restore)
    "$VENV/bin/python" -m app.system.backup stage "$2" "${pass_arg[@]}"
    ok "Restore staged. Run ./scripts/stop.sh && ./scripts/start.sh to apply it." ;;
  *) err "usage: backup.sh [create|list|verify FILE|restore FILE]"; exit 2 ;;
esac
