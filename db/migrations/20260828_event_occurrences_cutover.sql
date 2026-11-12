-- Canonical business activities are no longer maintained from Excel.
-- `event_candidates` stays as an auditable discovery/verification staging area;
-- only verified candidates are written to `event_occurrences` by application code.

DO $$
BEGIN
    IF to_regclass('public.event_occurrences') IS NULL
       AND to_regclass('public.exhibition_events') IS NOT NULL THEN
        ALTER TABLE public.exhibition_events RENAME TO event_occurrences;
    END IF;
END $$;

ALTER TABLE public.event_occurrences
    ADD COLUMN IF NOT EXISTS occurrence_key TEXT,
    ADD COLUMN IF NOT EXISTS city TEXT NOT NULL DEFAULT '上海',
    ADD COLUMN IF NOT EXISTS source_kind TEXT NOT NULL DEFAULT 'legacy_excel',
    ADD COLUMN IF NOT EXISTS source_confidence TEXT NOT NULL DEFAULT 'legacy',
    ADD COLUMN IF NOT EXISTS primary_source_url TEXT,
    ADD COLUMN IF NOT EXISTS last_verified_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS canonical_status TEXT NOT NULL DEFAULT 'legacy',
    ADD COLUMN IF NOT EXISTS is_business_active BOOLEAN NOT NULL DEFAULT FALSE;

-- Keep remaining historical Excel rows for audit only.  The future Excel rows
-- were removed separately at the operator's request and are in recovery tables.
UPDATE public.event_occurrences
SET occurrence_key = 'legacy:' || id::text,
    source_kind = 'legacy_excel',
    source_confidence = 'legacy',
    canonical_status = 'legacy',
    is_business_active = FALSE
WHERE occurrence_key IS NULL;

ALTER TABLE public.event_occurrences
    ALTER COLUMN occurrence_key SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_event_occurrences_occurrence_key
    ON public.event_occurrences (occurrence_key);

CREATE INDEX IF NOT EXISTS idx_event_occurrences_active_dates
    ON public.event_occurrences (is_business_active, event_start_date, event_end_date);

ALTER TABLE public.event_candidates
    ADD COLUMN IF NOT EXISTS canonical_event_id BIGINT
        REFERENCES public.event_occurrences(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_event_candidates_canonical_event
    ON public.event_candidates (canonical_event_id)
    WHERE canonical_event_id IS NOT NULL;

-- Read-only transition compatibility for historical reports and recovery.
-- New application code must read `event_occurrences` directly.
CREATE OR REPLACE VIEW public.exhibition_events AS
SELECT
    id,
    venue,
    event_name,
    event_start_date,
    event_end_date,
    stay_start_date,
    stay_end_date,
    duration_days,
    exhibitor_count_raw,
    area_raw,
    visitor_count_raw,
    category,
    demand_level,
    source_file,
    source_sheet,
    source_row,
    date_fill_method,
    imported_at
FROM public.event_occurrences;
