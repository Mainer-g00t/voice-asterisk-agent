# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A local voice AI agent that answers phone calls via Asterisk PBX. A SIP softphone connects to Asterisk over SIP; Asterisk routes the call to a Python agent container via the AudioSocket protocol; the agent runs a Pipecat pipeline: STT → LLM → TTS.

```
Softphone ──SIP──▶ Asterisk ──AudioSocket──▶ agent
                                                │
                                   stt ◀────────┤
                                   llm ◀────────┤
                                   tts ◀────────┘

Admin UI (browser) ──▶ config-api ──▶ Postgres (source of truth)
                                   └──▶ Redis  (hot cache, per-call read)
```

Eight Docker services total (`docker-compose.yml`). Everything runs locally; no cloud API keys required by default.

## Common commands

```bash
make up            # build images and start all services in background
make down          # stop and remove containers
make restart       # rebuild and restart only the agent (faster than make up)
make migrate       # apply all SQL migrations to the running Postgres (safe to re-run)
make logs          # stream logs from all services
make logs-agent    # stream logs from a single service (also: logs-tts, logs-llm, logs-stt, logs-asterisk)
make cli           # open Asterisk CLI (use `pjsip show endpoints` to verify softphone registration)
make shell         # shell into the agent container

# Test individual AI services
./scripts/test-stt.sh
./scripts/test-tts.sh
./scripts/test-llm.sh

# Admin UI
open http://localhost:8080/admin

# API docs
open http://localhost:8080/docs
```

## Architecture

### Config store: Postgres + Redis (`config-api/`)

Agent configuration (prompts, provider selection, tool schemas, specialist prompts) lives in Postgres and is cached in Redis. The agent reads from Redis on every call (~1 ms). On a cache miss it falls back to `config-api`'s `/internal/agents/{slug}/snapshot` endpoint, which re-reads Postgres and re-warms Redis.

**Push-on-save:** when an admin saves an agent in the UI, `config-api` writes to Postgres in a transaction, then immediately pushes a fully-denormalized snapshot to Redis before returning 200. The next call picks up the new config instantly — no agent restart needed.

**Redis key:** `agent:config:{slug}` (TTL 300s) — a single JSON snapshot containing system_prompt, greeting_trigger, providers, tool schemas, and specialist prompts.

**Postgres tables:** `agents`, `provider_configs`, `tool_definitions`, `specialist_configs`, `config_versions` (audit log with rollback).

**Schema DDL:** `config-api/migrations/001_initial.sql` (auto-applied on first Postgres start via `docker-entrypoint-initdb.d`).

### AudioSocket transport (`agent/transport/audiosocket.py`)

