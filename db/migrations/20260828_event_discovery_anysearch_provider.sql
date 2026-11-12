ALTER TABLE event_discovery_runs
    DROP CONSTRAINT IF EXISTS event_discovery_runs_provider_check;

ALTER TABLE event_discovery_runs
    ADD CONSTRAINT event_discovery_runs_provider_check
    CHECK (provider IN ('searxng', 'anysearch'));
