#!/bin/sh
set -e

MODEL="${OLLAMA_MODEL:-smollm2:135m}"

ollama serve &
OLLAMA_PID=$!

echo "Waiting for Ollama to start..."
until curl -sf http://localhost:11434/api/tags > /dev/null 2>&1; do
    sleep 1
done

echo "Pulling $MODEL..."
ollama pull "$MODEL"

echo "Warming up $MODEL..."
curl -sf http://localhost:11434/api/generate \
  -d "{\"model\":\"$MODEL\",\"prompt\":\"hi\",\"stream\":false}" \
  > /dev/null
echo "Model warm and ready."

wait $OLLAMA_PID
