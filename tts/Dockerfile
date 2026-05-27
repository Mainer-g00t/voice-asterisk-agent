FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download smallest English voice model (~64 MB)
RUN mkdir -p /models && \
    wget -q -O /models/voice.onnx \
      "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/low/en_US-amy-low.onnx" && \
    wget -q -O /models/voice.onnx.json \
      "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/low/en_US-amy-low.onnx.json"

COPY . .

EXPOSE 5000

CMD ["python", "server.py"]
