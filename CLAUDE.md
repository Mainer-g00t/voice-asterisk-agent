# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A local voice AI agent that answers phone calls via Asterisk PBX. A SIP softphone connects to Asterisk over SIP; Asterisk routes the call to a Python agent container via the AudioSocket protocol; the agent runs a Pipecat pipeline: STT → LLM → TTS.

```
Softphone ──SIP──▶ Asterisk (Docker) ──AudioSocket──▶ agent (Docker)
                                                           │
                                              stt ◀────────┤
                                              llm ◀────────┤
                                              tts ◀────────┘
```

All five services run in Docker (`docker-compose.yml`). Everything works locally with no cloud API keys.

## Common commands

```bash
make up            # build images and start all services in background
make down          # stop and remove containers
make restart       # rebuild and restart only the agent (faster than make up)
make logs          # stream logs from all services
make logs-agent    # stream logs from a single service (also: logs-tts, logs-llm, logs-stt, logs-asterisk)
make cli           # open Asterisk CLI (use `pjsip show endpoints` to verify softphone registration)
make shell         # shell into the agent container

# Test individual AI services
./scripts/test-stt.sh
./scripts/test-tts.sh
./scripts/test-llm.sh
```

## Architecture

### AudioSocket transport (`agent/transport/audiosocket.py`)

Pipecat has no native Asterisk support, so this project implements the [AudioSocket protocol](https://docs.asterisk.org/Configuration/Channel-Drivers/AudioSocket/) as a custom `BaseTransport`. Key details:

- **Protocol**: each frame is `[type:1B][length:2B big-endian][payload:NB]`. Types: `0x00` hangup, `0x01` UUID, `0x03` DTMF, `0x10` audio.
- **Resampling**: Asterisk sends/receives 8 kHz PCM; the pipeline runs at 16 kHz. The transport resamples in both directions.
- **Output pacing**: Pipecat's audio output task flushes frames as fast as the queue empties. Asterisk's internal AudioSocket queue is fixed-size — bursts overflow it and cause choppy audio. The output transport deliberately paces each 20 ms chunk with `asyncio.sleep(0.020)` to match real-time playback.

### Server (`agent/server.py`)

An asyncio TCP server on port 9099. Each incoming connection spawns one `AudioSocketTransport` + one Pipecat `PipelineTask`. The pipeline is created *before* `transport.connect()` so that `on_client_connected` event handlers are registered before they fire. Calls end via `EndFrame` (hangup) not inactivity timeout.

### Pipeline (`agent/pipeline.py`)

`transport.input() → STT → user aggregator (with SileroVAD) → LLM → TTS → transport.output() → assistant aggregator`

The greeting is triggered by injecting a synthetic `"Hello"` user message into the context on `on_client_connected`, which makes the bot speak first without waiting for the caller.

### Provider and agent selection

All controlled via `.env` / environment variables — no code changes needed to switch providers:

| Variable | Default | Options |
|---|---|---|
| `STT_PROVIDER` | `local` | `local`, `deepgram`, `openai` |
| `LLM_PROVIDER` | `local` | `local`, `anthropic`, `openai` |
| `TTS_PROVIDER` | `local` | `local`, `cartesia`, `openai` |
| `AGENT_MODE` | `basic` | `basic`, `customer_service`, `storyteller`, `language_tutor` |

`local` providers hit the `stt`/`llm`/`tts` Docker services on the internal `voiceai` network. The local TTS service wraps Piper behind an OpenAI-compatible API; it ignores the `voice` name but Pipecat validates that a valid OpenAI voice string is passed.

### Local AI services

- **STT** (`stt/`): Whisper `tiny` model, OpenAI-compatible API on port 8000.
- **LLM** (`llm/`): Ollama with `smollm2:135m` by default. Model data persisted in a `ollama_data` Docker volume. Try `llama3.2:1b` for better quality.
- **TTS** (`tts/`): Piper TTS, OpenAI-compatible API on port 5000 (mapped to 5001 on host — macOS AirPlay occupies 5000).

### Asterisk config (`asterisk/`)

- `pjsip.conf` — SIP transport and a single endpoint (`softphone`) with password `1234`.
- `extensions.conf` — all calls from `[from-softphone]` context are answered and routed to `AudioSocket(${CALL_UUID}, agent:9099)`.
- `rtp.conf` — RTP port range 10000–10100.
- `docker-entrypoint.sh` — substitutes `ASTERISK_EXTERNAL_IP` into `pjsip.conf` at container start (needed for RTP NAT traversal when baresip and Docker are on different hosts).

### Adding a new agent mode

1. Create `agent/agents/<name>.py` with a `SYSTEM_PROMPT` string and optionally `GREETING_TRIGGER`.
2. Add an entry to the `_AGENTS` dict in `agent/pipeline.py`.
3. Set `AGENT_MODE=<name>` in `.env` and `make restart`.

### Multi-agent / tool-calling agents

An agent module can optionally export two additional symbols that `pipeline.py` picks up automatically:

- `TOOLS: ToolsSchema` — tool definitions passed to `LLMContext`. The orchestrator LLM sees these as callable tools.
- `register_tools(llm)` — called once per call to wire async handler functions onto the LLM service via `llm.register_function(name, handler)`.

**`orchestrator`** (`agents/orchestrator.py`) is the reference implementation: a hotel concierge that delegates every request to a specialist subagent. When the orchestrator LLM calls `route_to_specialist(specialist, query)`, the handler spawns a real second LLM call (claude-haiku) with a specialist-specific system prompt, then returns the answer back into the conversation. This demonstrates full multi-agent delegation — two separate LLMs cooperating on a single voice call.

To use it:
```bash
# in .env
AGENT_MODE=orchestrator
LLM_PROVIDER=anthropic      # tool calling + subagent calls require Anthropic
ANTHROPIC_API_KEY=sk-...
```

## Key gotcha: `ASTERISK_EXTERNAL_IP`

Set this in `.env` to your Mac's LAN IP (`ipconfig getifaddr en0`) when baresip and Docker are on different machines. Defaults to `127.0.0.1`, which works when both run on the same host.
