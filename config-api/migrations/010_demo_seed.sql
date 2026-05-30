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

-- ── Demo call log samples (illustrate inbound + outbound From/To columns) ─────
-- These show up in the Calls UI on a fresh install so the table isn't empty.
-- caller_id = who called (inbound) or who we said we were (outbound)
-- did       = the DID that was dialed (inbound)
-- destination = who we called (outbound)

INSERT INTO call_logs (
    call_uuid, agent_slug, direction,
    caller_id, did, destination,
    started_at, ended_at, duration_seconds, turn_count,
    stt_provider, llm_provider, tts_provider, end_reason,
    transcript
) VALUES
(
    'demo-inbound-001', 'basic', 'inbound',
    '+15550001111', '_X.', NULL,
    NOW() - INTERVAL '2 hours', NOW() - INTERVAL '2 hours' + INTERVAL '47 seconds', 47, 3,
    'local', 'local', 'local', 'hangup',
    '[{"role":"assistant","content":"Hello, how can I help you today?"},{"role":"user","content":"What time is it?"},{"role":"assistant","content":"I don''t have access to a clock, but I can help with many other questions!"}]'
),
(
    'demo-outbound-001', 'basic', 'outbound',
    'Voice Agent <+10000000000>', NULL, '+15559876543',
    NOW() - INTERVAL '1 hour', NOW() - INTERVAL '1 hour' + INTERVAL '92 seconds', 92, 5,
    'local', 'local', 'local', 'hangup',
    '[{"role":"assistant","content":"Hello, this is an automated call. How are you today?"},{"role":"user","content":"I''m good, thanks."}]'
),
(
    'demo-inbound-002', 'customer_service', 'inbound',
    '+15553334444', '1001', NULL,
    NOW() - INTERVAL '30 minutes', NOW() - INTERVAL '30 minutes' + INTERVAL '124 seconds', 124, 7,
    'local', 'local', 'local', 'hangup',
    '[{"role":"assistant","content":"Thank you for calling support. My name is Alex, how can I help you?"},{"role":"user","content":"My device isn''t turning on."}]'
),
(
    'demo-outbound-002', 'orchestrator', 'outbound',
    'Hotel Concierge <+10000000001>', NULL, 'softphone',
    NOW() - INTERVAL '10 minutes', NOW() - INTERVAL '10 minutes' + INTERVAL '68 seconds', 68, 4,
    'local', 'anthropic', 'local', 'hangup',
    '[{"role":"assistant","content":"Good evening! Thank you for calling the front desk. How may I assist you?"},{"role":"user","content":"Can I order room service?"}]'
)
ON CONFLICT (call_uuid) DO NOTHING;
