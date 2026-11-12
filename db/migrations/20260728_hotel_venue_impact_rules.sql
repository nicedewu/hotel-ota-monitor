CREATE TABLE IF NOT EXISTS hotel_venue_impact_rules (
    own_hotel_key TEXT NOT NULL REFERENCES hotels(hotel_key),
    venue TEXT NOT NULL,
    impact_level TEXT NOT NULL CHECK (impact_level IN ('high', 'medium', 'low')),
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (own_hotel_key, venue)
);
