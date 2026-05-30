-- 010_demo_seed.sql — Global tool library seed data + provider configs for demo agents
-- Safe to re-run (INSERT ... ON CONFLICT DO NOTHING).

-- ── Global tools (shared across all users) ────────────────────────────────────

INSERT INTO tool_definitions (tool_name, handler_type, description, parameters, required_params, is_global, handler_config)
VALUES
(
    'get_current_time',
    'webhook',
    'Returns the current date and time, optionally in a given timezone.',
    '{"timezone": {"type": "string", "description": "IANA timezone name, e.g. America/New_York, Europe/Madrid, UTC. Defaults to UTC."}}',
    '{}',
    TRUE,
    '{"url": "http://tools-server:8100/get_current_time", "timeout": 5}'
),
(
    'get_weather',
    'webhook',
    'Returns current weather conditions for a given location.',
    '{"units": {"enum": ["celsius", "fahrenheit"], "type": "string", "description": "Temperature unit. Defaults to celsius."}, "location": {"type": "string", "description": "City or location name, e.g. Madrid, New York, Tokyo."}}',
    '{location}',
    TRUE,
    '{"url": "http://tools-server:8100/get_weather", "timeout": 5}'
)
ON CONFLICT DO NOTHING;

-- ── Provider configs for basic agent (all local by default) ───────────────────
-- basic, customer_service, storyteller, language_tutor get local STT/LLM/TTS
-- orchestrator already has anthropic/llm from 001_initial.sql

INSERT INTO provider_configs (agent_id, provider_type, provider_name)
SELECT id, 'stt', 'local' FROM agents WHERE slug IN ('basic', 'customer_service', 'storyteller', 'language_tutor')
ON CONFLICT (agent_id, provider_type) DO NOTHING;

INSERT INTO provider_configs (agent_id, provider_type, provider_name)
SELECT id, 'llm', 'local' FROM agents WHERE slug IN ('basic', 'customer_service', 'storyteller', 'language_tutor')
ON CONFLICT (agent_id, provider_type) DO NOTHING;

INSERT INTO provider_configs (agent_id, provider_type, provider_name)
SELECT id, 'tts', 'local' FROM agents WHERE slug IN ('basic', 'customer_service', 'storyteller', 'language_tutor')
ON CONFLICT (agent_id, provider_type) DO NOTHING;

-- orchestrator also gets local STT and TTS (LLM stays on anthropic from 001)
INSERT INTO provider_configs (agent_id, provider_type, provider_name)
SELECT id, 'stt', 'local' FROM agents WHERE slug = 'orchestrator'
ON CONFLICT (agent_id, provider_type) DO NOTHING;

INSERT INTO provider_configs (agent_id, provider_type, provider_name)
SELECT id, 'tts', 'local' FROM agents WHERE slug = 'orchestrator'
ON CONFLICT (agent_id, provider_type) DO NOTHING;