Pipecat has no native Asterisk support, so this project implements the [AudioSocket protocol](https://docs.asterisk.org/Configuration/Channel-Drivers/AudioSocket/) as a custom `BaseTransport`. Key details:

- **Protocol**: each frame is `[type:1B][length:2B big-endian][payload:NB]`. Types: `0x00` hangup, `0x01` UUID, `0x03` DTMF, `0x10` audio.
- **Resampling**: Asterisk sends/receives 8 kHz PCM; the pipeline runs at 16 kHz. The transport resamples in both directions.
- **Output pacing**: Pipecat's audio output task flushes frames as fast as the queue empties. Asterisk's internal AudioSocket queue is fixed-size — bursts overflow it and cause choppy audio. The output transport deliberately paces each 20 ms chunk with `asyncio.sleep(0.020)` to match real-time playback.

### Server (`agent/server.py`)

An asyncio TCP server on port 9099. Each incoming connection spawns one `AudioSocketTransport` + one Pipecat `PipelineTask`. The pipeline is created *before* `transport.connect()` so that `on_client_connected` event handlers are registered before they fire. Calls end via `EndFrame` (hangup) not inactivity timeout.

### Pipeline (`agent/pipeline.py`)

`transport.input() → STT → user aggregator (with SileroVAD) → LLM → TTS → transport.output() → assistant aggregator`

At the start of each call, `create_pipeline_task(transport, call_uuid)` loads the agent config from Redis (or config-api fallback). Providers and tool handlers are built from the config dict — no module-level agent imports remain.

The greeting is triggered by injecting the `greeting_trigger` string as the first user message on `on_client_connected`, making the bot speak first.

### Tool handler registry (`agent/tool_handlers/`)

Tool **schemas** (JSON) live in the DB/Redis snapshot. Tool **handlers** (Python async functions) live in code, registered by `handler_type` string:

```python
# agent/tool_handlers/__init__.py
HANDLER_REGISTRY = {
    "specialist_router": make_specialist_handler,
    # add new handler types here
}
```

`make_specialist_handler(agent_config)` returns a closure that reads specialist prompts from the config snapshot — so prompts are editable via the admin UI without any code change. Adding a new `handler_type` still requires a code deploy.

### Provider and agent selection

Configured in the admin UI at `http://localhost:8080/admin` — no `.env` changes or restarts needed. `AGENT_SLUG` in `.env` sets which agent the container serves by default.

| Provider type | Options |
|---|---|
| STT | `local` (Whisper), `openai`, `deepgram` |
| LLM | `local` (Ollama), `openai`, `anthropic` |
| TTS | `local` (Piper), `openai`, `cartesia` |

### Local AI services

- **STT** (`stt/`): Whisper `tiny` model, OpenAI-compatible API on port 8000.
- **LLM** (`llm/`): Ollama with `smollm2:135m` by default. Model data persisted in the `ollama_data` Docker volume. Try `llama3.2:1b` for better quality.
- **TTS** (`tts/`): Piper TTS, OpenAI-compatible API on port 5000 (mapped to 5001 on host — macOS AirPlay occupies 5000).

### Asterisk config (`asterisk/`)

- `pjsip.conf` — SIP transport and a single endpoint (`softphone`) with password `1234`.
- `extensions.conf` — all calls from `[from-softphone]` context are answered and routed to `AudioSocket(${CALL_UUID}, agent:9099)`.
- `rtp.conf` — RTP port range 10000–10100.
- `docker-entrypoint.sh` — substitutes `ASTERISK_EXTERNAL_IP` into `pjsip.conf` at container start (needed for RTP NAT traversal when the softphone and Docker are on different hosts).

### Phone routing (`phone_routes` table)

Each row maps a DID (dialed number or Asterisk extension pattern) to an agent slug. Managed at `http://localhost:8080/admin/routes`.

**Apply button flow:**
1. Queries active routes from Postgres
2. For each unique agent slug: starts `agent-{slug}` Docker container via Docker SDK (using the pre-built `voice-asterisk-agent-agent` image)
3. Generates `asterisk/extensions.conf` from the route table and writes it to disk
4. Runs `docker exec asterisk asterisk -rx "dialplan reload"` to activate new routes immediately

Each `agent-{slug}` container listens on port 9099 internally. Asterisk addresses them by container name (`agent-basic:9099`, `agent-sales:9099`, etc.) on the `voiceai` Docker network — no port conflicts.

The `agent` service in `docker-compose.yml` is the fallback container (and also builds the image that route-managed containers use).

**New migrations**: run `make migrate` after pulling changes that add new `.sql` files to `config-api/migrations/`. Postgres only auto-applies migrations on first init.

### Adding a new agent

Via admin UI: `http://localhost:8080/admin/agents` → New agent. No code change or restart needed.

Via API: `POST /api/agents` with `{slug, display_name, system_prompt, greeting_trigger}`.

### Multi-agent / tool-calling agents

The **orchestrator** agent (seeded in `001_initial.sql`) is the reference implementation: a hotel concierge that delegates requests to specialist subagents via the `route_to_specialist` tool. Each specialist is a separate LLM call (claude-haiku) with its own system prompt — all editable in the admin UI under Specialists.

Requires `ANTHROPIC_API_KEY` set in `.env` and the `orchestrator` agent's LLM provider set to `anthropic` in the admin UI.

## Key gotcha: `ASTERISK_EXTERNAL_IP`

Set this in `.env` to your Mac's LAN IP (`ipconfig getifaddr en0`) when the softphone and Docker are on different machines. Defaults to `127.0.0.1`, which works when both run on the same host.
