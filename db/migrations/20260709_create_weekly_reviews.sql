CREATE TABLE IF NOT EXISTS weekly_reviews (
    id BIGSERIAL PRIMARY KEY,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    week_start DATE NOT NULL,
    week_end DATE NOT NULL,
    hotel_key TEXT NOT NULL,
    hotel_name TEXT,
    review_type TEXT NOT NULL DEFAULT 'single_hotel',
    report_markdown TEXT NOT NULL,
    report_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    hindsight_mode TEXT,
    memory_write_status TEXT,
    notify_status TEXT,
    run_id TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_weekly_reviews_hotel_week
ON weekly_reviews (hotel_key, week_start, week_end);

CREATE INDEX IF NOT EXISTS idx_weekly_reviews_generated_at
ON weekly_reviews (generated_at DESC);
