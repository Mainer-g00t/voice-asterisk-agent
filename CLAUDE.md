# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A local voice AI agent that answers **and makes** phone calls via Asterisk PBX. A SIP softphone connects to Asterisk over SIP; Asterisk routes the call to a Python agent container via the AudioSocket protocol; the agent runs a Pipecat pipeline: STT → LLM → TTS.

```
Softphone ──SIP──▶ Asterisk ──AudioSocket──▶ agent-{slug}
                                                   │
                                      stt ◀────────┤
                                      llm ◀────────┤
                                      tts ◀────────┘

API ──▶ config-api ──▶ Asterisk AMI ──▶ (outbound dial) ──AudioSocket──▶ agent-{slug}

Admin UI (browser) ──▶ config-api ──▶ Postgres (source of truth)
                                   └──▶ Redis  (hot cache, per-call read)
```

Services: `postgres`, `redis`, `config-api`, `stt`, `llm`, `tts`, `agent` (fallback), `asterisk`, `prometheus`, `grafana`, `tools-server` in `docker-compose.yml`. Route-managed agent containers (`agent-{slug}`) are started dynamically via the Docker SDK.

## Common commands

```bash
make up            # build images and start all services in background (includes frontend build)
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
open http://localhost:8080/admin    # Routes, Agents, Tools, Flows, Calls
open http://localhost:8080/docs     # API docs (agents, tools, outbound, flows, calls)
```

## Architecture

### Config store: Postgres + Redis (`config-api/`)

Agent configuration (prompts, provider selection, tool schemas, specialist prompts, flow assignment) lives in Postgres and is cached in Redis. The agent reads from Redis on every call (~1 ms). On a cache miss it falls back to `config-api`'s `/internal/agents/{slug}/snapshot` endpoint, which re-reads Postgres and re-warms Redis.

**Push-on-save:** when an admin saves an agent, `config-api` writes to Postgres then immediately pushes a denormalized snapshot to Redis. Next call picks up the new config — no restart needed.

**Postgres migrations:** `config-api/migrations/00N_*.sql`. Files 001–003 are auto-applied on first Postgres init. For existing installs, run `make migrate` after pulling new ones.

**Postgres tables:**
- `agents`, `provider_configs`, `tool_definitions`, `specialist_configs`, `config_versions` — agent config
- `tool_definitions` (global: `agent_id=NULL, is_global=TRUE`), `agent_tool_refs` — global tool library
- `phone_routes` — DID → agent_slug routing
- `call_logs` — per-call metadata, transcript, direction, destination
- `flows` — flow definitions (JSON blob: nodes + edges)
- `flow_executions` — per-call execution state (current node, runtime variables, status)
- `flow_events` — audit log of every node entry, edge traversal, and event received

### Call Flows (`config-api/flow_engine.py`, `agent/flow_controller.py`)

Flows define multi-step branching call logic as a node graph. The engine is a stateless Python module (`flow_engine.py`) copied into both `config-api/` and `agent/` — it runs locally inside the agent container with no network hop per event.

**Node types:** `conversation` | `say` | `gather_dtmf` | `transfer` | `webhook` | `set_variable` | `condition` | `end`

**Node config fields (quick ref):**
- `conversation`: `system_prompt`, `greeting`, `silence_timeout_seconds`
- `say`: `message`
- `gather_dtmf`: `prompt` (optional TTS before waiting), `dtmf_timeout` (seconds, default 10)
- `transfer`: `destination`, `dialplan_context` (default `"default"`)
- `webhook`: `url`, `timeout` (seconds, default 10) — POSTs `{call_uuid, current_node_id, state}`, stores response in `last_webhook_result`
- `set_variable`: `variable_name`, `value` — writes into `state.variables`
- `condition`: `variable_name` — silent branch, draw one `variable_equals` edge per value + default

**Edge condition types:** `keyword_matched` | `turn_count_gte` | `dtmf_digit` | `tool_result` | `variable_equals` | `silence_timeout` | `webhook_field` | `default`

**Edge condition parameters:**
- `keyword_matched`: `words: ["bye", "stop"]`
- `turn_count_gte`: `n: 5`
- `dtmf_digit`: `digit: "1"`
- `tool_result`: `tool: "name"`, `field: "key"`, `value: "expected"`
- `variable_equals`: `var: "last_dtmf"`, `value: "1"` — checks built-in state keys OR user-defined variables
- `webhook_field`: `field: "action"`, `value: "transfer"`
- `silence_timeout`, `default`: no parameters

**Flow state variables** (all readable via `variable_equals` / `condition` node):

