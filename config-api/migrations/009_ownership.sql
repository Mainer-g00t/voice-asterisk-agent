-- Per-user resource isolation.
-- Adds owner_id to the three top-level resource tables.
-- call_logs is filtered transitively through agent ownership (no column needed).

ALTER TABLE agents       ADD COLUMN IF NOT EXISTS owner_id UUID REFERENCES users(id);
ALTER TABLE flows        ADD COLUMN IF NOT EXISTS owner_id UUID REFERENCES users(id);
ALTER TABLE phone_routes ADD COLUMN IF NOT EXISTS owner_id UUID REFERENCES users(id);

-- Assign all existing rows to the first (earliest) user so nothing is orphaned.
DO $$
DECLARE first_user_id UUID;
BEGIN
    SELECT id INTO first_user_id FROM users ORDER BY created_at LIMIT 1;
    IF first_user_id IS NOT NULL THEN
        UPDATE agents       SET owner_id = first_user_id WHERE owner_id IS NULL;
        UPDATE flows        SET owner_id = first_user_id WHERE owner_id IS NULL;
        UPDATE phone_routes SET owner_id = first_user_id WHERE owner_id IS NULL;
    END IF;
END $$;
