-- ── phone_routes ─────────────────────────────────────────────────────────────
-- Maps a dialed number/extension to an agent slug.
-- The agent container "agent-{slug}" must be running (managed via the Apply button).
CREATE TABLE IF NOT EXISTS phone_routes (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    did         TEXT NOT NULL UNIQUE,          -- dialed number or extension pattern, e.g. "1000" or "+15551234567"
    agent_slug  TEXT NOT NULL REFERENCES agents(slug) ON DELETE RESTRICT,
    description TEXT,
    is_active   BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE TRIGGER phone_routes_updated_at
    BEFORE UPDATE ON phone_routes
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Seed a default catch-all route so existing installs keep working
INSERT INTO phone_routes (did, agent_slug, description)
VALUES ('_X.', 'basic', 'Default catch-all → basic agent')
ON CONFLICT (did) DO NOTHING;