Built-in (auto-updated by the engine):
- `turn_count` — incremented after every agent response
- `last_dtmf` — last keypad digit pressed (`"1"`, `"*"`, etc.)
- `last_transcript` — full STT text of the last user turn
- `last_webhook_result` — full JSON response from the last `webhook` node (stringified)
- `last_tool_results` — `{tool_name: result}` map after any tool call
- `last_intent` — intent label if intent detection is used
- `call_status` — `"no_answer"` or `"busy"` for unanswered outbound calls

User-defined (via `set_variable` nodes, stored in `state.variables`): any key you choose. User-defined take precedence over built-ins on name collision.

**Flow definition shape** (stored as JSONB in `flows.definition`):
```json
{
  "entry_node_id": "n1",
  "nodes": [{"id":"n1","type":"conversation","label":"...","config":{...}}],
  "edges": [{"id":"e1","source":"n1","target":"n2","condition":{"type":"keyword_matched","words":["bye"]}}],
  "_positions": {"n1": {"x": 300, "y": 100}}
}
```
`_positions` stores canvas coordinates for the visual editor; ignored by the engine.

**Visual flow editor** (`config-api/flow-editor/`): React + React Flow app that replaces the JSON textarea on the flow edit page. Built inside Docker via a multi-stage Dockerfile (node:20-slim → python:3.12-slim). The compiled bundle (`index.js` + `index.css`) is produced at image build time and served from `/static/flow-editor/` via FastAPI's `StaticFiles`. No Node.js needed on the host — `make up` handles everything.

Source lives in `config-api/flow-editor/src/`:
- `App.jsx` — ReactFlow canvas, toolbar (add node buttons), side-panel wiring
- `nodeTypes.jsx` — 8 custom node components (colored cards with config preview)
- `NodePanel.jsx` — right-side panel for editing selected node config
- `EdgePanel.jsx` — right-side panel for editing selected edge condition
- `convert.js` — `toReactFlow()` / `fromReactFlow()` (bidirectional, preserves positions)
- `constants.js` — node type metadata, condition type definitions, `conditionLabel()`

To iterate on the editor UI locally (requires Node): `cd config-api/flow-editor && npm ci && npm run build`, then rebuild the config-api container. The `make build-frontend` target does the same.

**Assigning flows:**
- Admin UI → Agents → edit → 🔀 Flow dropdown → pick flow → Save (applies to all calls to this agent)
- `POST /api/outbound/originate` with `"flow_id": "..."` — per-call override, ignores agent's default flow

**Inbound call flow init:** `pipeline.py` checks if `flow_id` is in the agent config and calls `POST /internal/flows/init-execution` to create the DB row + warm Redis (`flow:exec:{call_uuid}`). For outbound calls this is pre-created by `outbound.py` before dialing.

**Agent-side execution:**
- `FlowWatcherProcessor(FrameProcessor)` sits between STT and the user aggregator in the pipeline — watches `TranscriptionFrame` (keyword matching), `DTMFInputFrame` (DTMF), `LLMFullResponseEndFrame` (turn counting)
- `FlowController` is the per-call state machine. When an edge fires it puts a `FlowTransitionFrame` on an `asyncio.Queue`
- `server.py` has `_flow_transition_handler()` running alongside the pipeline runner — reads the queue and acts: `end` → cancel pipeline, `transfer` → AMI Redirect, `say` → inject TTS message via LLMContextFrame, `conversation` → update system prompt, `webhook` → HTTP POST, `set_variable`/`condition` → delegate back to FlowController
- At call end, `FlowController.finalize()` bulk-posts event log to `POST /api/flows/executions/complete`

**DTMF:** `AudioSocketInputTransport._read_loop()` emits `DTMFInputFrame` when Asterisk sends a type `0x03` frame. This flows upstream through the pipeline to `FlowWatcherProcessor`.

**New frames** (`agent/frames.py`): `DTMFInputFrame(digit)`, `FlowTransitionFrame(node_id, node_type, node_config, edge_id)`.

### Outbound calls (`config-api/routers/outbound.py`, `config-api/ami_client.py`)

`POST /api/outbound/originate` triggers an outbound call:
1. Validates agent slug, pre-creates `call_logs` row with `direction='outbound'`
2. Stores `template_vars` in Redis at `call:vars:{call_uuid}` (TTL 10 min)
3. If `flow_id` provided: fetches flow, creates `flow_executions` row, caches at `flow:exec:{call_uuid}`
4. Stores `callback_url` + `metadata` in Redis at `call:meta:{call_uuid}` for CDR webhook
5. Calls `docker_manager.ensure_agent_running(slug)` — auto-starts the container if needed
6. Sends AMI `Originate` to Asterisk via TCP 5038 (`ami_client.py`)
7. Asterisk dials; when answered, runs `[outbound-agent]` dialplan → AudioSocket → agent

