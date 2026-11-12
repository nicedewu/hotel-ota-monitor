-- Extend the existing branch outbox contract to permit the new weekly-review route.
CREATE OR REPLACE FUNCTION public.reserve_branch_push(
    p_hotel_key text, p_push_type text, p_webhook_env_key text,
    p_source_record_type text, p_source_record_id text, p_push_context jsonb
)
RETURNS TABLE(push_id uuid, push_ref text, should_send boolean, delivery_token uuid,
              push_context jsonb, delivery_status text, delivery_channel text,
              feishu_chat_env_key text)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path TO 'public', 'pg_temp'
AS $function$
DECLARE
    rec public.branch_push_records%ROWTYPE;
    route public.hotel_push_routes%ROWTYPE;
    generated_ref TEXT;
    try_count INTEGER;
    route_count INTEGER;
    effective_delivery_channel TEXT;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM public.hotels WHERE hotel_key = p_hotel_key AND hotel_role = 'own' AND is_active IS TRUE) THEN
        RAISE EXCEPTION 'invalid active own hotel_key';
    END IF;
    IF p_push_type NOT IN ('business_snapshot', 'price_recommendation', 'price_comparison', 'inventory_alert', 'operation_notice', 'weekly_review')
       OR p_webhook_env_key IS NULL OR btrim(p_webhook_env_key) = ''
       OR p_source_record_type IS NULL OR btrim(p_source_record_type) = ''
       OR p_source_record_id IS NULL OR btrim(p_source_record_id) = ''
       OR p_push_context IS NULL OR jsonb_typeof(p_push_context) <> 'object'
       OR COALESCE(p_push_context->>'message', '') = '' THEN
        RAISE EXCEPTION 'invalid branch push payload';
    END IF;

    SELECT count(*) INTO route_count FROM public.hotel_push_routes r
     WHERE r.own_hotel_key = p_hotel_key AND r.push_type = p_push_type
       AND r.webhook_env_key = p_webhook_env_key AND r.channel = 'feishu' AND r.is_active IS TRUE;
    IF route_count <> 1 THEN RAISE EXCEPTION 'inactive or ambiguous branch push route'; END IF;
    SELECT * INTO route FROM public.hotel_push_routes r
     WHERE r.own_hotel_key = p_hotel_key AND r.push_type = p_push_type
       AND r.webhook_env_key = p_webhook_env_key AND r.channel = 'feishu' AND r.is_active IS TRUE;
    IF route.delivery_channel = 'feishu_app' AND (route.feishu_chat_env_key IS NULL OR route.feishu_chat_id_fingerprint IS NULL) THEN
        RAISE EXCEPTION 'incomplete feishu app route';
    END IF;

    effective_delivery_channel := CASE WHEN p_source_record_type = 'delivery_migration_test' THEN 'feishu_app' ELSE route.delivery_channel END;
    IF effective_delivery_channel = 'feishu_app' AND (route.feishu_chat_env_key IS NULL OR route.feishu_chat_id_fingerprint IS NULL) THEN
        RAISE EXCEPTION 'incomplete feishu app route';
    END IF;

    FOR try_count IN 1..8 LOOP
        generated_ref := 'P-' || to_char(now() AT TIME ZONE 'Asia/Shanghai', 'YYYYMMDD-HH24MI') || '-' || upper(substr(replace(gen_random_uuid()::text, '-', ''), 1, 8));
        INSERT INTO public.branch_push_records (
            push_ref, route_id, hotel_key, push_type, webhook_env_key,
            source_record_type, source_record_id, payload_digest, push_summary,
            push_context, delivery_channel, feishu_chat_env_key
        ) VALUES (
            generated_ref, route.id, p_hotel_key, p_push_type, route.webhook_env_key,
            p_source_record_type, p_source_record_id, md5(p_push_context::text),
            left(p_push_context->>'message', 500), p_push_context, effective_delivery_channel,
            CASE WHEN effective_delivery_channel = 'feishu_app' THEN route.feishu_chat_env_key ELSE NULL END
        ) ON CONFLICT DO NOTHING RETURNING * INTO rec;
        IF FOUND THEN EXIT; END IF;
        SELECT * INTO rec FROM public.branch_push_records
         WHERE route_id = route.id AND source_record_type = p_source_record_type AND source_record_id = p_source_record_id
         FOR UPDATE;
        IF FOUND THEN EXIT; END IF;
    END LOOP;
    IF NOT FOUND THEN RAISE EXCEPTION 'could not reserve branch push'; END IF;
    IF rec.delivery_status = 'sent' OR rec.delivery_status = 'uncertain' THEN
        RETURN QUERY SELECT rec.push_id, rec.push_ref, FALSE, NULL::UUID, rec.push_context, rec.delivery_status, rec.delivery_channel, rec.feishu_chat_env_key;
        RETURN;
    END IF;
    IF rec.delivery_status = 'sending' AND rec.lease_until > now() THEN
        RETURN QUERY SELECT rec.push_id, rec.push_ref, FALSE, NULL::UUID, rec.push_context, rec.delivery_status, rec.delivery_channel, rec.feishu_chat_env_key;
        RETURN;
    END IF;
    UPDATE public.branch_push_records SET delivery_status = 'sending', delivery_attempts = delivery_attempts + 1,
        delivery_token = gen_random_uuid(), send_attempt_id = gen_random_uuid(), lease_until = now() + interval '60 seconds',
        last_error_code = NULL, last_error_summary = NULL, updated_at = now()
     WHERE id = rec.id RETURNING * INTO rec;
    RETURN QUERY SELECT rec.push_id, rec.push_ref, TRUE, rec.delivery_token, rec.push_context, rec.delivery_status, rec.delivery_channel, rec.feishu_chat_env_key;
END;
$function$;
