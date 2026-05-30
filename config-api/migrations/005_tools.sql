-- 005_tools.sql — Global tool library + handler_config support
-- Safe to re-run (all statements use IF NOT EXISTS / IF EXISTS guards).

-- 1. Make agent_id nullable so global tools can exist without an agent owner.
ALTER TABLE tool_definitions ALTER COLUMN agent_id DROP NOT NULL;

-- 2. Add is_global flag (true = tool lives in the global library, agent_id = NULL).
ALTER TABLE tool_definitions ADD COLUMN IF NOT EXISTS is_global BOOLEAN NOT NULL DEFAULT FALSE;

-- 3. Add handler_config for handler-specific settings (e.g. webhook URL, timeout).
ALTER TABLE tool_definitions ADD COLUMN IF NOT EXISTS handler_config JSONB;

-- 4. The existing unique constraint (agent_id, tool_name) breaks with NULL agent_id
--    because NULL != NULL in Postgres. Replace with two partial indexes instead.
DO $$
BEGIN
  -- Drop old constraint if it exists
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'tool_definitions'::regclass
      AND contype = 'u'
      AND conname LIKE '%agent_id%tool_name%'
  ) THEN
    ALTER TABLE tool_definitions DROP CONSTRAINT IF EXISTS tool_definitions_agent_id_tool_name_key;
  END IF;
END $$;

-- Unique tool_name per agent (agent-specific tools)
CREATE UNIQUE INDEX IF NOT EXISTS tool_definitions_agent_tool_name_idx
  ON tool_definitions (agent_id, tool_name)
  WHERE agent_id IS NOT NULL;

-- Unique tool_name across global tools
CREATE UNIQUE INDEX IF NOT EXISTS tool_definitions_global_tool_name_idx
  ON tool_definitions (tool_name)
  WHERE is_global = TRUE;

-- 5. agent_tool_refs — assigns global tools to agents (many-to-many).
CREATE TABLE IF NOT EXISTS agent_tool_refs (
  agent_id   UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  tool_id    UUID NOT NULL REFERENCES tool_definitions(id) ON DELETE CASCADE,
  sort_order INT  NOT NULL DEFAULT 0,
  PRIMARY KEY (agent_id, tool_id)
);