The agent's `call_log.send()` upserts the pre-created row with transcript/duration on completion. Direction and destination are preserved through the upsert.

**AMI config:** `asterisk/manager.conf.tmpl` — secret substituted from `AMI_SECRET` env var by `docker-entrypoint.sh`. AMI user is `voiceagent`, port 5038.

**Channel format:** `OUTBOUND_CHANNEL_FORMAT` env var (default `PJSIP/{destination}`). `{destination}` is replaced with the request's destination field. For SIP trunks: `PJSIP/{destination}@trunk-name`.

**CDR webhook:** after the agent posts the completed call record to `/api/calls`, `calls.py` reads `call:meta:{call_uuid}` from Redis and fires an async HTTP POST to `callback_url` if set. Failures are logged as warnings and never affect the call record.

### Prompt template vars

Agent `system_prompt` and `greeting_trigger` — and flow node `system_prompt`/`greeting` configs — support `{placeholder}` syntax. At call start, `pipeline.py` reads `call:vars:{call_uuid}` from Redis and substitutes using `str.format_map()`. Unknown placeholders are left as-is (no crash). Inbound calls with no vars are unaffected.

Stored by `redis_client.push_call_vars()` in config-api; read by `_load_call_vars()` in agent/pipeline.py.

### Prometheus + Grafana monitoring

Each agent container (default `agent` + route-managed `agent-{slug}`) runs a Prometheus metrics HTTP server on port **9090** (background thread, started in `server.py main()`). Prometheus scrapes all targets every 10 seconds. Grafana auto-provisions the datasource and dashboard at startup.

**Metrics exposed** (`agent/metrics.py`):
- `voiceai_stt_ttfb_seconds` / `voiceai_llm_ttfb_seconds` / `voiceai_tts_ttfb_seconds` — per-stage TTFB histograms (labels: agent_slug, provider)
- `voiceai_llm_tokens_total` — prompt + completion token counters
- `voiceai_tts_chars_total` — TTS character counter
- `voiceai_calls_active` — concurrent call gauge
- `voiceai_calls_total` — call counter by end_reason
- `voiceai_call_duration_seconds` — call duration histogram

**Capture path**: `MetricsCapture` (a `FrameProcessor` inserted after TTS in `agent/pipeline.py`) intercepts `MetricsFrame` objects emitted by Pipecat (`enable_metrics=True`, `enable_usage_metrics=True`) and records them into the Prometheus metric objects. Call-level counters (active, total, duration) are tracked in `server.py`.

**Scrape config**: `monitoring/prometheus.yml` — add new agent slugs there when adding routes.

**Grafana password**: set `GRAFANA_PASSWORD` in `.env` (defaults to `admin`).

### AudioSocket transport (`agent/transport/audiosocket.py`)

