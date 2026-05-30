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
git clone https://github.com/luisbeqja/voice-asterisk-agent.git
cd voice-asterisk-agent

# 2. Copy env and fill in your settings
cp .env.example .env
# REQUIRED: set your Mac's LAN IP (so SIP works across machines)
#   macOS: ipconfig getifaddr en0
# RECOMMENDED: set ADMIN_PASSWORD, API_KEY, POSTGRES_PASSWORD

# 3. Build and start all services
# (includes compiling the React flow editor bundle inside Docker — no Node.js needed)
make up

# 4. Apply DB migrations and seed demo data
make migrate

# 5. Open the admin UI
open http://localhost:8080/admin
```

On a **new laptop after pulling**, run:
```bash
make up      # rebuilds images
make migrate # applies any new DB migrations + seeds demo data
```

### What you get out of the box

`make migrate` seeds 5 demo agents, the default phone route, and the global tool library:

| Slug | Description |
|---|---|
| `basic` | Open-ended Q&A assistant (local STT/LLM/TTS) |
| `customer_service` | Guided tech-support agent (Alex) |
| `storyteller` | Collaborative story builder |
| `language_tutor` | English conversation practice tutor |
| `orchestrator` | Hotel concierge — delegates to specialist subagents (requires Anthropic API key) |

Global tools pre-loaded: `get_current_time`, `get_weather` (served by `tools-server`).

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

The flow editor is a visual drag-and-drop canvas (React Flow). Nodes are colored cards; connections are drawn by pulling from the ● handle at the bottom of any node. Click a node or edge to open a side panel with typed form fields. Select a node and drag its corners to resize it. The JSON stays as the underlying format and is auto-synced — a "View JSON" toggle reveals it.

For a complete technical deep-dive, see [**FLOWS.md**](./FLOWS.md).

### 📋 Calls — call history and transcripts

Every completed call is logged automatically. The table shows:

| Column | Inbound | Outbound |
|---|---|---|
| **From** | Caller's number (`CALLERID` from Asterisk) | Your caller ID (e.g. `"Acme <+10000000>"`) |
| **To** | Your DID that was dialed | Destination number/endpoint |
| **Agent** | Which agent handled the call | — |
| **Duration** | — | — |
| **Turns** | STT→LLM→TTS round-trips | — |
| **Providers** | STT / LLM / TTS used | — |
| **Reason** | How the call ended (`hangup`, `error`, …) | — |

Click **Transcript** on any row to view the full chat-bubble replay.

All tables support **live search** (filter any column as you type) and **sortable columns** (click a header to sort ascending → descending → original).

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

Postgres tables: `agents`, `provider_configs`, `tool_definitions`, `specialist_configs`, `config_versions`, `phone_routes`, `call_logs`, `flows`, `flow_executions`, `flow_events`, `users`, `api_keys`.

Migrations live in `config-api/migrations/`. Run `make migrate` after pulling new ones.

---

## Authentication and accounts

### Dev mode (no auth)

Leave `ADMIN_PASSWORD` and `GITHUB_CLIENT_ID` blank in `.env` — the admin UI and REST API are fully open. Use this for local development.

### Admin password (single user)

Set `ADMIN_PASSWORD=your-password` and `API_KEY=<random-hex>` in `.env`:

```bash
# Generate a secure API key
python3 -c "import secrets; print(secrets.token_hex(32))"
```

- Admin UI: prompted for password at `/admin/login`
- REST API: pass `X-Api-Key: <value>` header
- Admin backdoor sees **all** resources across all users

### GitHub OAuth (multi-user)

1. Create a GitHub OAuth App at **github.com/settings/developers → OAuth Apps**
   - Homepage: your server URL
   - Callback URL: `http://localhost:8080/admin/auth/github/callback`
2. Set in `.env`:
   ```
   GITHUB_CLIENT_ID=your-client-id
   GITHUB_CLIENT_SECRET=your-client-secret
   BASE_URL=http://localhost:8080
   ```
3. Restart: `docker compose up -d config-api`

Each GitHub account gets its own isolated workspace (agents, flows, routes, calls). Demo agents seeded by `make migrate` have no owner and are **visible to all users** — great for getting started. Each user can create their own agents and only sees their own data plus the shared demo content.

### REST API authentication

There are two kinds of API keys:

**Global key** (`API_KEY` env var) — admin-level, no owner filter, set once for the whole server:
```bash
curl -H "X-Api-Key: $API_KEY" http://localhost:8080/api/outbound/originate ...
```

**Per-user keys** — scoped to the user's own agents and calls. Generated in the admin UI:
1. Sign in with GitHub
2. Click your avatar (top-right) → **API Keys** → **New key**
3. Copy the `sk-va-…` key — shown once, only the hash is stored
4. Use it in any API call:

```bash
curl -X POST http://localhost:8080/api/outbound/originate \
  -H "X-Api-Key: sk-va-abc123..." \
  -H "Content-Type: application/json" \
  -d '{"destination": "+15551234567", "agent_slug": "basic"}'
# Only succeeds for agents you own
```

Each user can create multiple named keys (e.g. "CI pipeline", "Campaign manager") and revoke them individually. `last_used_at` is tracked per key.

```bash
# Full API docs
open http://localhost:8080/docs
```

---

## Workflow across laptops

```bash
git pull
make up        # rebuild images
make migrate   # apply new DB migrations + seed demo data
```

Set your own `ASTERISK_EXTERNAL_IP`, `POSTGRES_PASSWORD`, and optionally `ADMIN_PASSWORD`/`API_KEY` in `.env`. Agent configs and call history live in Postgres (persisted in the `postgres_data` Docker volume). The demo agents from `make migrate` are always re-seeded if missing.

---

## Testing

### End-to-end call test

Runs the full stack automatically — SIP registration, an inbound call, and an outbound call — and reports pass/fail:

```bash
./scripts/test-e2e.sh
```

Requires `baresip` and `ffmpeg` (`brew install baresip ffmpeg`). The script:

1. Verifies all services are reachable and routes are applied
2. Generates real speech audio via the local TTS service (falls back to synthetic tone)
3. **Inbound:** baresip dials `sip:1000@<LAN_IP>` → Asterisk → agent; confirms pipeline started, call logged, at least 1 STT→LLM→TTS turn completed
4. **Outbound:** `POST /api/outbound/originate` → Asterisk AMI → baresip auto-answers → agent; confirms AudioSocket channel active, call logged with `direction=outbound`
5. Prints a summary of the last 5 calls from the DB

Run `./scripts/test-e2e.sh --no-cleanup` to keep the temp directory for log inspection on failure.

### Test individual AI services

```bash
./scripts/test-tts.sh "Hello, is this working?"
./scripts/test-stt.sh path/to/audio.wav
./scripts/test-llm.sh "Who are you?"
```

## Debugging

```bash
make logs                          # all services
make logs-agent                    # default agent container
docker logs agent-basic --tail=50  # route-managed agent container
make logs-tts / logs-llm / logs-stt / logs-asterisk
make cli                           # Asterisk CLI
make shell                         # shell into agent container

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
