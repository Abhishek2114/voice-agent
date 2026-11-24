#!/usr/bin/env bash
# One-time setup on Apple Silicon macOS. Installs only what is missing.
#   ./scripts/setup-mac.sh            dependencies + environments + database
#   ./scripts/setup-mac.sh --models   also download speech models (~125 MB, asks first)
#   ./scripts/setup-mac.sh --yes      non-interactive (assume yes)
# Never downloads an LLM and never reinstalls Ollama.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

WANT_MODELS=0; ASSUME_YES=0
for a in "$@"; do
  case "$a" in --models) WANT_MODELS=1 ;; --yes|-y) ASSUME_YES=1 ;; esac
done
confirm() { [[ $ASSUME_YES == 1 ]] && return 0; read -r -p "$1 [y/N] " r; [[ "$r" =~ ^[Yy]$ ]]; }

echo "voice-agent setup"
echo "================="

# --- platform
[[ "$(uname -s)" == "Darwin" ]] || { err "This script targets macOS."; exit 1; }
if [[ "$(uname -m)" == "arm64" ]]; then ok "Apple Silicon ($(sysctl -n machdep.cpu.brand_string 2>/dev/null || echo arm64))"
else warn "Intel Mac detected — works, but whisper.cpp Metal acceleration is unavailable"; fi
mem_gb=$(( $(sysctl -n hw.memsize) / 1024 / 1024 / 1024 ))
info "Memory: ${mem_gb} GB · Free disk: $(df -h "$ROOT" | awk 'NR==2{print $4}')"

# --- Homebrew packages (only if missing)
command -v brew >/dev/null || { err "Homebrew is required: https://brew.sh"; exit 1; }
need_brew=()
command -v ffmpeg >/dev/null || need_brew+=(ffmpeg)
command -v whisper-server >/dev/null || need_brew+=(whisper-cpp)
command -v uv >/dev/null || need_brew+=(uv)
command -v node >/dev/null || need_brew+=(node)
if ((${#need_brew[@]})); then
  info "Installing missing packages: ${need_brew[*]}"
  brew install "${need_brew[@]}"
fi
ok "ffmpeg $(ffmpeg -version 2>/dev/null | head -1 | awk '{print $3}')"
ok "whisper.cpp $(brew list --versions whisper-cpp 2>/dev/null | awk '{print $2}')"
ok "uv $(uv --version | awk '{print $2}') · node $(node --version)"
node_major=$(node --version | sed 's/v\([0-9]*\).*/\1/')
(( node_major >= 20 )) || { err "Node.js 20+ required"; exit 1; }

# --- Ollama (never reinstalled, never exposed)
if command -v ollama >/dev/null; then
  ok "Ollama $(ollama --version 2>&1 | grep -o '[0-9][0-9.]*' | tail -1) installed"
else
  warn "Ollama not installed. Install from https://ollama.com/download (voice-agent will not install it for you)."
fi

# --- directories
mkdir -p "$ROOT"/data/{database,recordings,transcripts,uploads,backups,models/{whisper,piper,vad}}
chmod 700 "$ROOT"/data "$ROOT"/data/{database,recordings,transcripts,uploads,backups}
[[ -f "$ROOT/.env" ]] || { cp "$ROOT/.env.example" "$ROOT/.env"; chmod 600 "$ROOT/.env"; ok "Created .env from .env.example"; }

# --- backend environment (Python 3.12 — wheels for aiortc/onnxruntime are available)
cd "$ROOT/backend"
uv venv --python 3.12 .venv -q 2>/dev/null || true
uv sync -q
SITE=$("$VENV/bin/python" -c 'import site; print(site.getsitepackages()[0])')
echo "$ROOT" > "$SITE/voice_agent_root.pth"
ok "Backend environment ready ($("$VENV/bin/python" --version))"

# --- Piper (GPL-3.0) in its own isolated environment
if [[ ! -x "$PIPER_VENV/bin/python" ]]; then
  uv venv --python 3.12 "$PIPER_VENV" -q
fi
uv pip install --python "$PIPER_VENV/bin/python" -q "piper-tts[http]==1.8.0"
ok "Piper TTS installed in .tools/piper (separate process, GPL-3.0)"

# --- dashboard
cd "$ROOT/apps/dashboard"
npm ci --no-audit --no-fund --loglevel=error
ok "Dashboard dependencies installed"

# --- database
cd "$ROOT/backend"
PYTHONPATH="$ROOT/backend:$ROOT" "$VENV/bin/python" - <<'PY'
import asyncio
from app.core.config import get_settings
from app.database.migrate import upgrade_database
from app.database.session import dispose_engine
async def main():
    get_settings().ensure_dirs(); get_settings().resolved_secret()
    await upgrade_database(); await dispose_engine()
asyncio.run(main())
PY
ok "Database migrated ($(ls "$ROOT/data/database"))"

# --- speech models
download() { # url dest
  [[ -f "$2" ]] && { info "exists: ${2#$ROOT/}"; return; }
  curl -fL --progress-bar -o "$2.part" "$1" && mv "$2.part" "$2"
}
missing_models=()
[[ -f "$MODELS/whisper/ggml-base.en-q5_1.bin" ]] || missing_models+=("whisper base.en q5_1 (57 MB, MIT)")
[[ -f "$MODELS/piper/en_GB-alba-medium.onnx" ]] || missing_models+=("Piper en_GB-alba-medium (60 MB, CC BY 4.0)")
[[ -f "$MODELS/vad/silero_vad.onnx" ]] || missing_models+=("Silero VAD (2 MB, MIT)")
if ((${#missing_models[@]})); then
  warn "Missing speech models:"; printf '    - %s\n' "${missing_models[@]}"
  if [[ $WANT_MODELS == 1 ]] && confirm "Download them now (~125 MB)?"; then
    HF=https://huggingface.co
    download "$HF/ggerganov/whisper.cpp/resolve/main/ggml-base.en-q5_1.bin" "$MODELS/whisper/ggml-base.en-q5_1.bin"
    for ext in onnx onnx.json; do
      download "$HF/rhasspy/piper-voices/resolve/main/en/en_GB/alba/medium/en_GB-alba-medium.$ext" \
        "$MODELS/piper/en_GB-alba-medium.$ext"
    done
    download "$HF/rhasspy/piper-voices/resolve/main/en/en_GB/alba/medium/MODEL_CARD" "$MODELS/piper/en_GB-alba-medium.MODEL_CARD"
    download "https://raw.githubusercontent.com/snakers4/silero-vad/master/src/silero_vad/data/silero_vad.onnx" "$MODELS/vad/silero_vad.onnx"
    ok "Speech models downloaded"
  else
    info "Re-run with --models to download them."
  fi
else
  ok "Speech models present"
fi

# --- LLM check (no download)
if curl -fsS -m 3 "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
  model="${VA_DEFAULT_LLM_MODEL:-qwen3:4b-instruct-2507-q4_K_M}"
  if curl -fsS "$OLLAMA_URL/api/tags" | grep -qF "\"$model\""; then ok "LLM $model available"
  else warn "LLM '$model' not installed. Choose an installed model in Settings, or (your decision) run: ollama pull $model"; fi
else
  warn "Ollama is not running; start it with: OLLAMA_HOST=127.0.0.1 ollama serve"
fi

echo
ok "Setup complete. Start with: ./scripts/start.sh"