Pipecat has no native Asterisk support, so this project implements the [AudioSocket protocol](https://docs.asterisk.org/Configuration/Channel-Drivers/AudioSocket/) as a custom `BaseTransport`. Key details:

- **Protocol**: each frame is `[type:1B][length:2B big-endian][payload:NB]`. Types: `0x00` hangup, `0x01` UUID, `0x03` DTMF, `0x10` audio.
- **Resampling**: Asterisk sends/receives 8 kHz PCM; the pipeline runs at 16 kHz. The transport resamples in both directions.
- **Output pacing**: Pipecat's audio output task flushes frames as fast as the queue empties. Asterisk's internal AudioSocket queue is fixed-size — bursts overflow it and cause choppy audio. The output transport deliberately paces each 20 ms chunk with `asyncio.sleep(0.020)` to match real-time playback.
- **DTMF forwarding**: when a `0x03` frame arrives, `_read_loop` emits a `DTMFInputFrame` upstream so `FlowWatcherProcessor` can catch it.

### Server (`agent/server.py`)

An asyncio TCP server on port 9099. Each connection spawns one `AudioSocketTransport` + one Pipecat `PipelineTask`. The pipeline is created *before* `transport.connect()` so event handlers fire correctly. Both inbound and outbound calls use the same server — the UUID frame from Asterisk identifies the call.

**Hangup handling**: `runner.run(task)` runs as an asyncio Task alongside a `_hangup_watchdog` coroutine. The watchdog polls `reader.at_eof()` to detect connection close, records the accurate hangup timestamp on `call_log`, then force-cancels the pipeline after `HANGUP_DRAIN_TIMEOUT=2s` if it hasn't self-terminated.

**Flow handling**: `_flow_transition_handler()` runs as a third asyncio Task alongside the runner and watchdog. It drains `transition_queue` and acts on each `FlowTransitionFrame`. At call end, `FlowController.finalize()` is fired as a background task.

The `finally` block always runs `call_log.send()` regardless of how the call ended.

### Pipeline (`agent/pipeline.py`)

With no flow: `transport.input() → STT → user aggregator (with SileroVAD) → LLM → TTS → MetricsCapture → transport.output() → assistant aggregator`

With a flow: `FlowWatcherProcessor` is inserted between STT and the user aggregator.

`create_pipeline_task(transport, call_uuid, transition_queue)` returns `(task, call_log, flow_controller, context)`. At call start:
1. Reads agent config from Redis (or config-api fallback)
2. Reads per-call template vars from Redis (`call:vars:{call_uuid}`) — applies to prompt/greeting
3. Reads flow execution from Redis (`flow:exec:{call_uuid}`); if absent but agent has `flow_id`, calls `/internal/flows/init-execution` to create it
4. If flow present: overrides system_prompt/greeting from entry node config, instantiates `FlowController` + `FlowWatcherProcessor`
5. Builds STT/LLM/TTS providers and tool handlers from config
6. Wires `MetricsCapture` processor and `PipelineTask` observers

### Call logging (`agent/call_logger.py`)

`CallLogger` collects `started_at` (set on `on_client_connected`), `ended_at` (set by watchdog on hangup), and the full transcript from `LLMContext.messages` (skipping the system prompt and synthetic greeting trigger). After the pipeline terminates, `call_log.send()` POSTs to `POST /api/calls`.

The upsert in `calls.py` preserves `direction` and `destination` set at originate time — they are never overwritten by the agent's POST.

Transcripts land in `call_logs.transcript` (JSONB) and are viewable at `/admin/calls/{call_uuid}`.

### Tool handler registry (`agent/tool_handlers/`)

Tool **schemas** (JSON) live in the DB/Redis snapshot. Tool **handlers** (Python async functions) live in code, registered by `handler_type` string:

```python
HANDLER_REGISTRY = {
    "specialist_router": make_specialist_handler,
    "webhook":           make_webhook_handler,
    "transfer_call":     make_transfer_call_handler,
}
```

Handler factories receive `(agent_config, tool_config)` — `tool_config` includes `handler_config` (handler-specific settings from the DB, e.g. webhook URL). New `handler_type` values require a code deploy; everything else (schemas, parameters, handler_config) is data-driven.

**Built-in handlers:**
- `specialist_router` — spawns a specialist subagent via Anthropic API; prompts editable in UI
- `webhook` — HTTP POST to URL in `handler_config.url`; returns JSON response to LLM
- `transfer_call` — stub; logs transfer request, returns confirmation to LLM (Asterisk AMI TBD)

**Global tool library** (`/admin/tools`): tools defined once and assigned to multiple agents via `agent_tool_refs` table. Agent-specific tools override globals with the same name. Both appear in the Redis snapshot merged at push time.

**Example tools server** (`tools-server/`): FastAPI service on port 8100 with `get_current_time`, `get_weather`, and `echo` endpoints. Reachable inside the voiceai network as `http://tools-server:8100`. Add endpoints here to expose new webhook tools.

**DB tables:** `tool_definitions` (agent-specific when `agent_id` set, global when `is_global=TRUE` and `agent_id=NULL`), `agent_tool_refs` (many-to-many assignment).

**Note:** `smollm2:135m` does not support function/tool calling. Use `llama3.2:3b` or a cloud LLM for agents with tools.

### Phone routing (`phone_routes` table)

Each row maps a DID to an agent slug. Managed at `/admin/routes`.

**Apply button flow:**
1. Queries active routes from Postgres
2. For each unique agent slug: starts `agent-{slug}` container via Docker SDK (image: `voice-asterisk-agent-agent`)
3. Generates `asterisk/extensions.conf` and writes it to disk (shared volume) — always includes the `[outbound-agent]` context
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

## Key gotcha: outbound calls need a SIP trunk for real numbers

The default `OUTBOUND_CHANNEL_FORMAT=PJSIP/{destination}` works for dialing registered PJSIP endpoints (e.g. `destination=softphone` rings the softphone). To dial real E.164 numbers, configure a SIP provider trunk in `pjsip.conf` and set `OUTBOUND_CHANNEL_FORMAT=PJSIP/{destination}@your-trunk`.

## Shared package: `packages/voiceai_common`

`packages/voiceai_common/flow_engine.py` is the **single source of truth** for flow edge evaluation. Both `config-api` and `agent` install it as a pip package at Docker build time — no duplication. To change flow engine logic, edit only `packages/voiceai_common/flow_engine.py` and rebuild both images.

Both Dockerfiles use repo root as build context (`context: .`) so they can `COPY packages/ /packages/` before their own source. Import as `from voiceai_common.flow_engine import ...`.
