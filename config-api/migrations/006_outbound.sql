-- 006_outbound.sql — outbound call support
-- Safe to re-run.

ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS direction    TEXT NOT NULL DEFAULT 'inbound';
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS destination  TEXT;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS dial_status  TEXT;
-- dial_status: ANSWER | NO ANSWER | BUSY | FAILED | CONGESTION (set by AMI event)

CREATE INDEX IF NOT EXISTS call_logs_direction_idx ON call_logs (direction);
