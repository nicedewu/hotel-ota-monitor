-- Materialize event impact for venues configured in hotel_venue_impact_rules.
INSERT INTO hotel_event_impact (own_hotel_key, event_id, impact_level, note)
SELECT rule.own_hotel_key, event.id, rule.impact_level, 'configured venue rule'
FROM hotel_venue_impact_rules AS rule
JOIN exhibition_events AS event ON event.venue = rule.venue
ON CONFLICT (own_hotel_key, event_id) DO NOTHING;
