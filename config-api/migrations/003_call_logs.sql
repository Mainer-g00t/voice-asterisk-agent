CREATE TABLE IF NOT EXISTS call_logs (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    call_uuid        TEXT NOT NULL UNIQUE,
    agent_slug       TEXT NOT NULL,
    did              TEXT,                 -- dialed number (agent_slug → route lookup)
    started_at       TIMESTAMPTZ,
    ended_at         TIMESTAMPTZ,
    duration_seconds INT,
    turn_count       INT NOT NULL DEFAULT 0,
    transcript       JSONB,               -- [{role, content}]
    stt_provider     TEXT,
    llm_provider     TEXT,
    tts_provider     TEXT,
    end_reason       TEXT,                -- "hangup" | "error" | "unknown"
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS call_logs_agent_slug_idx ON call_logs (agent_slug);
CREATE INDEX IF NOT EXISTS call_logs_started_at_idx ON call_logs (started_at DESC);
