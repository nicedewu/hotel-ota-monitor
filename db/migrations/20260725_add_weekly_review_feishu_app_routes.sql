-- Copy each active per-hotel business-snapshot Feishu-app route for weekly reviews.
-- Existing webhook routes remain untouched as a rollback path.
INSERT INTO hotel_push_routes (
    own_hotel_key, route_name, channel, webhook_env_key, push_type, is_active,
    delivery_channel, feishu_chat_env_key, feishu_chat_id_fingerprint, hotel_alias
)
SELECT
    source.own_hotel_key,
    CONCAT(COALESCE(source.route_name, source.own_hotel_key), '_weekly_review'),
    source.channel,
    source.webhook_env_key,
    'weekly_review',
    TRUE,
    'feishu_app',
    source.feishu_chat_env_key,
    source.feishu_chat_id_fingerprint,
    source.hotel_alias
FROM hotel_push_routes AS source
WHERE source.push_type = 'business_snapshot'
  AND source.is_active = TRUE
  AND COALESCE(source.delivery_channel, '') = 'feishu_app'
  AND COALESCE(source.feishu_chat_env_key, '') <> ''
  AND NOT EXISTS (
      SELECT 1 FROM hotel_push_routes AS target
      WHERE target.own_hotel_key = source.own_hotel_key
        AND target.push_type = 'weekly_review'
        AND target.is_active = TRUE
  );
