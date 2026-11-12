CREATE TABLE IF NOT EXISTS weekly_review_candidate_drafts (
    id UUID PRIMARY KEY,
    weekly_review_id BIGINT NOT NULL REFERENCES weekly_reviews(id) ON DELETE RESTRICT,
    run_id TEXT NOT NULL,
    hotel_key TEXT NOT NULL REFERENCES hotels(hotel_key) ON DELETE RESTRICT,
    week_start DATE NOT NULL,
    week_end DATE NOT NULL,
    rule_type TEXT NOT NULL,
    experience_summary TEXT NOT NULL CHECK (length(btrim(experience_summary)) > 0),
    applicable_conditions TEXT NOT NULL,
    applicability TEXT NOT NULL,
    evidence_refs JSONB NOT NULL,
    confidence TEXT NOT NULL CHECK (confidence IN ('low','medium','high')),
    content_hash TEXT NOT NULL,
    evidence_status TEXT NOT NULL DEFAULT 'pending' CHECK (evidence_status IN ('pending','valid','invalid')),
    evidence_details JSONB NOT NULL DEFAULT '{}'::jsonb,
    conflict_status TEXT NOT NULL DEFAULT 'pending' CHECK (conflict_status IN ('pending','clear','conflict')),
    conflict_details JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'pending_validation' CHECK (status IN ('pending_validation','needs_more_evidence','pending_review','approved','rejected','sync_pending','synced','sync_failed')),
    reviewed_by TEXT,
    reviewed_at TIMESTAMPTZ,
    review_comment TEXT,
    mnemosyne_sync_status TEXT NOT NULL DEFAULT 'not_ready' CHECK (mnemosyne_sync_status IN ('not_ready','pending','synced','failed')),
    mnemosyne_memory_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (weekly_review_id, content_hash)
);

CREATE TABLE IF NOT EXISTS weekly_review_candidate_reviews (
    id UUID PRIMARY KEY,
    candidate_id UUID NOT NULL REFERENCES weekly_review_candidate_drafts(id) ON DELETE RESTRICT,
    action TEXT NOT NULL CHECK (action IN ('approve','reject','request_more_evidence','sync_success','sync_failed')),
    comment TEXT NOT NULL CHECK (length(btrim(comment)) > 0),
    actor_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS weekly_review_candidate_drafts_review_idx
    ON weekly_review_candidate_drafts (hotel_key, status, week_start DESC);
