CREATE TABLE IF NOT EXISTS event_candidates (
    id BIGSERIAL PRIMARY KEY,
    candidate_key TEXT NOT NULL UNIQUE,
    event_name TEXT NOT NULL,
    aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
    city TEXT NOT NULL,
    venue TEXT,
    event_start_date DATE,
    event_end_date DATE,
    category TEXT NOT NULL DEFAULT 'other',
    event_status TEXT NOT NULL DEFAULT 'discovered'
        CHECK (event_status IN ('discovered', 'probable', 'confirmed', 'promoted', 'cancelled')),
    offline_mode TEXT NOT NULL DEFAULT 'unknown'
        CHECK (offline_mode IN ('offline', 'online', 'hybrid', 'unknown')),
    source_confidence TEXT NOT NULL DEFAULT 'discovery'
        CHECK (source_confidence IN ('official', 'authoritative', 'media', 'discovery')),
    date_confidence INTEGER NOT NULL DEFAULT 0 CHECK (date_confidence BETWEEN 0 AND 100),
    venue_confidence INTEGER NOT NULL DEFAULT 0 CHECK (venue_confidence BETWEEN 0 AND 100),
    primary_source_url TEXT NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_verified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    promoted_at TIMESTAMPTZ,
    raw JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (event_start_date IS NULL AND event_end_date IS NULL)
        OR (event_start_date IS NOT NULL AND event_end_date IS NOT NULL AND event_end_date >= event_start_date)
    )
);

CREATE INDEX IF NOT EXISTS idx_event_candidates_city_dates
ON event_candidates (city, event_start_date, event_end_date);

CREATE INDEX IF NOT EXISTS idx_event_candidates_status_verified
ON event_candidates (event_status, last_verified_at DESC);

CREATE TABLE IF NOT EXISTS event_candidate_sources (
    id BIGSERIAL PRIMARY KEY,
    candidate_id BIGINT NOT NULL REFERENCES event_candidates(id) ON DELETE CASCADE,
    source_key TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_title TEXT,
    source_level TEXT NOT NULL
        CHECK (source_level IN ('official', 'authoritative', 'media', 'discovery')),
    published_at TIMESTAMPTZ,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    raw JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (candidate_id, source_url)
);

CREATE INDEX IF NOT EXISTS idx_event_candidate_sources_key_fetched
ON event_candidate_sources (source_key, fetched_at DESC);
