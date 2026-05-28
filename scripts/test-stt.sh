#!/usr/bin/env bash
# Test STT service: POST a WAV file and get back a transcript.
# Usage: ./test-stt.sh [path/to/audio.wav]
# Requires: curl, sox (for generating a test tone if no file is provided)

set -euo pipefail

STT_URL="${STT_URL:-http://127.0.0.1:8000}"
AUDIO_FILE="${1:-}"

if [[ -z "$AUDIO_FILE" ]]; then
  # Generate a short silent WAV as a smoke-test input
  AUDIO_FILE="$(mktemp /tmp/test-stt-XXXX.wav)"
  CLEANUP=true
  if command -v sox &>/dev/null; then
    sox -n -r 16000 -c 1 -b 16 "$AUDIO_FILE" trim 0.0 1.0
    echo "Generated 1-second silent WAV via sox: $AUDIO_FILE"
  elif command -v ffmpeg &>/dev/null; then
    ffmpeg -f lavfi -i anullsrc=r=16000:cl=mono -t 1 -acodec pcm_s16le "$AUDIO_FILE" -y -loglevel quiet
    echo "Generated 1-second silent WAV via ffmpeg: $AUDIO_FILE"
  else
    echo "No audio file provided and neither sox nor ffmpeg is available."
    echo "Pass a WAV file as argument: $0 path/to/audio.wav"
    exit 1
  fi
else
  CLEANUP=false
fi

echo
echo "=== STT endpoint: POST $STT_URL/v1/audio/transcriptions ==="
echo

curl -sS \
  -X POST \
  "$STT_URL/v1/audio/transcriptions" \
  -F "file=@$AUDIO_FILE;type=audio/wav" \
  -F "model=whisper-1" \
  -F "language=en" | python3 -m json.tool

if [[ "$CLEANUP" == "true" ]]; then
  rm -f "$AUDIO_FILE"
fi
