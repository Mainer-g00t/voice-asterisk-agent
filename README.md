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

Agent configuration (prompts, providers, tool schemas) is stored in **Postgres**, cached in **Redis**, and managed through a built-in **web admin UI** — no code changes or restarts needed to update an agent.

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
# If softphone and Docker are on the same machine, 127.0.0.1 (the default) works as-is.

# 3. Build and start all 8 services
make up

# 4. Open the admin UI to manage agents
open http://localhost:8080/admin

# 5. Watch logs
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

## Agent management (Admin UI)

All agent configuration is managed through the web UI at **http://localhost:8080/admin** — no file edits or container restarts needed.

Each agent has:
- **System prompt** — the LLM's personality and instructions
- **Greeting trigger** — the message injected to make the bot speak first
- **Providers** — per-agent STT / LLM / TTS provider and model selection
- **Tools** (optional) — tool schemas for function-calling agents
- **Specialists** (optional) — subagent prompts for orchestrator-style agents

Changes take effect on the **next incoming call** with no restart. In-flight calls finish with their original config.

### Built-in agents

| Slug | What it does |
|------|-------------|
| `basic` *(default)* | Open-ended Q&A assistant |
| `customer_service` | Guided tech-support flow — collects issue, troubleshoots step by step, offers escalation |
| `storyteller` | Collaborative story — bot opens a scene, then caller and bot take turns |
| `language_tutor` | English conversation practice — gentle inline corrections, keeps the student talking |
| `orchestrator` | Hotel concierge that delegates to specialist subagents via tool calling (requires Anthropic) |

To switch the active agent, change `AGENT_SLUG` in `.env` (or set it per-call via the API):

```bash
# in .env
AGENT_SLUG=customer_service
make restart   # only needed when changing AGENT_SLUG
```

### Orchestrator / multi-agent

The `orchestrator` agent demonstrates LLM-to-LLM delegation: the main LLM determines what the caller needs, then calls a `route_to_specialist` tool. The tool handler spawns a separate claude-haiku API call with a specialist-specific system prompt and returns the answer. Specialist prompts are fully editable in the admin UI under **Specialists**.

Requires `ANTHROPIC_API_KEY` in `.env` and the orchestrator's LLM provider set to `anthropic` in the admin UI.

---

## AI providers

Provider selection is configured per-agent in the **admin UI**. The `.env` file only needs the API keys for whichever cloud providers you use.

### Local (default — no API keys needed)

| Service | Technology | Container |
|---------|-----------|-----------|
| STT | Whisper (via faster-whisper) | `stt` on port 8000 |
| LLM | Ollama — `smollm2:135m` by default | `llm` on port 11434 |
| TTS | Piper TTS — `en_US-amy-low` voice | `tts` on port 5001 |

To use a different Ollama model, pull it and update the agent in the admin UI:

```bash
docker compose exec llm ollama pull llama3.2:3b
# then set the model in the admin UI under the agent's LLM provider
```

### Cloud providers

Set the relevant API keys in `.env`:

```env
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
DEEPGRAM_API_KEY=...
CARTESIA_API_KEY=...
```

Then select the provider and model for each agent in the admin UI. Supported options:

| Provider type | Options |
|---|---|
| STT | `local` (Whisper), `openai`, `deepgram` |
| LLM | `local` (Ollama), `anthropic`, `openai` |
| TTS | `local` (Piper), `openai`, `cartesia` |

---

## Config store architecture

```
Admin UI ──▶ config-api (FastAPI, port 8080) ──▶ Postgres (source of truth)
                                              └──▶ Redis   (hot cache, TTL 300s)
                                                      ▲
                                              agent reads per-call (~1 ms)
```

- **Postgres** stores agent config across 5 tables: `agents`, `provider_configs`, `tool_definitions`, `specialist_configs`, `config_versions` (audit log with rollback).
- **Redis** holds a fully-denormalized JSON snapshot per agent (`agent:config:{slug}`). One GET per call, no joins.
- **Push-on-save**: the API writes to Postgres then immediately pushes to Redis before returning. If Redis is down, the agent falls back to fetching from the API directly.
- **API docs**: http://localhost:8080/docs

---

## Workflow across laptops

```bash
make pull   # git pull --ff-only + docker compose pull
make up     # docker compose up --build -d
```

Each developer sets their own `ASTERISK_EXTERNAL_IP` in `.env`. Agent configs live in Postgres (persisted in the `postgres_data` Docker volume) — no per-laptop config needed beyond the IP and any cloud API keys.

---

## Debugging — test individual services

```bash
# TTS: POST text, receive raw PCM audio
./scripts/test-tts.sh "Hello, is this working?"

# STT: POST a WAV file, receive a transcript
./scripts/test-stt.sh path/to/audio.wav

# LLM: POST a chat message, receive a completion
./scripts/test-llm.sh "Who are you?"
```

---

## Useful commands

```bash
make logs           # stream logs from all services (last 50 lines each)
make logs-agent     # stream agent logs only
make logs-tts / logs-llm / logs-stt / logs-asterisk
make cli            # open Asterisk CLI (pjsip show endpoints, dialplan show)
make shell          # bash inside the agent container
make restart        # rebuild + restart only the agent (faster iteration)
make down           # stop everything
```

---

## Architecture notes

- **One pipeline per call**: `server.py` creates a fresh `AudioSocketTransport` and Pipecat `PipelineTask` for every incoming TCP connection. Calls are fully isolated.
- **Config loaded per call**: `pipeline.py` fetches the agent snapshot from Redis at the start of each call. Changing a prompt in the admin UI takes effect on the next call — no restart needed.
- **Tool handlers stay in code**: Tool *schemas* (JSON) live in the DB and travel to the LLM via Redis. Tool *handlers* (Python async functions) stay in `agent/tool_handlers/`, registered by a `handler_type` string. Adding a new handler type requires a code deploy; editing tool schemas or specialist prompts does not.
- **Custom transport only**: No Pipecat source files were modified. `AudioSocketTransport` subclasses Pipecat's public `BaseInputTransport`/`BaseOutputTransport`. Updating Pipecat is safe — only the transport base class API needs to be checked after an upgrade.
- **Audio resampling**: Asterisk sends/receives 8 kHz PCM. The transport resamples to 16 kHz for the STT pipeline. The local Piper TTS outputs audio that is downsampled back to 8 kHz for Asterisk.
- **Audio pacing**: Pipecat's output queue has no built-in pacing. The transport sends audio in 20 ms chunks with `asyncio.sleep(0.020)` between each to prevent Asterisk's frame-queue overflow (choppy audio).
- **VAD**: Silero VAD (via PyTorch CPU) is used for end-of-speech detection.
- **Asterisk NAT**: `docker-entrypoint.sh` substitutes `ASTERISK_EXTERNAL_IP` into `pjsip.conf` at startup so the SDP `c=` line advertises a reachable IP for RTP.
