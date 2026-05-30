# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A local voice AI agent that answers phone calls via Asterisk PBX. A SIP softphone connects to Asterisk over SIP; Asterisk routes the call to a Python agent container via the AudioSocket protocol; the agent runs a Pipecat pipeline: STT → LLM → TTS.

```
Softphone ──SIP──▶ Asterisk ──AudioSocket──▶ agent-{slug}
                                                   │
                                      stt ◀────────┤
                                      llm ◀────────┤
                                      tts ◀────────┘

Admin UI (browser) ──▶ config-api ──▶ Postgres (source of truth)
                                   └──▶ Redis  (hot cache, per-call read)
```

Services: `postgres`, `redis`, `config-api`, `stt`, `llm`, `tts`, `agent` (fallback), `asterisk`, `prometheus`, `grafana` in `docker-compose.yml`. Route-managed agent containers (`agent-{slug}`) are started dynamically via the Docker SDK.

## Common commands

```bash
make up            # build images and start all services in background
make down          # stop and remove containers
make restart       # rebuild and restart only the default agent
make migrate       # apply all SQL migrations to the running Postgres (safe to re-run)
make logs          # stream logs from all services
make logs-agent    # stream logs from a single service (also: logs-tts, logs-llm, logs-stt, logs-asterisk)
make cli           # open Asterisk CLI (use `pjsip show endpoints` to verify softphone registration)
make shell         # shell into the agent container
make grafana       # open Grafana dashboard (http://localhost:3000)
make prometheus    # open Prometheus UI (http://localhost:9091)

# After a code deploy, rebuild and restart route-managed containers:
docker compose build agent
docker rm -f agent-basic agent-sales   # remove stale containers
curl -X POST http://localhost:8080/api/routes/apply   # restart with new image

# Test individual AI services
./scripts/test-stt.sh
./scripts/test-tts.sh
./scripts/test-llm.sh

# Admin UI
open http://localhost:8080/admin    # Routes, Agents, Calls
open http://localhost:8080/docs     # API docs
```

## Architecture

### Config store: Postgres + Redis (`config-api/`)

Agent configuration (prompts, provider selection, tool schemas, specialist prompts) lives in Postgres and is cached in Redis. The agent reads from Redis on every call (~1 ms). On a cache miss it falls back to `config-api`'s `/internal/agents/{slug}/snapshot` endpoint, which re-reads Postgres and re-warms Redis.

**Push-on-save:** when an admin saves an agent, `config-api` writes to Postgres then immediately pushes a denormalized snapshot to Redis. Next call picks up the new config — no restart needed.

**Postgres migrations:** `config-api/migrations/00N_*.sql`. Files 001–003 are auto-applied on first Postgres init. For existing installs, run `make migrate` after pulling new ones.

**Postgres tables:**
- `agents`, `provider_configs`, `tool_definitions`, `specialist_configs`, `config_versions` — agent config
- `phone_routes` — DID → agent_slug routing
- `call_logs` — per-call metadata and transcript

### Prometheus + Grafana monitoring

Each agent container (default `agent` + route-managed `agent-{slug}`) runs a Prometheus metrics HTTP server on port **9090** (background thread, started in `server.py main()`). Prometheus scrapes all targets every 10 seconds. Grafana auto-provisions the datasource and dashboard at startup.

**Metrics exposed** (`agent/metrics.py`):
- `voiceai_stt_ttfb_seconds` / `voiceai_llm_ttfb_seconds` / `voiceai_tts_ttfb_seconds` — per-stage TTFB histograms
- `voiceai_llm_tokens_total` — prompt + completion token counters
- `voiceai_tts_chars_total` — TTS character counter
- `voiceai_calls_active` — concurrent call gauge
- `voiceai_calls_total` — call counter by end_reason
- `voiceai_call_duration_seconds` — call duration histogram

**Capture path**: `MetricsCapture` (a `FrameProcessor` in `agent/pipeline.py`) intercepts `MetricsFrame` objects emitted by Pipecat (`enable_metrics=True`, `enable_usage_metrics=True`) and calls the corresponding Prometheus metric objects.

**Scrape config**: `monitoring/prometheus.yml` — add new agent slugs there when adding routes.

**Grafana password**: set `GRAFANA_PASSWORD` in `.env` (defaults to `admin`).

### AudioSocket transport (`agent/transport/audiosocket.py`)

