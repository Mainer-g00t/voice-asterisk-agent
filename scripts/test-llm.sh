#!/usr/bin/env bash
# Test LLM service: POST a chat completion request via the OpenAI-compatible Ollama API.
# Usage: ./test-llm.sh ["user message"] [model]

set -euo pipefail

LLM_URL="${LLM_URL:-http://127.0.0.1:11434}"
MODEL="${2:-smollm2:135m}"
MESSAGE="${1:-Hello, who are you?}"

echo "=== LLM endpoint: POST $LLM_URL/v1/chat/completions ==="
echo "Model:   $MODEL"
echo "Message: $MESSAGE"
echo

curl -sS \
  -X POST \
  "$LLM_URL/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d "{
    \"model\": \"$MODEL\",
    \"messages\": [
      {\"role\": \"system\", \"content\": \"You are a helpful voice assistant. Keep your answers short and clear.\"},
      {\"role\": \"user\",   \"content\": \"$MESSAGE\"}
    ],
    \"temperature\": 0.7
  }" | python3 -m json.tool
