-- ── agents ───────────────────────────────────────────────────────────────────
CREATE TABLE agents (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug             TEXT NOT NULL UNIQUE,
    display_name     TEXT NOT NULL,
    system_prompt    TEXT NOT NULL,
    greeting_trigger TEXT NOT NULL DEFAULT 'Hello',
    is_active        BOOLEAN NOT NULL DEFAULT true,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── provider_configs ──────────────────────────────────────────────────────────
CREATE TABLE provider_configs (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id      UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    provider_type TEXT NOT NULL CHECK (provider_type IN ('stt', 'llm', 'tts')),
    provider_name TEXT NOT NULL,   -- "local" | "openai" | "anthropic" | "deepgram" | "cartesia"
    model         TEXT,            -- e.g. "claude-sonnet-4-6", "gpt-4o-mini"
    extra_config  JSONB,           -- voice IDs, base_url overrides, etc.
    UNIQUE (agent_id, provider_type)
);

-- ── tool_definitions ──────────────────────────────────────────────────────────
-- Tool schemas go in DB; handlers stay in Python code (keyed by handler_type).
CREATE TABLE tool_definitions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id        UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    tool_name       TEXT NOT NULL,
    handler_type    TEXT NOT NULL,    -- matches HANDLER_REGISTRY key in agent/tool_handlers/
    description     TEXT NOT NULL,
    parameters      JSONB NOT NULL,   -- JSON Schema "properties" object
    required_params TEXT[] NOT NULL DEFAULT '{}',
    sort_order      INT NOT NULL DEFAULT 0,
    UNIQUE (agent_id, tool_name)
);

-- ── specialist_configs ────────────────────────────────────────────────────────
-- Replaces the hardcoded _SPECIALISTS dict in orchestrator.py.
CREATE TABLE specialist_configs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id        UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    specialist_key  TEXT NOT NULL,
    display_name    TEXT NOT NULL,
    system_prompt   TEXT NOT NULL,
    subagent_model  TEXT,             -- override model for this specialist (null = use default)
    sort_order      INT NOT NULL DEFAULT 0,
    UNIQUE (agent_id, specialist_key)
);

