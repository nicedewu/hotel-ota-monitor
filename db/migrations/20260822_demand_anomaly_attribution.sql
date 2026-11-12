CREATE TABLE IF NOT EXISTS demand_anomalies (
    id BIGSERIAL PRIMARY KEY,
    anomaly_key TEXT NOT NULL UNIQUE,
    stay_date DATE NOT NULL,
    affected_hotels JSONB NOT NULL DEFAULT '[]'::jsonb,
    signal_count INTEGER NOT NULL DEFAULT 0,
    total_net_pickup INTEGER NOT NULL DEFAULT 0,
    score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
    status TEXT NOT NULL CHECK (
        status IN (
            'single_hotel_observation',
            'city_observation',
            'preliminary',
            'explained',
            'unexplained'
        )
    ),
    features JSONB NOT NULL DEFAULT '{}'::jsonb,
    evaluated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_demand_anomalies_stay_score
ON demand_anomalies (stay_date, score DESC);

CREATE TABLE IF NOT EXISTS demand_event_attributions (
    id BIGSERIAL PRIMARY KEY,
    anomaly_id BIGINT NOT NULL REFERENCES demand_anomalies(id) ON DELETE CASCADE,
    event_kind TEXT NOT NULL CHECK (event_kind IN ('canonical', 'candidate')),
    event_reference TEXT NOT NULL,
    event_name TEXT NOT NULL,
    venue TEXT,
    category TEXT,
    source_confidence TEXT NOT NULL,
    event_status TEXT NOT NULL,
    attribution_score INTEGER NOT NULL CHECK (attribution_score BETWEEN 0 AND 100),
    rank INTEGER NOT NULL CHECK (rank BETWEEN 1 AND 3),
    reason JSONB NOT NULL DEFAULT '[]'::jsonb,
    evaluated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (anomaly_id, event_reference)
);

CREATE INDEX IF NOT EXISTS idx_demand_event_attributions_anomaly_rank
ON demand_event_attributions (anomaly_id, rank);