Pipecat has no native Asterisk support, so this project implements the [AudioSocket protocol](https://docs.asterisk.org/Configuration/Channel-Drivers/AudioSocket/) as a custom `BaseTransport`. Key details:

- **Protocol**: each frame is `[type:1B][length:2B big-endian][payload:NB]`. Types: `0x00` hangup, `0x01` UUID, `0x03` DTMF, `0x10` audio.
- **Resampling**: Asterisk sends/receives 8 kHz PCM; the pipeline runs at 16 kHz. The transport resamples in both directions.
- **Output pacing**: Pipecat's audio output task flushes frames as fast as the queue empties. Asterisk's internal AudioSocket queue is fixed-size — bursts overflow it and cause choppy audio. The output transport deliberately paces each 20 ms chunk with `asyncio.sleep(0.020)` to match real-time playback.

### Server (`agent/server.py`)

An asyncio TCP server on port 9099. Each connection spawns one `AudioSocketTransport` + one Pipecat `PipelineTask`. The pipeline is created *before* `transport.connect()` so event handlers fire correctly.

**Hangup handling**: `runner.run(task)` runs as an asyncio Task alongside a `_hangup_watchdog` coroutine. The watchdog polls `reader.at_eof()` to detect connection close, records the accurate hangup timestamp on `call_log`, then force-cancels the pipeline after `HANGUP_DRAIN_TIMEOUT=2s` if it hasn't self-terminated (the output transport can be slow draining queued TTS frames into a closed socket with 20 ms sleeps each).

The `finally` block always runs `call_log.send()` regardless of how the call ended.

### Pipeline (`agent/pipeline.py`)

`transport.input() → STT → user aggregator (with SileroVAD) → LLM → TTS → transport.output() → assistant aggregator`

`create_pipeline_task(transport, call_uuid)` returns `(task, call_log)`. At call start it reads the agent config from Redis (or config-api fallback), builds providers and tool handlers from the config dict.

### Call logging (`agent/call_logger.py`)

`CallLogger` collects `started_at` (set on `on_client_connected`), `ended_at` (set by watchdog on hangup), and the full transcript from `LLMContext.messages` (skipping the system prompt and synthetic greeting trigger). After the pipeline terminates, `call_log.send()` POSTs to `POST /api/calls`.

Transcripts land in `call_logs.transcript` (JSONB) and are viewable at `/admin/calls/{call_uuid}`.

### Tool handler registry (`agent/tool_handlers/`)

Tool **schemas** (JSON) live in the DB/Redis snapshot. Tool **handlers** (Python async functions) live in code, registered by `handler_type` string:

```python
HANDLER_REGISTRY = {
    "specialist_router": make_specialist_handler,
}
```

`make_specialist_handler(agent_config)` returns a closure reading specialist prompts from the config snapshot — prompts are editable in the UI without a code deploy. New `handler_type` values require a code deploy.

### Phone routing (`phone_routes` table)

Each row maps a DID to an agent slug. Managed at `/admin/routes`.

**Apply button flow:**
1. Queries active routes from Postgres
2. For each unique agent slug: starts `agent-{slug}` container via Docker SDK (image: `voice-asterisk-agent-agent`)
3. Generates `asterisk/extensions.conf` and writes it to disk (shared volume)
4. Runs `docker exec asterisk asterisk -rx "dialplan reload"`

Each `agent-{slug}` container listens on port 9099 internally; Asterisk addresses them by container name on the `voiceai` network.

**After a code deploy**: rebuild the image (`docker compose build agent`), remove stale route-managed containers (`docker rm -f agent-basic ...`), then click Apply or call `POST /api/routes/apply`.

### Provider and agent selection

Configured in the admin UI — no `.env` changes or restarts needed. `AGENT_SLUG` in `.env` sets which agent the default container serves.

**Important:** do not store model names as empty strings — always use SQL `NULL`. Jinja2 renders Python `None` as the string `"None"` in HTML attribute values, which then gets saved to the DB. The save handler uses `_clean_model()` to convert empty/`"None"` strings to `NULL`.

### Adding a new agent

Via admin UI: `/admin/agents` → New agent. No code change needed.

### Multi-agent / tool-calling agents

The **orchestrator** agent (seeded in `001_initial.sql`) delegates to specialist subagents via the `route_to_specialist` tool (claude-haiku subagent). Requires `ANTHROPIC_API_KEY` and LLM provider set to `anthropic`.

## Key gotcha: `ASTERISK_EXTERNAL_IP`

Set in `.env` to your Mac's LAN IP (`ipconfig getifaddr en0`) when the softphone and Docker are on different machines. Defaults to `127.0.0.1`.

## Key gotcha: route-managed containers and image updates

`agent-basic`, `agent-sales`, etc. are started by the Docker SDK — they don't auto-update when you run `docker compose build agent`. After any agent code change, rebuild the image then remove and re-apply the route-managed containers (see commands above).
