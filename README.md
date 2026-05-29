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

Agent configuration, phone routing, and call history are managed through a built-in **web admin UI** backed by Postgres + Redis — no code changes or container restarts needed.

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

# 2. Copy env and fill in your settings
cp .env.example .env
# Set ASTERISK_EXTERNAL_IP to your Mac's LAN IP:  ipconfig getifaddr en0
# Set POSTGRES_PASSWORD to a strong password
# If softphone and Docker run on the same machine, ASTERISK_EXTERNAL_IP=127.0.0.1 is fine.

# 3. Build and start all services
make up

# 4. Open the admin UI
open http://localhost:8080/admin
```

On a **new laptop after pulling**, also run:
```bash
make migrate   # applies any new SQL migrations to the running Postgres
```

---

## Softphone setup (once per laptop)

| Field | Value |
|---|---|
| SIP username | `softphone` |
| SIP password | `secret1234` |
| SIP domain / registrar | `127.0.0.1` (or your Mac's LAN IP if softphone is on a different machine) |
| Transport | UDP, port 5060 |

Dial any number — the dialplan routes calls based on the **Phone Routes** table.

---

## Admin UI — http://localhost:8080/admin

Three sections, all editable live with no restarts:

### 📞 Routes — phone number → agent mapping

Each row maps a dialed number or Asterisk extension pattern to an agent slug.

- `_X.` is the catch-all (matches any number not explicitly listed)
- Specific DIDs (e.g. `1000`, `+15551234567`) are matched first
- **Apply** starts the required `agent-{slug}` Docker containers, regenerates `extensions.conf`, and reloads the Asterisk dialplan in one click

### 🤖 Agents — prompts and providers

Each agent has a system prompt, greeting trigger, and per-provider settings (STT / LLM / TTS). Changes take effect on the next incoming call — in-flight calls finish with their original config.

| Slug | What it does |
|------|-------------|
| `basic` | Open-ended Q&A assistant |
| `customer_service` | Guided tech-support flow |
| `storyteller` | Collaborative story builder |
| `language_tutor` | English conversation practice |
| `orchestrator` | Hotel concierge — delegates to specialist subagents via tool calling (requires Anthropic) |

### 📋 Calls — call history and transcripts

Every completed call is logged automatically: duration, turn count, STT/LLM/TTS providers used, end reason, and the full conversation transcript. Click **Transcript** on any row to view the chat-bubble replay.

---

## Multi-agent routing (Option A)

Different phone numbers can route to different agents, each running in its own Docker container. All managed through the Routes UI:

```
+1-555-1000 → agent-basic        (container: agent-basic, port 9099)
+1-555-2000 → agent-sales        (container: agent-sales, port 9099)
+1-555-3000 → agent-orchestrator (container: agent-orchestrator, port 9099)
```

Each container uses the same pre-built image (`voice-asterisk-agent-agent`), launched automatically by the Docker SDK when you click **Apply**. Asterisk addresses them by container name on the internal `voiceai` Docker network.

**After a code deploy** (new agent image), remove stale route-managed containers and re-apply:
```bash
make up                          # rebuilds the agent image
docker rm -f agent-basic         # remove stale containers
# then click Apply in the Routes UI (or: curl -X POST http://localhost:8080/api/routes/apply)
```

---

## AI providers

Configured per-agent in the admin UI. Cloud API keys go in `.env`.

### Local (default — no API keys needed)

| Service | Technology | Port |
|---------|-----------|------|
| STT | Whisper `tiny` | 8000 |
| LLM | Ollama `smollm2:135m` | 11434 |
| TTS | Piper TTS | 5001 |

To switch Ollama model:
```bash
docker compose exec llm ollama pull llama3.2:3b
# then set the model in the admin UI under the agent's LLM provider
```

### Cloud

Set keys in `.env`, then select provider + model in the admin UI:

| Type | Options |
|---|---|
| STT | `local`, `openai`, `deepgram` |
| LLM | `local`, `anthropic`, `openai` |
| TTS | `local`, `openai`, `cartesia` |

---

## Config store architecture

```
Admin UI ──▶ config-api (FastAPI :8080) ──▶ Postgres (source of truth)
                                        └──▶ Redis   (hot cache, TTL 300s)
                                                ▲
                                        agent reads per-call (~1 ms)
```

Postgres tables: `agents`, `provider_configs`, `tool_definitions`, `specialist_configs`, `config_versions`, `phone_routes`, `call_logs`.

Migrations live in `config-api/migrations/`. Run `make migrate` after pulling new ones.

---

## Workflow across laptops

```bash
make pull      # git pull + docker compose pull
make up        # rebuild and start everything
make migrate   # apply any new DB migrations
```

Set your own `ASTERISK_EXTERNAL_IP` and `POSTGRES_PASSWORD` in `.env`. Agent configs and call history live in Postgres (persisted in the `postgres_data` Docker volume).

---

## Debugging

```bash
make logs                          # all services
make logs-agent                    # default agent container
docker logs agent-basic --tail=50  # route-managed agent container
make logs-tts / logs-llm / logs-stt / logs-asterisk
make cli                           # Asterisk CLI
make shell                         # shell into agent container

# Test AI services directly
./scripts/test-tts.sh "Hello, is this working?"
./scripts/test-stt.sh path/to/audio.wav
./scripts/test-llm.sh "Who are you?"

# API docs
open http://localhost:8080/docs
```

---

## Architecture notes

- **One pipeline per call**: `server.py` creates a fresh `AudioSocketTransport` + Pipecat `PipelineTask` per TCP connection. Calls are fully isolated.
- **Config loaded per call**: `pipeline.py` reads the agent snapshot from Redis at call start. Prompt changes take effect on the next call.
- **Hangup handling**: a watchdog coroutine watches `reader.at_eof()`, records the hangup time, and force-cancels the pipeline after 2 s if it hasn't self-terminated (the Pipecat output transport can be slow to drain queued TTS audio into a closed socket).
- **Call logging**: `call_logger.py` collects timestamps and the full conversation from `LLMContext.messages` after each call, then POSTs to `POST /api/calls`.
- **Tool handlers stay in code**: tool *schemas* live in the DB; handlers are Python async functions registered by `handler_type` string in `agent/tool_handlers/`.
- **Custom transport only**: no Pipecat source files modified. `AudioSocketTransport` subclasses public Pipecat base classes.
- **Audio pacing**: output sends 20 ms chunks with `asyncio.sleep(0.020)` to prevent Asterisk's AudioSocket frame-queue overflow.
- **VAD**: Silero VAD (PyTorch CPU) for end-of-speech detection.
- **Asterisk NAT**: `docker-entrypoint.sh` substitutes `ASTERISK_EXTERNAL_IP` into `pjsip.conf` at startup.
