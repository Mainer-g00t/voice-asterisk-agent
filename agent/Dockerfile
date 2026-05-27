FROM python:3.12-slim

WORKDIR /app

# libsoxr-dev: system dependency for the SOXR audio resampler used by pipecat
RUN apt-get update \
    && apt-get install -y --no-install-recommends libsoxr-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 9099

CMD ["python", "server.py"]