-- ── config_versions ───────────────────────────────────────────────────────────
-- Append-only audit log; enables rollback by re-pushing a snapshot to Redis.
CREATE TABLE config_versions (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id   UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    snapshot   JSONB NOT NULL,
    changed_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── system_settings ───────────────────────────────────────────────────────────
CREATE TABLE system_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT INTO system_settings VALUES
    ('default_stt_provider', 'local'),
    ('default_llm_provider', 'local'),
    ('default_tts_provider', 'local');

-- ── updated_at trigger ────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER agents_updated_at
    BEFORE UPDATE ON agents
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ── seed data ─────────────────────────────────────────────────────────────────
INSERT INTO agents (slug, display_name, system_prompt, greeting_trigger) VALUES
('basic', 'Basic Assistant',
 'You are a helpful voice assistant. Your responses will be spoken aloud over a phone call. Keep answers short and conversational — two or three sentences maximum. Avoid bullet points, markdown, or anything that doesn''t speak naturally.',
 'Hello'),

('customer_service', 'Customer Service (Alex)',
 'You are Alex, a friendly and patient customer service representative for a tech company. Your goal is to help the caller resolve their issue step by step over the phone. Start by warmly greeting them and asking for their name and the product or service they need help with. Once you know the issue, guide them through troubleshooting one clear step at a time — wait for confirmation before moving to the next step. If the issue is resolved, confirm it and wish them a good day. If you cannot resolve it after a few steps, empathetically offer to escalate to a specialist. Keep every response short and spoken — one or two sentences. Never use bullet points, lists, or markdown.',
 'A customer is calling. Please answer the phone.'),

('storyteller', 'Collaborative Storyteller',
 'You are a collaborative storyteller. You and the caller are building a story together, taking turns — the caller adds a sentence or two, then you continue with one or two sentences, and you always end your turn with a natural pause that hands it back to them. Keep the story engaging, imaginative, and family-friendly. Open by setting a vivid scene and explicitly inviting the caller to continue it. Your responses must be short — two sentences maximum — and always end with something like ''What happens next?'' or ''What does she do?'' or a similar open invitation. Never use lists, markdown, or anything that doesn''t sound natural when spoken aloud.',
 'I''d like to build a story together. Please start.'),

('language_tutor', 'English Language Tutor',
 'You are a warm, encouraging English conversation tutor helping a student practice spoken English over the phone. Your job is to have natural conversations that make the student comfortable speaking. If the student makes a grammar or pronunciation mistake, gently weave the correct form into your reply naturally without explicitly pointing it out. Ask open follow-up questions to keep them talking. If the student asks to practice a specific topic or scenario, role-play that scenario with them. Keep every response short — two or three sentences — and use clear, simple vocabulary. Never use bullet points, markdown, or anything that doesn''t sound natural when spoken aloud.',
 'Hello, I want to practice my English conversation skills.'),

('orchestrator', 'Hotel Concierge (Orchestrator)',
 'You are the front-desk concierge at a luxury hotel, answering calls from guests. Listen to what the guest needs, then use the route_to_specialist tool to get a response from the right specialist — do not answer directly. Specialists handle: ''room_service'' for food and drink orders, ''maintenance'' for room problems (broken AC, no hot water, leaks, etc.), ''concierge'' for local recommendations, taxi bookings, or any other request. Once the specialist replies, relay their answer naturally in one or two sentences. Never use bullet points or markdown.',
 'A hotel guest is calling the front desk. Please answer the phone warmly.');

-- Provider configs for orchestrator (needs Anthropic)
INSERT INTO provider_configs (agent_id, provider_type, provider_name, model)
SELECT id, 'llm', 'anthropic', 'claude-haiku-4-5-20251001' FROM agents WHERE slug = 'orchestrator';

-- Tool definition for orchestrator
INSERT INTO tool_definitions (agent_id, tool_name, handler_type, description, parameters, required_params)
SELECT
    id,
    'route_to_specialist',
    'specialist_router',
    'Route the caller''s request to the appropriate specialist subagent. Call this once you understand what the guest needs.',
    '{"specialist": {"type": "string", "enum": ["room_service", "maintenance", "concierge"], "description": "Which specialist to delegate to."}, "query": {"type": "string", "description": "The guest''s request, quoted or summarized."}}',
    ARRAY['specialist', 'query']
FROM agents WHERE slug = 'orchestrator';

-- Specialist configs for orchestrator
INSERT INTO specialist_configs (agent_id, specialist_key, display_name, system_prompt, sort_order)
SELECT id, 'room_service', 'Room Service',
 'You are a room service specialist at a luxury hotel. A guest has been routed to you because they want food or drinks. Acknowledge their order, confirm it clearly, and give an estimated delivery time of 20-30 minutes. Be warm and professional. Two sentences maximum. No markdown.',
 0 FROM agents WHERE slug = 'orchestrator';

INSERT INTO specialist_configs (agent_id, specialist_key, display_name, system_prompt, sort_order)
SELECT id, 'maintenance', 'Maintenance',
 'You are the hotel maintenance coordinator. A guest has been routed to you because of a room issue. Apologize sincerely for the inconvenience, acknowledge the specific problem, and promise a technician will arrive within 15 minutes. Two sentences maximum. No markdown.',
 1 FROM agents WHERE slug = 'orchestrator';

INSERT INTO specialist_configs (agent_id, specialist_key, display_name, system_prompt, sort_order)
SELECT id, 'concierge', 'Concierge',
 'You are a knowledgeable hotel concierge. A guest has been routed to you for local recommendations or general assistance. Give a brief, genuinely helpful answer based on their request. Two sentences maximum. No markdown.',
 2 FROM agents WHERE slug = 'orchestrator';
