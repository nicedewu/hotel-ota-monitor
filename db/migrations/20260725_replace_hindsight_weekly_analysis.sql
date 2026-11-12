ALTER TABLE weekly_reviews
    ADD COLUMN IF NOT EXISTS analysis_mode TEXT,
    ADD COLUMN IF NOT EXISTS analysis_status TEXT,
    ADD COLUMN IF NOT EXISTS analysis_degraded BOOLEAN,
    ADD COLUMN IF NOT EXISTS analysis_model TEXT,
    ADD COLUMN IF NOT EXISTS analysis_model_version TEXT,
    ADD COLUMN IF NOT EXISTS analysis_schema_version TEXT,
    ADD COLUMN IF NOT EXISTS analysis_duration_ms INTEGER,
    ADD COLUMN IF NOT EXISTS analysis_quality_status TEXT,
    ADD COLUMN IF NOT EXISTS analysis_error_code TEXT,
    ADD COLUMN IF NOT EXISTS analysis_error_message TEXT,
    ADD COLUMN IF NOT EXISTS fact_snapshot_json JSONB,
    ADD COLUMN IF NOT EXISTS fact_quality_passed BOOLEAN;

-- Historical hindsight_* columns and values are intentionally retained.
