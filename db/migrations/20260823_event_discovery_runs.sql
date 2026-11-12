CREATE TABLE IF NOT EXISTS event_discovery_runs (
    id BIGSERIAL PRIMARY KEY,
    anomaly_id BIGINT NOT NULL REFERENCES demand_anomalies(id) ON DELETE CASCADE,
    provider TEXT NOT NULL CHECK (provider IN ('searxng')),
    attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
    query_count INTEGER NOT NULL DEFAULT 0 CHECK (query_count >= 0),
    result_count INTEGER NOT NULL DEFAULT 0 CHECK (result_count >= 0),
    candidate_count INTEGER NOT NULL DEFAULT 0 CHECK (candidate_count >= 0),
    status TEXT NOT NULL CHECK (status IN ('success', 'failed')),
    error_summary TEXT,
    finished_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (anomaly_id, provider, attempt_no)
);

CREATE INDEX IF NOT EXISTS idx_event_discovery_runs_anomaly_finished
ON event_discovery_runs (anomaly_id, finished_at DESC);
