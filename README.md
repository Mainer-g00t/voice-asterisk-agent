# voice-asterisk-agent

A local voice AI agent that answers **and makes** phone calls via Asterisk PBX.

```
Softphone ──SIP──▶ Asterisk (Docker) ──AudioSocket──▶ Pipecat agent (Docker)
                                                           STT → LLM → TTS
API ────────────▶ Asterisk AMI ────────────────────▶ (same pipeline, outbound)
```

Built with [Pipecat](https://github.com/pipecat-ai/pipecat) and a custom `AudioSocketTransport` —
Pipecat has no native Asterisk support, so this project implements the
[AudioSocket protocol](https://docs.asterisk.org/Configuration/Channel-Drivers/AudioSocket/)
as a first-class Pipecat transport.

Agent configuration, phone routing, call flows, and call history are managed through a built-in **web admin UI** backed by Postgres + Redis — no code changes or container restarts needed.

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

# 3. Build and start all services
# (includes compiling the React flow editor bundle inside Docker — no Node.js needed)
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

Six sections, all editable live with no restarts:

### 📞 Routes — phone number → agent mapping

Each row maps a dialed number or Asterisk extension pattern to an agent slug.

- `_X.` is the catch-all (matches any number not explicitly listed)
- Specific DIDs (e.g. `1000`, `+15551234567`) are matched first
- **Apply** starts the required `agent-{slug}` Docker containers, regenerates `extensions.conf`, and reloads the Asterisk dialplan in one click

### 🤖 Agents — prompts, providers, and flow assignment

Each agent has a system prompt, greeting trigger, and per-provider settings (STT / LLM / TTS). Changes take effect on the next incoming call — in-flight calls finish with their original config.

Prompts and greetings support **`{placeholder}` substitution** — values are injected at call time via the outbound API (see below).

Each agent can optionally have a **flow** assigned — when set, every call to that agent runs the flow's node graph instead of the flat single-prompt model.

| Slug | What it does |
|------|-------------|
| `basic` | Open-ended Q&A assistant |
| `customer_service` | Guided tech-support flow |
| `storyteller` | Collaborative story builder |
| `language_tutor` | English conversation practice |
| `orchestrator` | Hotel concierge — delegates to specialist subagents via tool calling (requires Anthropic) |

### 🔧 Tools — global tool library

Define tools once in the global library and assign them to any agent, or add agent-specific tools directly in the agent edit page.

Each tool has:
- **Name** — snake_case identifier the LLM calls (e.g. `get_weather`)
- **Handler type** — which Python handler runs when the LLM calls the tool
- **Parameters** — structured form builder: per-parameter name, type, description, enum values, required flag
- **Handler config** — handler-specific settings (e.g. webhook URL and timeout)

Available handler types:

| Handler | What it does |
|---------|-------------|
| `specialist_router` | Delegates to a specialist subagent (prompts configurable per-agent in Specialists section) |
| `webhook` | HTTP POST to a configurable URL; returns the JSON response to the LLM |
| `transfer_call` | Initiates a call transfer (stub — Asterisk AMI integration TBD) |

Agent-specific tools override global tools with the same name. All changes push to Redis immediately.

### 🔀 Flows — multi-step call logic

Flows define branching call graphs: nodes connected by conditional edges. The engine runs locally inside the agent container — no extra latency per event.

The flow editor is a visual drag-and-drop canvas (React Flow). Nodes are colored cards; connections are drawn by pulling from the handle at the bottom of any node. Click a node or edge to open a side panel with typed form fields. The JSON stays as the underlying format and is auto-synced — a "View JSON" toggle reveals it.

**Node types:**

| Type | What it does |
|---|---|
| `conversation` | STT → LLM → TTS loop with its own system prompt and greeting. Stays until an edge condition fires. |
| `say` | Plays a fixed TTS message verbatim, then takes the default edge. |
| `gather_dtmf` | Waits for a keypress (0–9, *, #), branches on the digit. |
| `transfer` | Issues an Asterisk AMI Redirect to a destination extension/number. |
| `webhook` | HTTP POSTs current flow state to a URL; branches on a field in the JSON response. |
| `set_variable` | Writes a value into flow state variables, then takes the default edge. |
| `condition` | Branches immediately on a variable value without audio. |
| `end` | Hangs up and marks execution complete. |

**Edge condition types:**

| Condition | Fires when… |
|---|---|
| `keyword_matched` | STT transcript contains any of the listed words |
| `turn_count_gte` | Turn counter reaches N |
| `dtmf_digit` | User pressed a specific digit |
| `tool_result` | A named tool returned a specific field value |
| `variable_equals` | A flow variable equals a value |
| `silence_timeout` | No user input for N seconds |
| `webhook_field` | Webhook response field equals a value |
| `default` | Always matches (use as last/fallback edge) |

**Assigning a flow to an agent:** Admin UI → Agents → edit → 🔀 Flow dropdown → pick a flow → Save. Every call (inbound and outbound) to that agent then runs the flow.

**Per-call override (outbound):** pass `flow_id` in `POST /api/outbound/originate` to use a different flow for a specific call regardless of the agent's default.

**Execution history:** Admin UI → 🔀 Flows → click the run-count badge, or use the shortcut button on the agent edit page. Each execution shows status, final node, turn count, and duration.

The flow definition is stored as JSON in Postgres. The canvas also saves node positions under `_positions` (ignored by the engine):
```json
{
  "entry_node_id": "n1",
  "nodes": [
    {"id": "n1", "type": "conversation", "label": "Main", "config": {
      "system_prompt": "You are a sales agent calling {name}.",
      "greeting": "Hello {name}, this is Acme calling."
    }},
    {"id": "n2", "type": "transfer", "label": "To human", "config": {"destination": "operator"}},
    {"id": "n3", "type": "end", "label": "End", "config": {}}
  ],
  "edges": [
    {"id": "e1", "source": "n1", "target": "n2",
     "condition": {"type": "keyword_matched", "words": ["speak to someone", "human"]}},
    {"id": "e2", "source": "n1", "target": "n3",
     "condition": {"type": "turn_count_gte", "n": 15}}
  ]
}
```

### 📋 Calls — call history and transcripts

Every completed call is logged automatically: direction (inbound 📞 / outbound 📤), duration, turn count, STT/LLM/TTS providers used, end reason, and the full conversation transcript. Click **Transcript** on any row to view the chat-bubble replay.

---

## Outbound calls API

Originate a call programmatically — Asterisk dials the destination, and when answered the full STT→LLM→TTS pipeline runs exactly as for inbound calls. All logging, telemetry, and flows work identically.

```bash
POST http://localhost:8080/api/outbound/originate
```

```json
{
  "destination": "+15551234567",
  "agent_slug": "sales",
  "caller_id": "Acme Corp <+10000000000>",
  "timeout_seconds": 30,
  "flow_id": "optional-flow-uuid",
  "template_vars": {
    "name": "Luis",
    "product": "Premium Plan"
  },
  "metadata": {"campaign_id": "q4-promo"},
  "callback_url": "https://your-campaign-manager/cdr"
}
```

Returns immediately with a `call_uuid`. Poll `GET /api/calls/{call_uuid}` for status and transcript.

The agent container for the requested slug is **started automatically** if not already running.

### Prompt placeholders

Define `{placeholder}` patterns in the agent's system prompt, greeting trigger, or flow node configs. They are substituted at call time using `template_vars`:

```
System prompt:  "You are a sales agent for Acme. You are calling {name} about {product}."
Greeting:       "Hello {name}, this is an automated call from Acme."
```

Unknown placeholders are left as-is (`{unknown}` → `{unknown}`), so partial substitution never crashes.

### CDR webhook callback

When a call ends, config-api POSTs the completed call record to `callback_url`:

```json
{
  "call_uuid": "...",
  "agent_slug": "sales",
  "direction": "outbound",
  "destination": "+15551234567",
  "started_at": "...", "ended_at": "...",
  "duration_seconds": 87,
  "turn_count": 5,
  "end_reason": "hangup",
  "transcript": [...],
  "metadata": {"campaign_id": "q4-promo"}
}
```

Failures are logged as warnings and never affect the call record. Use this to integrate with a campaign manager or CRM.

### Outbound configuration

| `.env` variable | Default | Description |
|----------------|---------|-------------|
| `AMI_SECRET` | — | Asterisk AMI password |
| `OUTBOUND_CHANNEL_FORMAT` | `PJSIP/{destination}` | Channel template. `{destination}` is replaced with the dialed number. Use `PJSIP/{destination}@trunk` for a SIP trunk. |

For local testing with the softphone: `destination=softphone` rings the registered softphone directly.

Full API docs: **http://localhost:8080/docs** → outbound section.

---

## Monitoring — Grafana + Prometheus

```bash
make grafana     # http://localhost:3000  (admin / admin or GRAFANA_PASSWORD)
make prometheus  # http://localhost:9091
```

Each agent container exposes a Prometheus `/metrics` endpoint on port 9090. Prometheus scrapes every 10 seconds; the Grafana "Voice Agent" dashboard is pre-provisioned.

**Metrics collected per call:**

| Metric | Type | What it measures |
|--------|------|-----------------|
| `voiceai_stt_ttfb_seconds` | Histogram | STT time-to-first-byte |
| `voiceai_llm_ttfb_seconds` | Histogram | LLM time-to-first-token |
| `voiceai_tts_ttfb_seconds` | Histogram | TTS time-to-first-audio |
| `voiceai_llm_tokens_total` | Counter | Prompt + completion tokens |
| `voiceai_tts_chars_total` | Counter | TTS characters processed |
| `voiceai_calls_active` | Gauge | Concurrent calls right now |
| `voiceai_calls_total` | Counter | Calls by agent and end reason |
| `voiceai_call_duration_seconds` | Histogram | Total call duration |

**Dashboard panels:** p50/p95/p99 latency per stage, active calls, call rate, token/char usage over time.

To add a new route-managed agent slug to Prometheus scraping, add it to `monitoring/prometheus.yml`.

---

## Multi-agent routing

Different phone numbers route to different agents, each in its own Docker container:

```
+1-555-1000 → agent-basic        (container: agent-basic, port 9099)
+1-555-2000 → agent-sales        (container: agent-sales, port 9099)
+1-555-3000 → agent-orchestrator (container: agent-orchestrator, port 9099)
```

All managed through the Routes UI — click **Apply** to spin up containers and reload Asterisk.

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

**Note:** `smollm2:135m` does not support tool/function calling. Use `llama3.2:3b` or a cloud provider for agents with tools:
```bash
docker compose exec llm ollama pull llama3.2:3b
# then set the model in admin UI → agent → LLM provider
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
                                        call template vars, flow execution stored here too
```

Postgres tables: `agents`, `provider_configs`, `tool_definitions`, `specialist_configs`, `config_versions`, `phone_routes`, `call_logs`, `flows`, `flow_executions`, `flow_events`.

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

# API docs (includes outbound originate, call status, flows, tool management)
open http://localhost:8080/docs
```

---

## Architecture notes

- **One pipeline per call**: `server.py` creates a fresh `AudioSocketTransport` + Pipecat `PipelineTask` per TCP connection. Inbound and outbound calls use the same code path.
- **Config loaded per call**: `pipeline.py` reads the agent snapshot from Redis at call start. Also reads per-call template vars (`call:vars:{uuid}`) and applies them to the prompt and greeting before the pipeline starts.
- **Flow execution**: if the agent config (or originate request) includes a `flow_id`, a `FlowController` is attached to the pipeline. `FlowWatcherProcessor` monitors STT transcriptions, DTMF digits, and LLM turn-end events; when an edge condition fires it emits a `FlowTransitionFrame`. `server.py` acts on it (say, conversation, transfer, end, webhook, set_variable, condition).
- **Inbound flow init**: for inbound calls, `pipeline.py` calls `POST /internal/flows/init-execution` at call start to create the execution row and warm the Redis cache — same path as outbound from that point.
- **Flow execution log**: at call end, the agent bulk-posts all events (node_entered, edge_taken, event_received) to `POST /api/flows/executions/complete` for analytics and replay.
- **Hangup handling**: a watchdog coroutine watches `reader.at_eof()`, records the hangup time, and force-cancels the pipeline after 2 s if it hasn't self-terminated.
- **Call logging**: `call_logger.py` collects timestamps, direction, and the full conversation from `LLMContext.messages` after each call, then POSTs to `POST /api/calls`. Outbound calls are pre-created as `direction=outbound` at originate time; the agent upserts the transcript on completion.
- **Outbound flow**: `POST /api/outbound/originate` → auto-starts agent container → stores template vars + flow execution in Redis → AMI Originate → Asterisk dials → `[outbound-agent]` dialplan → AudioSocket → agent pipeline.
- **CDR webhook**: after call log upsert, config-api fires `callback_url` (if set at originate time) as a background task. Failures are warnings only.
- **Tool handlers stay in code**: tool *schemas* live in the DB; handlers are Python async functions registered by `handler_type` string in `agent/tool_handlers/`.
- **Prometheus metrics**: `MetricsCapture` FrameProcessor intercepts Pipecat `MetricsFrame` objects and records TTFB/token/char metrics. Call-level counters tracked in `server.py`.
- **Audio pacing**: output sends 20 ms chunks with `asyncio.sleep(0.020)` to prevent Asterisk's AudioSocket frame-queue overflow.
- **VAD**: Silero VAD (PyTorch CPU) for end-of-speech detection.
- **Asterisk NAT**: `docker-entrypoint.sh` substitutes `ASTERISK_EXTERNAL_IP` into `pjsip.conf` and `AMI_SECRET` into `manager.conf` at startup.
