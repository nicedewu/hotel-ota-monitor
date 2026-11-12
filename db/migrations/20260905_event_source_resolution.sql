-- Additive migration for Qwen-native source observations and conflict-safe
-- event resolution.  No existing candidate or occurrence is deleted.

ALTER TABLE public.event_discovery_runs
    DROP CONSTRAINT IF EXISTS event_discovery_runs_provider_check;

ALTER TABLE public.event_discovery_runs
    ADD CONSTRAINT event_discovery_runs_provider_check
    CHECK (provider IN ('searxng', 'qwen_native'));

ALTER TABLE public.event_candidates
    ADD COLUMN IF NOT EXISTS event_identity_key TEXT,
    ADD COLUMN IF NOT EXISTS verification_state TEXT NOT NULL DEFAULT 'unverified'
        CHECK (verification_state IN ('unverified', 'verified', 'conflict', 'stale')),
    ADD COLUMN IF NOT EXISTS conflict_details JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS resolved_sources JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ;

-- Historical official imports can contain more than one candidate for the
-- same event identity. Keep lookup fast without making that legacy data a
-- migration blocker; the upsert query chooses the strongest incumbent.
CREATE INDEX IF NOT EXISTS idx_event_candidates_identity_key
    ON public.event_candidates (event_identity_key)
    WHERE event_identity_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_event_candidates_verification_state
    ON public.event_candidates (verification_state, updated_at DESC);
