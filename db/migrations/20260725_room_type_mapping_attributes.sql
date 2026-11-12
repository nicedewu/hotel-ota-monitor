BEGIN;
ALTER TABLE room_type_mapping
  ADD COLUMN IF NOT EXISTS attribute_overrides JSONB NOT NULL DEFAULT '{}'::jsonb;
COMMIT;

-- Rollback only after verifying no rule uses attribute_overrides:
-- ALTER TABLE room_type_mapping DROP COLUMN IF EXISTS attribute_overrides;
