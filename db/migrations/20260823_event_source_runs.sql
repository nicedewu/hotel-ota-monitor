CREATE TABLE IF NOT EXISTS event_source_runs (
    id BIGSERIAL PRIMARY KEY,
    run_id UUID NOT NULL,
    source_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('success', 'partial', 'failed')),
    fetched_entries INTEGER NOT NULL DEFAULT 0 CHECK (fetched_entries >= 0),
    matched_candidates INTEGER NOT NULL DEFAULT 0 CHECK (matched_candidates >= 0),
    written_candidates INTEGER NOT NULL DEFAULT 0 CHECK (written_candidates >= 0),
    entries_hash TEXT,
    error_summary TEXT,
    finished_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_event_source_runs_source_finished
ON event_source_runs (source_key, finished_at DESC, id DESC);
