-- Additive support for a separate non-official business reference layer.
-- Does not modify event_occurrences or existing candidate/source rows.
-- Individual evidence rows keep their own discovery/media/official level;
-- only the aggregate candidate can become corroborated.

ALTER TABLE public.event_candidates
    DROP CONSTRAINT IF EXISTS event_candidates_verification_state_check;

ALTER TABLE public.event_candidates
    ADD CONSTRAINT event_candidates_verification_state_check
    CHECK (verification_state IN ('unverified', 'corroborated', 'verified', 'conflict', 'stale'));

ALTER TABLE public.event_candidates
    DROP CONSTRAINT IF EXISTS event_candidates_source_confidence_check;

ALTER TABLE public.event_candidates
    ADD CONSTRAINT event_candidates_source_confidence_check
    CHECK (source_confidence IN ('official', 'authoritative', 'corroborated', 'media', 'discovery'));

ALTER TABLE public.event_candidates
    ADD COLUMN IF NOT EXISTS independent_source_count INTEGER NOT NULL DEFAULT 0
        CHECK (independent_source_count >= 0),
    ADD COLUMN IF NOT EXISTS corroborated_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_event_candidates_corroborated
    ON public.event_candidates (verification_state, independent_source_count DESC, event_start_date)
    WHERE verification_state = 'corroborated';
