#!/usr/bin/env bash
# Prepares the isolated E2E data directory (runs before the E2E API server starts):
#  - fresh data dir and database every run
#  - model files linked read-only from the real data/models
#  - caller.wav: real speech from the local Piper server, played by Chrome's fake microphone,
#    so the browser voice test exercises VAD -> STT -> LLM -> TTS for real.
set -euo pipefail
DATA="$1"
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
rm -rf "$DATA"
mkdir -p "$DATA"
ln -s "$ROOT/data/models" "$DATA/models"
if curl -fsS -m 30 -o "$DATA/caller-raw.wav" -H 'content-type: application/json' \
     -d '{"text":"Hello. What kind of hosting do you offer?","voice":"en_GB-alba-medium"}' \
     http://127.0.0.1:5005/synthesize; then
  # silence first so the greeting finishes before the caller "speaks" (Chrome loops the file)
  ffmpeg -loglevel error -y -f lavfi -t 12 -i anullsrc=r=48000:cl=mono -i "$DATA/caller-raw.wav" \
    -f lavfi -t 14 -i anullsrc=r=48000:cl=mono \
    -filter_complex "[1:a]aresample=48000,aformat=channel_layouts=mono[s];[0:a][s][2:a]concat=n=3:v=0:a=1" \
    -ar 48000 -ac 1 -c:a pcm_s16le "$DATA/caller.wav"
else
  ffmpeg -loglevel error -y -f lavfi -t 5 -i anullsrc=r=48000:cl=mono -c:a pcm_s16le "$DATA/caller.wav"
  echo "Piper unavailable" > "$DATA/NO_SPEECH"
fi
