CREATE TABLE IF NOT EXISTS agent_run_stages (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL,
    own_hotel_key TEXT,
    stage_name TEXT NOT NULL,
    stage_status TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    message TEXT,
    error_message TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_agent_run_stages_run_id
ON agent_run_stages(run_id);

CREATE INDEX IF NOT EXISTS idx_agent_run_stages_hotel_time
ON agent_run_stages(own_hotel_key, started_at DESC);

CREATE TABLE IF NOT EXISTS hotel_pricing_policies (
    id BIGSERIAL PRIMARY KEY,
    hotel_key TEXT NOT NULL REFERENCES hotels(hotel_key),
    room_category TEXT NOT NULL,
    base_price NUMERIC(10,2),
    min_price NUMERIC(10,2),
    max_price NUMERIC(10,2),
    weekend_markup_min NUMERIC(10,2),
    weekend_markup_max NUMERIC(10,2),
    exhibition_markup_min NUMERIC(10,2),
    exhibition_markup_max NUMERIC(10,2),
    holiday_markup_min NUMERIC(10,2),
    holiday_markup_max NUMERIC(10,2),
    max_daily_increase_pct NUMERIC(6,2) DEFAULT 15,
    max_daily_decrease_pct NUMERIC(6,2) DEFAULT 10,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(hotel_key, room_category)
);

CREATE INDEX IF NOT EXISTS idx_hotel_pricing_policies_hotel_active
ON hotel_pricing_policies(hotel_key, is_active);

CREATE TABLE IF NOT EXISTS price_comparisons (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT,
    own_hotel_key TEXT,
    competitor_hotel_key TEXT,
    stay_date DATE,
    normalized_room_type TEXT,
    own_room_name TEXT,
    own_price NUMERIC(10,2),
    competitor_room_name TEXT,
    competitor_price NUMERIC(10,2),
    price_diff NUMERIC(10,2),
    price_diff_pct NUMERIC(10,4),
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE price_comparisons ADD COLUMN IF NOT EXISTS competitor_min_price NUMERIC(10,2);
ALTER TABLE price_comparisons ADD COLUMN IF NOT EXISTS competitor_median_price NUMERIC(10,2);
ALTER TABLE price_comparisons ADD COLUMN IF NOT EXISTS competitor_max_price NUMERIC(10,2);
ALTER TABLE price_comparisons ADD COLUMN IF NOT EXISTS competitor_available_count INT;
ALTER TABLE price_comparisons ADD COLUMN IF NOT EXISTS competitor_hotel_count INT;
ALTER TABLE price_comparisons ADD COLUMN IF NOT EXISTS competitor_price_spread NUMERIC(10,2);
ALTER TABLE price_comparisons ADD COLUMN IF NOT EXISTS raw JSONB DEFAULT '{}'::jsonb;

CREATE OR REPLACE VIEW latest_own_daily_status AS
SELECT DISTINCT ON (hotel_key, stay_date) *
FROM own_daily_status
ORDER BY hotel_key, stay_date, collected_at DESC, id DESC;

CREATE OR REPLACE VIEW latest_price_decisions AS
SELECT DISTINCT ON (COALESCE(own_hotel_key, hotel_key), stay_date, COALESCE(room_category, normalized_room_type)) *
FROM price_decisions
ORDER BY COALESCE(own_hotel_key, hotel_key), stay_date, COALESCE(room_category, normalized_room_type), decision_at DESC, id DESC;

CREATE OR REPLACE VIEW latest_ota_room_prices AS
SELECT DISTINCT ON (source_hotel_key, stay_date, COALESCE(room_category, normalized_room_type), room_name) *
FROM ota_room_prices
ORDER BY source_hotel_key, stay_date, COALESCE(room_category, normalized_room_type), room_name, collected_at DESC, id DESC;
