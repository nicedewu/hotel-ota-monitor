\set ON_ERROR_STOP on

BEGIN;

CREATE TABLE IF NOT EXISTS hotels (
    hotel_key TEXT PRIMARY KEY,
    hotel_name TEXT NOT NULL,
    hotel_role TEXT NOT NULL CHECK (hotel_role IN ('own', 'competitor')),
    city TEXT,
    district TEXT,
    address TEXT,
    ota_platform TEXT DEFAULT 'ctrip',
    ota_url TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Inactive example hotels for the sample configuration.
INSERT INTO hotels (hotel_key, hotel_name, hotel_role, is_active)
VALUES
    ('own_demo_a', '示例自营酒店 A', 'own', FALSE),
    ('competitor_demo_a', '示例竞品酒店 A', 'competitor', FALSE)
ON CONFLICT (hotel_key) DO NOTHING;

CREATE TABLE IF NOT EXISTS hotel_competitors (
    id BIGSERIAL PRIMARY KEY,
    own_hotel_key TEXT NOT NULL REFERENCES hotels(hotel_key),
    competitor_hotel_key TEXT NOT NULL REFERENCES hotels(hotel_key),
    priority INT DEFAULT 100,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (own_hotel_key, competitor_hotel_key)
);

CREATE TABLE IF NOT EXISTS hotel_push_routes (
    id BIGSERIAL PRIMARY KEY,
    own_hotel_key TEXT NOT NULL REFERENCES hotels(hotel_key),
    route_name TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'feishu',
    webhook_env_key TEXT NOT NULL,
    push_type TEXT NOT NULL DEFAULT 'business_snapshot',
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_hotel_push_routes_route
ON hotel_push_routes (own_hotel_key, route_name, push_type);

CREATE TABLE IF NOT EXISTS hotel_pms_accounts (
    id BIGSERIAL PRIMARY KEY,
    own_hotel_key TEXT NOT NULL REFERENCES hotels(hotel_key),
    pms_vendor TEXT NOT NULL DEFAULT 'xz',
    pms_hotel_id TEXT,
    pms_account_key TEXT,
    secret_env_key TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_hotel_pms_accounts_account
ON hotel_pms_accounts (own_hotel_key, pms_vendor, pms_account_key);

CREATE TABLE IF NOT EXISTS room_type_mapping (
    id BIGSERIAL PRIMARY KEY,
    hotel_key TEXT REFERENCES hotels(hotel_key),
    match_keyword TEXT NOT NULL,
    normalized_room_type TEXT NOT NULL,
    priority INT DEFAULT 100,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_room_type_mapping_rule
ON room_type_mapping (COALESCE(hotel_key, ''), match_keyword, normalized_room_type);

CREATE TABLE IF NOT EXISTS price_comparisons (
    id BIGSERIAL,
    run_id TEXT NOT NULL,
    own_hotel_key TEXT NOT NULL REFERENCES hotels(hotel_key),
    competitor_hotel_key TEXT NOT NULL REFERENCES hotels(hotel_key),
    stay_date DATE NOT NULL,
    normalized_room_type TEXT NOT NULL,
    own_room_name TEXT,
    own_price NUMERIC(10,2),
    competitor_room_name TEXT,
    competitor_price NUMERIC(10,2),
    price_diff NUMERIC(10,2),
    price_diff_pct NUMERIC(8,4),
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id, collected_at)
);

CREATE TABLE IF NOT EXISTS hotel_event_impact (
    id BIGSERIAL PRIMARY KEY,
    own_hotel_key TEXT NOT NULL REFERENCES hotels(hotel_key),
    event_id BIGINT NOT NULL REFERENCES exhibition_events(id),
    impact_level TEXT,
    price_premium_min NUMERIC(10,2),
    price_premium_max NUMERIC(10,2),
    note TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (own_hotel_key, event_id)
);

ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS run_id TEXT;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS own_hotel_key TEXT;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS error_message TEXT;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_agent_runs_run_id ON agent_runs (run_id, started_at DESC);

CREATE OR REPLACE VIEW job_runs AS
SELECT
    run_id,
    run_type AS job_type,
    own_hotel_key,
    status,
    started_at,
    finished_at,
    COALESCE(error_message, message) AS error_message,
    COALESCE(metadata, raw, '{}'::jsonb) AS metadata
FROM agent_runs;

CREATE OR REPLACE VIEW events AS
SELECT
    id,
    event_name,
    venue AS venue_name,
    NULL::TEXT AS city,
    COALESCE(stay_start_date, event_start_date) AS start_date,
    COALESCE(stay_end_date, event_end_date) AS end_date,
    category AS event_type,
    demand_level,
    jsonb_build_object(
        'event_start_date', event_start_date,
        'event_end_date', event_end_date,
        'source_file', source_file,
        'source_sheet', source_sheet,
        'source_row', source_row
    ) AS raw_payload,
    imported_at AS created_at
FROM exhibition_events;

INSERT INTO hotel_competitors (own_hotel_key, competitor_hotel_key, priority, is_active)
VALUES ('own_demo_a', 'competitor_demo_a', 10, FALSE)
ON CONFLICT (own_hotel_key, competitor_hotel_key) DO NOTHING;

INSERT INTO room_type_mapping
    (hotel_key, match_keyword, normalized_room_type, priority, is_active)
VALUES
    (NULL, '单人', 'esports_single', 100, TRUE),
    (NULL, '大床', 'esports_double', 100, TRUE),
    (NULL, '情侣', 'esports_double', 110, TRUE),
    (NULL, '双人', 'esports_double', 120, TRUE),
    (NULL, '双床', 'esports_twin', 100, TRUE),
    (NULL, '三人', 'esports_triple', 100, TRUE),
    (NULL, '四人', 'esports_quad', 100, TRUE),
    (NULL, '五人', 'esports_penta', 100, TRUE),
    (NULL, '六人', 'esports_hexa', 100, TRUE),
    (NULL, '浴缸', 'bathtub_double', 100, TRUE),
    (NULL, '特大床', 'king_double', 100, TRUE)
ON CONFLICT DO NOTHING;

ALTER TABLE ota_room_prices ADD COLUMN IF NOT EXISTS run_id TEXT;
ALTER TABLE ota_room_prices ADD COLUMN IF NOT EXISTS source_hotel_key TEXT;
ALTER TABLE ota_room_prices ADD COLUMN IF NOT EXISTS normalized_room_type TEXT;
ALTER TABLE ota_room_prices ADD COLUMN IF NOT EXISTS raw_payload JSONB;
UPDATE ota_room_prices
SET source_hotel_key = COALESCE(source_hotel_key, hotel_key, 'own_demo_a'),
    normalized_room_type = COALESCE(normalized_room_type, room_category),
    raw_payload = COALESCE(raw_payload, raw)
WHERE source_hotel_key IS NULL OR normalized_room_type IS NULL OR raw_payload IS NULL;
ALTER TABLE ota_room_prices ALTER COLUMN source_hotel_key SET NOT NULL;

ALTER TABLE price_decisions ADD COLUMN IF NOT EXISTS own_hotel_key TEXT;
ALTER TABLE price_decisions ADD COLUMN IF NOT EXISTS normalized_room_type TEXT;
ALTER TABLE price_decisions ADD COLUMN IF NOT EXISTS current_price NUMERIC(10,2);
ALTER TABLE price_decisions ADD COLUMN IF NOT EXISTS suggested_price NUMERIC(10,2);
ALTER TABLE price_decisions ADD COLUMN IF NOT EXISTS decision_action TEXT;
ALTER TABLE price_decisions ADD COLUMN IF NOT EXISTS confidence TEXT;
ALTER TABLE price_decisions ADD COLUMN IF NOT EXISTS input_snapshot JSONB;
UPDATE price_decisions
SET own_hotel_key = COALESCE(own_hotel_key, hotel_key, 'own_demo_a'),
    normalized_room_type = COALESCE(normalized_room_type, room_category),
    current_price = COALESCE(current_price, own_current_price),
    suggested_price = COALESCE(suggested_price, recommended_price),
    decision_action = COALESCE(decision_action, recommended_action),
    input_snapshot = COALESCE(input_snapshot, raw)
WHERE own_hotel_key IS NULL OR normalized_room_type IS NULL OR input_snapshot IS NULL;
ALTER TABLE price_decisions ALTER COLUMN own_hotel_key SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_own_daily_status_hotel_key') THEN
        ALTER TABLE own_daily_status
        ADD CONSTRAINT fk_own_daily_status_hotel_key FOREIGN KEY (hotel_key) REFERENCES hotels(hotel_key);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_ota_room_prices_source_hotel_key') THEN
        ALTER TABLE ota_room_prices
        ADD CONSTRAINT fk_ota_room_prices_source_hotel_key FOREIGN KEY (source_hotel_key) REFERENCES hotels(hotel_key);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_price_decisions_own_hotel_key') THEN
        ALTER TABLE price_decisions
        ADD CONSTRAINT fk_price_decisions_own_hotel_key FOREIGN KEY (own_hotel_key) REFERENCES hotels(hotel_key);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_hotels_role_active ON hotels (hotel_role, is_active);
CREATE INDEX IF NOT EXISTS idx_hotel_competitors_own_active ON hotel_competitors (own_hotel_key, is_active, priority);
CREATE INDEX IF NOT EXISTS idx_hotel_push_routes_own_type_active ON hotel_push_routes (own_hotel_key, push_type, is_active);
CREATE INDEX IF NOT EXISTS idx_hotel_pms_accounts_own_active ON hotel_pms_accounts (own_hotel_key, is_active);
CREATE INDEX IF NOT EXISTS idx_room_type_mapping_hotel_keyword ON room_type_mapping (hotel_key, match_keyword, priority);
CREATE INDEX IF NOT EXISTS idx_own_daily_status_hotel_date ON own_daily_status (hotel_key, stay_date, collected_at DESC);
CREATE INDEX IF NOT EXISTS idx_ota_room_prices_source_date ON ota_room_prices (source_hotel_key, stay_date, collected_at DESC);
CREATE INDEX IF NOT EXISTS idx_ota_room_prices_type_price ON ota_room_prices (source_hotel_key, stay_date, normalized_room_type, price);
CREATE INDEX IF NOT EXISTS idx_price_comparisons_own_date ON price_comparisons (own_hotel_key, stay_date, collected_at DESC);
CREATE INDEX IF NOT EXISTS idx_price_comparisons_competitor ON price_comparisons (competitor_hotel_key, stay_date);
CREATE INDEX IF NOT EXISTS idx_price_decisions_own_date ON price_decisions (own_hotel_key, stay_date, decision_at DESC);
CREATE INDEX IF NOT EXISTS idx_hotel_event_impact_hotel ON hotel_event_impact (own_hotel_key, impact_level);
CREATE INDEX IF NOT EXISTS idx_exhibition_events_dates ON exhibition_events (event_start_date, event_end_date);

SELECT create_hypertable('price_comparisons', 'collected_at', if_not_exists => TRUE, migrate_data => TRUE);

COMMIT;
