BEGIN;

CREATE TABLE IF NOT EXISTS weekly_admin_reviews (
    id BIGSERIAL PRIMARY KEY,
    week_start DATE NOT NULL,
    week_end DATE NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    expected_hotel_count INTEGER NOT NULL,
    actual_hotel_count INTEGER NOT NULL,
    complete_hotel_count INTEGER NOT NULL DEFAULT 0,
    degraded_hotel_count INTEGER NOT NULL DEFAULT 0,
    blocked_hotel_count INTEGER NOT NULL DEFAULT 0,
    generation_status TEXT NOT NULL DEFAULT 'pending',
    push_status TEXT NOT NULL DEFAULT 'pending',
    report_version TEXT NOT NULL DEFAULT 'v1',
    hotel_keys JSONB NOT NULL DEFAULT '[]'::jsonb,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    report_text TEXT NOT NULL DEFAULT '',
    quality_checks JSONB NOT NULL DEFAULT '{}'::jsonb,
    hindsight_mode TEXT NOT NULL DEFAULT 'single_hotel_only',
    generated_at TIMESTAMPTZ,
    pushed_at TIMESTAMPTZ,
    generation_error TEXT,
    push_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (week_start, report_version)
);

CREATE INDEX IF NOT EXISTS idx_weekly_admin_reviews_week_start
ON weekly_admin_reviews (week_start DESC);

CREATE INDEX IF NOT EXISTS idx_weekly_admin_reviews_generation_status
ON weekly_admin_reviews (generation_status);

CREATE INDEX IF NOT EXISTS idx_weekly_admin_reviews_push_status
ON weekly_admin_reviews (push_status);

CREATE INDEX IF NOT EXISTS idx_weekly_reviews_hotel_week_start
ON weekly_reviews (hotel_key, week_start);

COMMIT;
