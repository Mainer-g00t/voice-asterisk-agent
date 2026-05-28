# voice-asterisk-agent

A local voice AI agent that answers phone calls via Asterisk PBX.

```
Softphone ──SIP──▶ Asterisk (Docker) ──AudioSocket──▶ Pipecat agent (Docker)
                                                           STT → LLM → TTS
```

Built with [Pipecat](https://github.com/pipecat-ai/pipecat) and a custom `AudioSocketTransport` —
Pipecat has no native Asterisk support, so this project implements the
[AudioSocket protocol](https://docs.asterisk.org/Configuration/Channel-Drivers/AudioSocket/)
as a first-class Pipecat transport.

---

## Requirements

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (macOS)
- A SIP softphone: [Linphone](https://www.linphone.org/), [baresip](https://github.com/baresip/baresip), or macOS [Telephone.app](https://telephone-app.com/)

No cloud API keys required — everything runs locally by default.

---

## Quick start

```bash
# 1. Clone
git clone https://github.com/<you>/voice-asterisk-agent.git
cd voice-asterisk-agent

# 2. Copy env and set your machine's LAN IP (used for RTP NAT traversal)
cp .env.example .env
# Edit .env and set ASTERISK_EXTERNAL_IP to your Mac's LAN IP:
#   macOS: ipconfig getifaddr en0
# If baresip and Docker are on the same machine, 127.0.0.1 (the default) works as-is.

# 3. Build and start
make up

# 4. Watch logs
make logs
```

---

## Softphone setup (once per laptop)

Configure your softphone with:

| Field | Value |
|---|---|
| SIP username | `softphone` |
| SIP password | `secret1234` |
| SIP domain / registrar | `127.0.0.1` |
| Transport | UDP |
| Port | 5060 |

Dial **any number** (e.g. `1000`) — the dialplan matches everything and routes the call to the AI agent.

> **Different machine on the same LAN?** Set `SIP domain` to the host Mac's LAN IP, and set `ASTERISK_EXTERNAL_IP` in `.env` to the same IP before running `make up`.

---

## AI providers

The pipeline selects providers via environment variables in `.env`:

```env
STT_PROVIDER=local      # local | deepgram | openai
LLM_PROVIDER=local      # local | anthropic | openai
TTS_PROVIDER=local      # local | cartesia  | openai
```

### Local (default — no API keys needed)

| Service | Technology | Container |
|---------|-----------|-----------|
| STT | Whisper (via faster-whisper) | `stt` |
| LLM | Ollama — `smollm2:135m` by default | `llm` |
| TTS | Piper TTS — `en_US-amy-low` voice | `tts` |

The Ollama model is kept warm in memory (`OLLAMA_KEEP_ALIVE=-1`) and pre-loaded at container startup to avoid cold-start latency on the first call.

To use a different Ollama model:

```env
OLLAMA_MODEL=llama3.2:3b
```

Pull it before starting:

```bash
docker compose exec llm ollama pull llama3.2:3b
```

### Cloud providers

Add the relevant keys to `.env` and set the provider:

```env
STT_PROVIDER=deepgram
DEEPGRAM_API_KEY=...

LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=...

TTS_PROVIDER=cartesia
CARTESIA_API_KEY=...
```

Supported options per service:

| Variable | Options |
|----------|---------|
| `STT_PROVIDER` | `local`, `deepgram`, `openai` |
| `LLM_PROVIDER` | `local`, `anthropic`, `openai` |
| `TTS_PROVIDER` | `local`, `cartesia`, `openai` |

---

## Workflow across laptops

```bash
make pull   # git pull --ff-only + docker compose pull
make up     # docker compose up --build -d
```

Each developer sets their own `ASTERISK_EXTERNAL_IP` in `.env`. On a new machine:

```bash
cp .env.example .env
# Set ASTERISK_EXTERNAL_IP to the machine's LAN IP (or leave as 127.0.0.1 for same-machine)
make up
```

---

## Debugging — test individual services

Three scripts let you smoke-test each AI service independently (no full call needed):

```bash
# TTS: POST text, receive raw PCM audio
./scripts/test-tts.sh "Hello, is this working?"
# Play back: ffplay -f s16le -ar 24000 -ch_layout mono /tmp/tts-output.pcm

# STT: POST a WAV file, receive a transcript
./scripts/test-stt.sh path/to/audio.wav
# Without an argument, generates a silent WAV as a smoke test (requires sox or ffmpeg)

# LLM: POST a chat message, receive a completion
./scripts/test-llm.sh "Who are you?"
```

Each script respects an override env var (`TTS_URL`, `STT_URL`, `LLM_URL`) if you need to point at a non-default port.

---

## Useful commands

```bash
make logs           # stream logs from all services (last 50 lines each)
make logs-agent     # stream agent logs only
make logs-tts       # stream TTS logs only
make logs-llm       # stream Ollama LLM logs only
make logs-stt       # stream Whisper STT logs only
make logs-asterisk  # stream Asterisk logs only
make cli            # open Asterisk CLI (pjsip show endpoints, dialplan show)
make shell          # bash inside the agent container
make restart        # rebuild + restart only the agent (faster iteration)
make down           # stop everything
```

---

## Architecture notes

- **One pipeline per call**: `server.py` creates a fresh `AudioSocketTransport` and Pipecat `PipelineTask` for every incoming TCP connection. Calls are fully isolated.
- **Custom transport only**: No Pipecat source files were modified. `AudioSocketTransport` subclasses Pipecat's public `BaseInputTransport`/`BaseOutputTransport`. Updating Pipecat is safe — only the transport base class API needs to be checked after an upgrade.
- **Audio resampling**: Asterisk sends/receives 8 kHz PCM. The transport resamples to 16 kHz for the STT pipeline (using SOXR). The local Piper TTS outputs 16 kHz which is upsampled to 24 kHz (required by Pipecat's `OpenAITTSService`) before being downsampled back to 8 kHz for Asterisk.
- **Audio pacing**: Pipecat's output queue has no built-in pacing. The transport sends audio in 20 ms chunks with `asyncio.sleep(0.020)` between each to match Asterisk's real-time playback rate and prevent frame-queue overflow (choppy audio).
- **VAD**: Silero VAD (via PyTorch CPU) is used for end-of-speech detection.
- **Asterisk NAT**: The Asterisk container builds from `asterisk/Dockerfile` and runs `docker-entrypoint.sh`, which substitutes `ASTERISK_EXTERNAL_IP` into `pjsip.conf` at startup via `envsubst`. This ensures the SDP `c=` line advertises a reachable IP for RTP rather than the Docker-internal container IP.
- **Asterisk image**: `andrius/asterisk:18` — ships `app_audiosocket` and `chan_pjsip`. Verify with `make cli` → `module show like audiosocket`.
- **macOS Docker note**: UDP ports are forwarded through the Docker Desktop VM. The RTP range is intentionally narrow (10000–10100) to keep port mapping fast.
