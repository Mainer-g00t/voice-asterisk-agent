# Call Flows — Complete Reference

Flows turn a single-prompt voice agent into a **multi-step call script** — an explicit graph of nodes connected by conditional edges. A call enters the graph at the entry node and traverses edges until it reaches an `end` node or the caller hangs up.

---

## Table of Contents

1. [Mental model](#1-mental-model)
2. [Architecture — where the code lives](#2-architecture--where-the-code-lives)
3. [Lifecycle of a call with a flow](#3-lifecycle-of-a-call-with-a-flow)
4. [Node types](#4-node-types)
5. [Edge conditions](#5-edge-conditions)
6. [Flow state](#6-flow-state)
7. [Event processing — how edges fire](#7-event-processing--how-edges-fire)
8. [Node actions — what happens on transition](#8-node-actions--what-happens-on-transition)
9. [Flow definition JSON](#9-flow-definition-json)
10. [Example flows](#10-example-flows)
11. [Visual editor](#11-visual-editor)
12. [Execution history and analytics](#12-execution-history-and-analytics)
13. [Assigning flows to agents](#13-assigning-flows-to-agents)
14. [Outbound calls with flows](#14-outbound-calls-with-flows)

---

## 1. Mental model

A flow is a **finite state machine** where:

- **Nodes** are states — each one describes what the agent does while it is "in" that node (talk, play audio, wait for a keypress, call a webhook, etc.)
- **Edges** are transitions — each one has a condition that is evaluated on every incoming event
- **State** is a dict that travels with the call — it records what happened so far (transcripts, DTMF presses, turn counts, webhook results, user-defined variables)

```
           ┌─────────────────────────────────────────────────────────┐
           │                      FLOW EXECUTION                     │
           │                                                         │
           │  ┌──────────────┐  edge fires   ┌──────────────┐       │
  CALL ───▶│  │  entry node  │──────────────▶│  next node   │ · · · │──▶ END
   START   │  └──────────────┘               └──────────────┘       │
           │        │  events arrive                                  │
           │        │  (transcript, dtmf,                            │
           │        │   turn_end, tool_result…)                      │
           └─────────────────────────────────────────────────────────┘
```

No flow = flat single-prompt agent (the original mode). Assign a flow to an agent and the pipeline switches to state-machine mode for every call to that agent.

---

## 2. Architecture — where the code lives

```
packages/voiceai_common/flow_engine.py   ← stateless evaluation engine (single source of truth)
         │                                  copied into both config-api and agent at build time
         │
         ├── config-api/flow_engine.py    ← used for validation on save, analytics
         └── agent/flow_engine.py         ← used at runtime, zero network round-trips per event

agent/flow_controller.py                 ← per-call state machine (wraps flow_engine)
agent/flow_watcher.py   (FlowWatcherProcessor)  ← Pipecat FrameProcessor: watches the pipeline
                                                   and feeds events into FlowController
agent/server.py         (_flow_transition_handler)  ← acts on FlowTransitionFrame (TTS inject,
                                                       AMI redirect, hangup, etc.)
config-api/routers/flows.py              ← CRUD for flow definitions + executions API
config-api/routers/internal.py           ← /internal/flows/init-execution (called at call start)
config-api/migrations/
  005_flows.sql          ← flows, flow_executions, flow_events tables
```

The engine (`flow_engine.py`) is **purely functional** — `apply_event(flow_def, node_id, state, event) → (new_node_id, new_state, edge | None)`. It has no I/O, no database, no side effects. Both config-api and the agent install it as the same pip package (`voiceai_common`) so the evaluation logic is never duplicated.

---

## 3. Lifecycle of a call with a flow

### Inbound call

```
Asterisk dialplan
  └── CURL /internal/calls/pre-register   (stores caller_id in call_logs)
  └── AudioSocket ──────────────────────────────────────────────────────────────┐
                                                                                 │
agent/server.py receives TCP connection                                          │
  └── pipeline.py: create_pipeline_task()                                        │
        ├── reads agent config from Redis                                         │
        ├── reads per-call template vars from Redis (call:vars:{uuid})           │
        ├── sees flow_id in agent config                                          │
        ├── calls POST /internal/flows/init-execution                            │
        │     └── creates flow_executions row in Postgres                        │
        │     └── warms Redis: flow:exec:{call_uuid}                             │
        ├── overrides system_prompt + greeting from entry node config            │
        ├── instantiates FlowController + FlowWatcherProcessor                   │
        └── builds and starts the Pipecat pipeline                               │
                                                                                 │
                 ┌───────────────────────────────────────────────────────────┐   │
                 │  Pipecat pipeline (running)                               │   │
                 │                                                           │◀──┘
                 │  AudioSocketInput → STT → FlowWatcher → UserAggregator   │
                 │                              │                           │
                 │                              │ events                    │
                 │                              ▼                           │
                 │                       FlowController                     │
                 │                              │                           │
                 │                              │ FlowTransitionFrame       │
                 │                              ▼                           │
                 │                    transition_queue (asyncio)            │
                 │                              │                           │
                 │  LLM → TTS → MetricsCapture → AudioSocketOutput          │
                 └───────────────────────────────────────────────────────────┘
                                                │
server.py: _flow_transition_handler() (third asyncio Task)
  └── reads transition_queue
  └── acts on each FlowTransitionFrame:
        conversation → update system prompt in LLMContext
        say          → inject TTS message via LLMContextFrame
        gather_dtmf  → (no-op; FlowController listens for DTMF)
        webhook      → HTTP POST; result fed back as webhook_result event
        set_variable → delegate to FlowController
        condition    → delegate to FlowController
        transfer     → Asterisk AMI Redirect
        end          → cancel pipeline task
```

### Outbound call

```
POST /api/outbound/originate
  ├── pre-creates call_logs row (direction=outbound)
  ├── stores template_vars in Redis: call:vars:{uuid}
  ├── if flow_id: creates flow_executions + warms Redis: flow:exec:{uuid}
  ├── ensures agent-{slug} container is running
  └── sends AMI Originate → Asterisk dials destination

When answered: Asterisk runs [outbound-agent] dialplan → AudioSocket → agent
  └── same pipeline path as inbound from here
      (flow already initialized in Redis — no extra DB round-trip)
```

---

## 4. Node types

### Overview

```
  ┌─────────────────────────────────────────────────────────────────────────┐
  │  NODE TYPES                                                             │
  │                                                                         │
  │  conversation 🟦  ←── AI loop: STT → LLM → TTS, stays until edge fires │
  │  say          🟨  ←── one-shot TTS, instant default edge               │
  │  gather_dtmf  🟧  ←── waits for keypad press, branches on digit        │
  │  transfer     🟥  ←── AMI Redirect, call handed off                    │
  │  webhook      🟪  ←── HTTP POST, branches on response field            │
  │  set_variable 🟩  ←── writes into state.variables, instant default     │
  │  condition    🟫  ←── silent branch on a variable value                │
  │  end          ⬛  ←── hangs up, marks execution completed              │
  └─────────────────────────────────────────────────────────────────────────┘
```

---

### `conversation` 🟦

The full AI loop: speech-to-text → LLM → text-to-speech. The agent stays in this node, accumulating turns, until an outgoing edge condition fires.

| Config field | Type | Description |
|---|---|---|
| `system_prompt` | string | System prompt sent to the LLM. Supports `{placeholder}` substitution. |
| `greeting` | string | Optional message spoken aloud when this node is first entered. |
| `silence_timeout_seconds` | number | Seconds of silence with no user speech before a `silence_timeout` edge fires. 0 = disabled. |

**Events that can trigger outgoing edges while in this node:**
- `transcription` — every user speech turn → `keyword_matched`, `variable_equals`
- `turn_end` — after every agent response → `turn_count_gte`
- `tool_result` — after any tool call → `tool_result`, `variable_equals`
- `silence_timeout` — from transport → `silence_timeout`
- `dtmf` — keypad press at any time → `dtmf_digit`, `variable_equals`

---

### `say` 🟨

Plays a fixed TTS message verbatim. No STT or LLM involved. Immediately follows the `default` edge after the message finishes playing.

| Config field | Type | Description |
|---|---|---|
| `message` | string | Text to speak. Supports `{placeholder}` substitution from `template_vars`. |

> Use `say` for confirmations, menu announcements, and error messages where you don't want the LLM to improvise.

---

### `gather_dtmf` 🟧

Waits for the caller to press a keypad digit (0–9, `*`, `#`). The digit is stored in `last_dtmf`. Branch using `dtmf_digit` edges (one per expected digit) plus a `default` fallback for timeout or unexpected input.

| Config field | Type | Description |
|---|---|---|
| `prompt` | string | Optional TTS message spoken before waiting. Supports `{placeholder}`. |
| `dtmf_timeout` | number | Seconds to wait for a digit before taking the `default` edge (default: 10). |

**DTMF signal path:**

```
Asterisk sends AudioSocket frame type 0x03 (DTMF)
  └── AudioSocketInputTransport._read_loop()
        └── emits DTMFInputFrame(digit)
              └── FlowWatcherProcessor intercepts it
                    └── feeds event {"type": "dtmf", "digit": "1"} into FlowController
                          └── evaluates dtmf_digit edges → fires transition
```

---

### `transfer` 🟥

Hands the call off to a different extension or phone number using Asterisk AMI Redirect. The flow ends after the transfer — the AI pipeline stops immediately.

| Config field | Type | Description |
|---|---|---|
| `destination` | string | Extension or E.164 number (e.g. `operator`, `+15551234567`). |
| `dialplan_context` | string | Asterisk dialplan context to redirect into (default: `default`). |

**Transfer signal path:**

```
FlowTransitionFrame(node_type="transfer")
  └── server.py _flow_transition_handler()
        └── ami_client.redirect(channel, destination, context)
              └── AMI Action: Redirect
                    └── Asterisk bridges call to destination extension
```

---

### `webhook` 🟪

HTTP POSTs the current call state to an external URL and waits for a JSON response. The response is stored in `last_webhook_result`. Branch using `webhook_field` edges or `variable_equals` on `last_webhook_result`.

| Config field | Type | Description |
|---|---|---|
| `url` | string | Endpoint to POST to. Must be reachable from inside Docker (e.g. `http://tools-server:8100/qualify`). |
| `timeout` | number | Request timeout in seconds (default: 10). |

**Request body sent to `url`:**
```json
{
  "call_uuid": "a1b2c3d4-...",
  "current_node_id": "n3",
  "state": {
    "turn_count": 2,
    "last_dtmf": "1",
    "last_transcript": "I'd like to know more about the premium plan",
    "variables": { "plan": "premium" }
  }
}
```

**Example response and branching:**
```json
{ "action": "qualify", "score": 85, "eligible": true }
```

```
edge: webhook_field  field="action"   value="qualify"   → leads to qualify node
edge: webhook_field  field="eligible" value="false"     → leads to ineligible node
edge: default                                           → leads to fallback node
```

---

### `set_variable` 🟩

Writes a key-value pair into `state.variables`, then immediately follows the `default` edge. No audio. Use before a `condition` node to prepare a branch value.

| Config field | Type | Description |
|---|---|---|
| `variable_name` | string | Key to store in `state.variables`. |
| `value` | string | Value to store. Supports `{placeholder}` substitution. |

---

### `condition` 🟫

Silent branch — evaluates a variable (built-in or user-defined) and follows the matching `variable_equals` edge. No audio, no LLM.

| Config field | Type | Description |
|---|---|---|
| `variable_name` | string | The variable to evaluate. Can be any built-in state key (`last_dtmf`, `turn_count`, etc.) or a user-defined variable. |

Draw one `variable_equals` edge per expected value, plus a `default` edge for the unmatched case.

```
condition node (variable_name = "plan")
  ├── variable_equals  var="plan"  value="premium"  → premium upsell node
  ├── variable_equals  var="plan"  value="basic"    → basic info node
  └── default                                       → unknown plan node
```

---

### `end` ⬛

Hangs up the call and marks the flow execution as `completed`. No configuration. Always the terminal node.

---

## 5. Edge conditions

Edges are evaluated in definition order for each incoming event. **Non-default edges are evaluated first**, then the `default` edge as a fallback. The first matching edge wins — subsequent edges are not evaluated.

```
  outgoing edges from node N
  ┌──────────────────────────────────────────────────────────┐
  │  1. keyword_matched  words=["bye", "cancel"]             │  ← evaluated first
  │  2. turn_count_gte   n=10                                │
  │  3. tool_result      tool="check_crm" field="ok" val="1" │
  │  4. default                                              │  ← evaluated last
  └──────────────────────────────────────────────────────────┘
         first match wins → FlowTransitionFrame emitted
```

### Condition reference

| Type | Parameters | Fires when… |
|---|---|---|
| `keyword_matched` | `words: ["bye", "stop"]` | The user's last transcript contains any of the words (case-insensitive substring match). |
| `turn_count_gte` | `n: 5` | `turn_count` ≥ N. Checked after every `turn_end` event. |
| `dtmf_digit` | `digit: "1"` | The user pressed the specified key. Checked after every `dtmf` event. |
| `silence_timeout` | _(none)_ | No speech for `silence_timeout_seconds`. Driven by the transport. |
| `tool_result` | `tool: "check_crm"` `field: "eligible"` `value: "true"` | The named tool's JSON result has `result[field] == value`. |
| `intent_is` | `intent: "transfer_to_human"` | The classified intent equals the value. |
| `variable_equals` | `var: "last_dtmf"` `value: "1"` | `state.variables[var]` or any built-in state key equals value. User-defined vars take precedence on name collision. |
| `call_no_answer` | _(none)_ | The outbound call was not answered (`call_status == "no_answer"`). |
| `webhook_field` | `field: "action"` `value: "transfer"` | `last_webhook_result[field] == value`. |
| `default` | _(none)_ | Always matches. Use as the last edge from every node. |

> **Tip:** `variable_equals` is the most general condition — it can check `last_dtmf`, `turn_count`, `last_transcript`, `last_webhook_result`, or any user-defined variable set by a `set_variable` node.

---

## 6. Flow state

Every call execution carries a `state` dict that persists across node transitions. The engine updates it on every event before evaluating edges.

### Built-in variables (auto-updated)

| Variable | Updated on | Value |
|---|---|---|
| `turn_count` | every `turn_end` event | Integer (as string): `"0"`, `"1"`, … |
| `last_dtmf` | every `dtmf` event | Single character: `"1"`, `"*"`, `"#"`, … |
| `last_transcript` | every `transcription` event | Full STT text of what the user said |
| `last_tool_results` | every `tool_result` event | Dict `{tool_name: result_dict}` |
| `last_webhook_result` | after a `webhook` node | Full JSON response dict |
| `last_intent` | every `intent` event | Intent label string |
| `call_status` | outbound call setup | `"no_answer"` or `"busy"` if unanswered |

### User-defined variables

Written by `set_variable` nodes, stored under `state.variables`:

```json
{
  "turn_count": "3",
  "last_dtmf": "2",
  "last_transcript": "I want the premium plan",
  "variables": {
    "plan": "premium",
    "customer_tier": "gold"
  }
}
```

`variable_equals` checks `state.variables` first, then falls back to built-in state keys. This means user-defined variables take precedence over built-ins on name collision.

### State persistence

State is stored in Redis at `flow:exec:{call_uuid}` and updated after every transition. At call end, the final state (plus all events) is bulk-posted to `POST /api/flows/executions/complete` and written to Postgres for analytics.

---

## 7. Event processing — how edges fire

```
                        EVENTS THAT FEED INTO THE ENGINE
  ┌──────────────────────────────────────────────────────────────────────┐
  │                                                                      │
  │  STT transcript ready ──▶  {"type": "transcription", "text": "..."}  │
  │  Agent turn completed ──▶  {"type": "turn_end", "turn_count": N}     │
  │  DTMF keypress ────────▶  {"type": "dtmf", "digit": "1"}            │
  │  Tool call returned ───▶  {"type": "tool_result", "tool": "...",     │
  │                                                   "result": {...}}   │
  │  Webhook returned ─────▶  {"type": "webhook_result", "result": {...}}│
  │  Silence timeout ──────▶  {"type": "silence_timeout"}               │
  │  Intent classified ────▶  {"type": "intent", "intent": "..."}       │
  │  Variable written ─────▶  {"type": "set_variable", "var", "value"}  │
  └──────────────────────────────────────────────────────────────────────┘
                   │
                   ▼
          flow_engine.apply_event(flow_def, current_node_id, state, event)
                   │
          ┌────────┴─────────┐
          │                  │
     edge matched       no edge matched
          │                  │
          ▼                  ▼
   FlowTransitionFrame   stay in current node,
   → transition_queue    update state only
```

`FlowWatcherProcessor` is a Pipecat `FrameProcessor` inserted between STT and the user aggregator in the pipeline. It intercepts:

- `TranscriptionFrame` → feeds `transcription` event
- `LLMFullResponseEndFrame` → feeds `turn_end` event
- `DTMFInputFrame` → feeds `dtmf` event

`FlowController` wraps the engine, manages state persistence (Redis read/write), and emits `FlowTransitionFrame` onto a `transition_queue` when an edge fires.

---

## 8. Node actions — what happens on transition

When a `FlowTransitionFrame` arrives on the `transition_queue`, `server.py`'s `_flow_transition_handler()` acts synchronously with the pipeline:

| Node type | Action taken |
|---|---|
| `conversation` | Updates `LLMContext.system_prompt` to the new node's `system_prompt`. If the node has a `greeting`, injects a `say` message first. |
| `say` | Injects an `LLMContextFrame` that plays the `message` through TTS without hitting the LLM. |
| `gather_dtmf` | Speaks the optional `prompt` via TTS, then waits silently. `FlowWatcherProcessor` continues listening for `DTMFInputFrame`. |
| `webhook` | Makes an HTTP POST (inside `_flow_transition_handler()`). Feeds the response as a `webhook_result` event back into `FlowController` to evaluate `webhook_field` edges. |
| `set_variable` | Calls `FlowController.apply_event()` with a `set_variable` event, updating `state.variables`. Then immediately evaluates `default` edges. |
| `condition` | Calls `FlowController.apply_event()` with a synthetic `condition` event. Evaluates `variable_equals` edges on the `variable_name` field. |
| `transfer` | Calls `ami_client.redirect(channel, destination, context)`. AMI sends `Action: Redirect` to Asterisk. Pipeline continues briefly then is cancelled. |
| `end` | Cancels the `PipelineTask` — audio stops, call logger runs, call_log is posted to config-api. Asterisk detects TCP close and hangs up. |

---

## 9. Flow definition JSON

Stored as JSONB in the `flows.definition` column. `_positions` holds canvas coordinates for the visual editor and is ignored by the engine.

```json
{
  "entry_node_id": "n1",
  "nodes": [
    {
      "id": "n1",
      "type": "conversation",
      "label": "Greeting",
      "config": {
        "system_prompt": "You are a support agent for Acme. Greet the customer warmly and ask how you can help.",
        "greeting": "Hello! Thanks for calling Acme support. How can I help you today?",
        "silence_timeout_seconds": 20
      }
    },
    {
      "id": "n2",
      "type": "gather_dtmf",
      "label": "Main menu",
      "config": {
        "prompt": "Press 1 for billing, press 2 for technical support, press 3 to speak with an agent.",
        "dtmf_timeout": 8
      }
    },
    {
      "id": "n3",
      "type": "say",
      "label": "Billing message",
      "config": {
        "message": "Transferring you to the billing department. Please hold."
      }
    },
    {
      "id": "n4",
      "type": "transfer",
      "label": "Transfer billing",
      "config": {
        "destination": "billing",
        "dialplan_context": "default"
      }
    },
    {
      "id": "n5",
      "type": "conversation",
      "label": "Tech support",
      "config": {
        "system_prompt": "You are a technical support agent. Help the customer troubleshoot their issue step by step.",
        "greeting": "I'll help you with your technical issue. Can you describe the problem?"
      }
    },
    {
      "id": "n6",
      "type": "transfer",
      "label": "Transfer human",
      "config": { "destination": "operator" }
    },
    {
      "id": "n7",
      "type": "end",
      "label": "End",
      "config": {}
    }
  ],
  "edges": [
    {"id": "e1", "source": "n1", "target": "n2",
     "condition": {"type": "turn_count_gte", "n": 1}},
    {"id": "e2", "source": "n2", "target": "n3",
     "condition": {"type": "dtmf_digit", "digit": "1"}},
    {"id": "e3", "source": "n2", "target": "n5",
     "condition": {"type": "dtmf_digit", "digit": "2"}},
    {"id": "e4", "source": "n2", "target": "n6",
     "condition": {"type": "dtmf_digit", "digit": "3"}},
    {"id": "e5", "source": "n2", "target": "n7",
     "condition": {"type": "default"}},
    {"id": "e6", "source": "n3", "target": "n4",
     "condition": {"type": "default"}},
    {"id": "e7", "source": "n5", "target": "n6",
     "condition": {"type": "keyword_matched", "words": ["human", "agent", "person"]}},
    {"id": "e8", "source": "n5", "target": "n7",
     "condition": {"type": "turn_count_gte", "n": 20}}
  ],
  "_positions": {
    "n1": {"x": 300, "y": 50},
    "n2": {"x": 300, "y": 220},
    "n3": {"x": 100, "y": 400},
    "n4": {"x": 100, "y": 560},
    "n5": {"x": 500, "y": 400},
    "n6": {"x": 500, "y": 560},
    "n7": {"x": 700, "y": 400}
  }
}
```

---

## 10. Example flows

### IVR menu (DTMF)

```
  ┌─────────────────┐
  │   conversation  │  "Hello! How can I help?"
  │   (Greeting)    │
  └────────┬────────┘
           │ turn_count_gte n=1
           ▼
  ┌─────────────────┐
  │   gather_dtmf   │  "Press 1 for sales, 2 for support, 3 to repeat"
  │   (Main menu)   │
  └──┬──────┬───┬───┘
     │      │   │
   "1"    "2" default (timeout)
     │      │   │
     ▼      │   ▼
  ┌──────┐  │  ┌──────────────┐
  │ say  │  │  │   say        │  "Sorry, I didn't catch that. Let me repeat."
  │Sales │  │  │  (Repeat)    │
  └──┬───┘  │  └──────┬───────┘
     │      │         │ default
     ▼      │         └──────────────────────────┐
  ┌──────┐  │                                    │
  │transf│  ▼                                    │
  │sales │ ┌─────────────────────────────────┐   │
  └──────┘ │        conversation             │   │
           │      (Tech support)             │   │
           └───────────────┬─────────────────┘   │
                           │ keyword: "human"     │
                           ▼                     │
                       ┌──────┐                  │
                       │transf│                  │
                       │ oper │◀─────────────────┘
                       └──────┘
```

---

### Outbound sales call with webhook qualification

```
  ┌─────────────────────────────────┐
  │         conversation            │  "Hi {name}, calling about {product}…"
  │         (Intro)                 │  system_prompt: "You are a sales agent…"
  └──────────┬──────────────────────┘
             │ keyword: ["interested", "tell me more"]
             ▼
  ┌─────────────────────────────────┐
  │           webhook               │  POST /qualify  body: {call_uuid, state}
  │        (Check CRM)              │  → {"eligible": true, "tier": "gold"}
  └──┬──────────────────────────────┘
     │
     ├─ webhook_field  field="eligible"  value="true"
     │         ▼
     │  ┌─────────────────────────────────┐
     │  │         conversation            │  pitch gold tier plan
     │  │         (Gold pitch)            │
     │  └──┬──────────────────────────────┘
     │     │ keyword: ["yes", "sign me up"]
     │     ▼
     │  ┌──────┐
     │  │ end  │
     │  └──────┘
     │
     └─ default (not eligible)
               ▼
        ┌─────────────────┐
        │       say       │  "Thank you for your time. Goodbye."
        └────────┬────────┘
                 │ default
                 ▼
              ┌──────┐
              │ end  │
              └──────┘
```

---

### Silence detection + escalation

```
  ┌─────────────────────────────────┐
  │         conversation            │  silence_timeout_seconds: 15
  │         (Main)                  │
  └──┬───────────────────────────┬──┘
     │                           │
     │ keyword: ["bye","goodbye"] │ silence_timeout
     ▼                           ▼
  ┌──────┐             ┌─────────────────┐
  │ end  │             │       say       │  "Are you still there?"
  └──────┘             └────────┬────────┘
                                │ default
                                ▼
                    ┌─────────────────────────┐
                    │      conversation        │  silence_timeout_seconds: 10
                    │   (Check in loop)        │
                    └──┬───────────────────┬───┘
                       │                   │
               keyword │                   │ silence_timeout
               "bye"   │                   ▼
                       │           ┌─────────────────┐
                       │           │       end        │
                       │           └─────────────────┘
                       ▼
                    ┌──────┐
                    │ end  │
                    └──────┘
```

---

### Variable branching with `set_variable` + `condition`

Sometimes you need to compute a branch value from a tool result and then route on it cleanly. Use `set_variable` → `condition`:

```
  ┌─────────────────┐
  │   conversation  │  "What plan are you on?"
  └────────┬────────┘
           │ turn_count_gte n=1
           ▼
  ┌─────────────────┐
  │    set_variable │  variable_name="plan"  value="{last_transcript}"
  └────────┬────────┘
           │ default
           ▼
  ┌─────────────────┐
  │    condition    │  variable_name="plan"
  └──┬──────────────┘
     │
     ├─ variable_equals  var="plan"  value="premium"  → premium node
     ├─ variable_equals  var="plan"  value="basic"    → basic node
     └─ default                                       → unknown node
```

---

## 11. Visual editor

The flow editor is a React + React Flow app compiled into the `config-api` Docker image. No Node.js needed on the host — `make up` builds it.

**Canvas controls:**

| Action | How |
|---|---|
| Add a node | Click a node-type button in the toolbar (top of canvas) |
| Connect nodes | Drag from the ● handle at the bottom of any node to another node |
| Edit a node | Click the node card → right side panel appears |
| Edit an edge | Click the edge label → right side panel for condition |
| Move a node | Drag the node card |
| Delete | Select → Delete/Backspace key |
| View JSON | Toggle "View JSON" at the top of the page |
| Save | Click Save (top-right) — pushes to Postgres + Redis immediately |

**Node colors match type:**

| Color | Node type |
|---|---|
| 🟦 Blue | `conversation` |
| 🟨 Yellow | `say` |
| 🟧 Orange | `gather_dtmf` |
| 🟥 Red | `transfer` |
| 🟪 Purple | `webhook` |
| 🟩 Green | `set_variable` |
| 🟫 Brown | `condition` |
| ⬛ Black | `end` |

**Editor source:** `config-api/flow-editor/src/`

To iterate on the editor UI locally (requires Node.js):
```bash
cd config-api/flow-editor
npm ci
npm run dev    # Vite dev server with hot reload
```
Then rebuild the Docker image to pick up changes: `docker compose build config-api`.

---

## 12. Execution history and analytics

Every call with a flow creates a `flow_executions` row in Postgres. The full event log is stored in `flow_events`.

**In the admin UI:**
- **Flows list** → click the run-count badge on any flow to see its executions
- **Agent edit page** → shortcut link to executions for that agent's flow

**Execution fields:**

| Field | Description |
|---|---|
| `status` | `running` → `completed` or `failed` |
| `current_node_id` | Last node reached |
| `state` | Final state dict (including all variables) |
| `started_at` / `ended_at` | Timestamps |
| `turn_count` | Extracted from final state |

**Event log** (`flow_events`): every `node_entered`, `edge_taken`, and `event_received` during the call. Append-only; written in bulk at call end via `POST /api/flows/executions/complete`.

---

## 13. Assigning flows to agents

1. Create a flow: **Admin UI → 🔀 Flows → New flow**
2. Build the graph in the visual editor, click **Save**
3. Assign to an agent: **Admin UI → 🤖 Agents → edit → Flow dropdown → pick flow → Save**

The assignment takes effect on the next call — no restart needed. In-flight calls finish with their original config.

To **remove a flow** from an agent: set the Flow dropdown to `(none)` and save.

---

## 14. Outbound calls with flows

Pass `flow_id` in the originate request:

```bash
curl -X POST http://localhost:8080/api/outbound/originate \
  -H "X-Api-Key: sk-va-..." \
  -H "Content-Type: application/json" \
  -d '{
    "destination": "+15551234567",
    "agent_slug": "sales",
    "flow_id": "uuid-of-your-flow",
    "template_vars": {
      "name": "Alex",
      "product": "Premium Plan"
    },
    "callback_url": "https://your-crm.example.com/cdr"
  }'
```

- `flow_id` overrides the agent's default flow for this call only
- `template_vars` are substituted into `{placeholder}` patterns in every node's `system_prompt`, `greeting`, `message`, and `value` fields
- The flow execution is pre-created in Postgres and cached in Redis **before** Asterisk dials, so the agent can start immediately when the call is answered
- When the call ends (any reason), the execution is finalized and the CDR is posted to `callback_url`

**Handling no-answer:**

If the destination doesn't pick up, `call_status` is set to `"no_answer"` in the flow state. Use a `call_no_answer` edge or `variable_equals var="call_status" value="no_answer"` to route to a `set_variable` node that schedules a follow-up or logs the outcome.
