-- 011_caller_id.sql — Add caller_id to call_logs for the From/To split in the UI
-- Safe to re-run.

ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS caller_id TEXT;
