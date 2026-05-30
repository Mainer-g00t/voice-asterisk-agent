-- 012_api_keys.sql — Per-user API keys
-- Each user can create multiple named keys. Raw key is shown once; only the hash is stored.

CREATE TABLE IF NOT EXISTS api_keys (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key_hash     TEXT        NOT NULL UNIQUE,   -- SHA-256 hex of the raw key
    label        TEXT        NOT NULL DEFAULT 'My key',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS api_keys_user_id_idx ON api_keys (user_id);
