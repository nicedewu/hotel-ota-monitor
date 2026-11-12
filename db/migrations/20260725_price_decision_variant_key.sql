BEGIN;
ALTER TABLE price_decisions ADD COLUMN IF NOT EXISTS core_feature_signature TEXT;
CREATE INDEX IF NOT EXISTS idx_price_decisions_variant
  ON price_decisions (stay_date, COALESCE(own_hotel_key, hotel_key), COALESCE(normalized_room_type, room_category), core_feature_signature, decision_at DESC);
COMMIT;
