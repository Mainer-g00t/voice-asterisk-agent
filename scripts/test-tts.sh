#!/usr/bin/env bash
# Test TTS service: POST text and save the PCM audio response.
# Usage: ./test-tts.sh ["text to speak"] [output.pcm]
# To play back: aplay -r 24000 -f S16_LE -c 1 output.pcm
#               OR: ffplay -f s16le -ar 24000 -ac 1 output.pcm

set -euo pipefail

TTS_URL="${TTS_URL:-http://127.0.0.1:5001}"
TEXT="${1:-Hello, this is a text-to-speech test.}"
OUTPUT="${2:-/tmp/tts-output.pcm}"

echo "=== TTS endpoint: POST $TTS_URL/v1/audio/speech ==="
echo "Text: $TEXT"
echo "Output: $OUTPUT"
echo

HTTP_CODE=$(curl -sS -w "%{http_code}" \
  -X POST \
  "$TTS_URL/v1/audio/speech" \
  -H "Content-Type: application/json" \
  -d "{
    \"input\": \"$TEXT\",
    \"model\": \"piper\",
    \"voice\": \"alloy\",
    \"response_format\": \"pcm\",
    \"speed\": 1.0
  }" \
  -o "$OUTPUT")

echo "HTTP status: $HTTP_CODE"

if [[ "$HTTP_CODE" == "200" ]]; then
  SIZE=$(wc -c < "$OUTPUT")
  echo "Received $SIZE bytes of PCM audio (16-bit, 24 kHz, mono)"
  echo
  echo "Play back with:"
  echo "  aplay -r 24000 -f S16_LE -c 1 $OUTPUT"
  echo "  ffplay -f s16le -ar 24000 -ch_layout mono $OUTPUT"
else
  echo "Error: unexpected HTTP $HTTP_CODE"
  cat "$OUTPUT"
  exit 1
fi
