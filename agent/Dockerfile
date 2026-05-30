FROM python:3.12-slim

WORKDIR /app

# libsoxr-dev: system dependency for the SOXR audio resampler used by pipecat
RUN apt-get update \
    && apt-get install -y --no-install-recommends libsoxr-dev \
    && rm -rf /var/lib/apt/lists/*

# Install shared voiceai_common package first (single source of truth for flow_engine etc.)
COPY packages/ /packages/
RUN pip install --no-cache-dir /packages/

COPY agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/ .

EXPOSE 9099

CMD ["python", "server.py"]
