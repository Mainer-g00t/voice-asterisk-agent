-- Migration 007: Call Flows
-- Adds flow definitions, per-call execution state, and event audit log.
-- Flows are opt-in: existing agents and routes are unchanged.

CREATE TABLE flows (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    description TEXT,
    -- JSON definition shape:
    --   { "entry_node_id": "n1",
    --     "nodes": [{"id":"n1","type":"conversation","label":"...","config":{...}}, ...],
    --     "edges": [{"id":"e1","source":"n1","target":"n2","condition":{...}}, ...] }
    -- Node types: conversation | say | gather_dtmf | transfer | webhook | set_variable | condition | end
    -- Condition types: default | keyword_matched | turn_count_gte | dtmf_digit |
    --                  silence_timeout | tool_result | intent_is | variable_equals |
    --                  call_no_answer | webhook_field
    definition  JSONB NOT NULL DEFAULT '{}',
    is_active   BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Per-call execution record — one row per call that uses a flow
CREATE TABLE flow_executions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    flow_id         UUID NOT NULL REFERENCES flows(id),
    call_uuid       TEXT UNIQUE,               -- links to call_logs.call_uuid
    current_node_id TEXT,
    -- Runtime state: turn_count, last_dtmf, flow variables, last_tool_result
    state           JSONB NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'running',
    -- status values: running | completed | failed | transferred | no_answer
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at        TIMESTAMPTZ
);

CREATE INDEX idx_flow_executions_call ON flow_executions(call_uuid);
CREATE INDEX idx_flow_executions_flow ON flow_executions(flow_id);

-- Audit log — every node entry, edge traversal, and event received
CREATE TABLE flow_events (
    id           BIGSERIAL PRIMARY KEY,
    execution_id UUID NOT NULL REFERENCES flow_executions(id),
    ts           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    event_type   TEXT NOT NULL,
    -- event_type values: node_entered | edge_taken | event_received |
    --                    condition_checked | transition_fired | execution_ended
    node_id      TEXT,
    edge_id      TEXT,
    data         JSONB
);

CREATE INDEX idx_flow_events_execution ON flow_events(execution_id, ts);

-- Attach an optional flow to a phone route (inbound calls)
ALTER TABLE phone_routes ADD COLUMN IF NOT EXISTS flow_id UUID REFERENCES flows(id);

-- Attach an optional flow to an agent (used when originating outbound calls)
ALTER TABLE agents ADD COLUMN IF NOT EXISTS flow_id UUID REFERENCES flows(id);
