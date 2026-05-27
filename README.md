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
- A SIP softphone: [Linphone](https://www.linphone.org/) (free) or macOS [Telephone.app](https://telephone-app.com/)

No cloud API keys required — everything runs locally by default.

---

## Quick start

```bash
# 1. Clone
git clone https://github.com/<you>/voice-asterisk-agent.git
cd voice-asterisk-agent

# 2. Copy env (no keys needed for local mode)
cp .env.example .env

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

> **Different machine on the same LAN?** Use the host Mac's LAN IP instead of `127.0.0.1`.

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
- **Audio resampling**: Asterisk sends/receives 8 kHz PCM. The transport resamples to 16 kHz for the STT pipeline (using SOXR). The local Piper TTS outputs 16 kHz which is upsampled to 24 kHz (required by Pipecat's `OpenAITTSService`) using scipy before being downsampled back to 8 kHz for Asterisk.
- **VAD**: Silero VAD (via PyTorch CPU) is used for end-of-speech detection.
- **Asterisk image**: `andrius/asterisk:18` — ships `app_audiosocket` and `chan_pjsip`. Verify with `make cli` → `module show like audiosocket`.
- **macOS Docker note**: UDP ports are forwarded through the Docker Desktop VM. The RTP range is intentionally narrow (10000–10100) to keep port mapping fast.
