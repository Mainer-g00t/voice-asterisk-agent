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
- API keys: Deepgram (STT), Anthropic (LLM), Cartesia (TTS)

---

## Quick start

```bash
# 1. Clone
git clone https://github.com/<you>/voice-asterisk-agent.git
cd voice-asterisk-agent

# 2. Fill in API keys
cp .env.example .env
# edit .env and add your DEEPGRAM_API_KEY, ANTHROPIC_API_KEY, CARTESIA_API_KEY

# 3. Start
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

## Workflow across laptops

```bash
make pull   # git pull --ff-only + docker compose pull
make up     # docker compose up --build -d
```

---

## Useful commands

```bash
make logs     # stream logs from both services
make cli      # open Asterisk CLI (pjsip show endpoints, dialplan show)
make shell    # bash inside the agent container
make restart  # rebuild + restart only the agent (faster iteration)
make down     # stop everything
```

---

## Swapping AI providers

The pipeline in [`agent/pipeline.py`](agent/pipeline.py) uses Deepgram + Anthropic + Cartesia by default.
Pipecat supports many providers — swap the service imports and update `.env` accordingly.
The `AudioSocketTransport` is provider-agnostic; no changes needed there.

---

## Architecture notes

- **One pipeline per call**: `server.py` creates a fresh `AudioSocketTransport` and Pipecat `PipelineTask` for every incoming TCP connection. Calls are fully isolated.
- **Audio resampling**: Asterisk sends 8 kHz PCM; the transport resamples to 16 kHz for STT (and back to 8 kHz for output) using SOXR.
- **Asterisk image**: `andrius/asterisk:18` — ships `app_audiosocket` and `chan_pjsip`. Verify with `make cli` → `module show like audiosocket`.
- **macOS Docker note**: UDP ports are forwarded through the Docker Desktop VM. The RTP range is intentionally narrow (10000–10100) to keep port mapping fast.
