BEGIN;

CREATE OR REPLACE FUNCTION public.reserve_branch_push(
    p_hotel_key TEXT,
    p_push_type TEXT,
    p_webhook_env_key TEXT,
    p_source_record_type TEXT,
    p_source_record_id TEXT,
    p_push_context JSONB
)
RETURNS TABLE (
    push_id UUID,
    push_ref TEXT,
    should_send BOOLEAN,
    delivery_token UUID,
    push_context JSONB
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public', 'pg_temp'
AS $function$
DECLARE
    rec public.branch_push_records%ROWTYPE;
    generated_ref TEXT;
    try_count INTEGER;
    rendered_context JSONB;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.hotels
         WHERE hotel_key = p_hotel_key AND hotel_role = 'own' AND is_active IS TRUE
    ) THEN
        RAISE EXCEPTION 'invalid active own hotel_key';
    END IF;
    IF p_push_type NOT IN ('business_snapshot', 'price_recommendation', 'price_comparison', 'inventory_alert', 'operation_notice')
       OR p_webhook_env_key IS NULL OR btrim(p_webhook_env_key) = ''
       OR p_source_record_type IS NULL OR btrim(p_source_record_type) = ''
       OR p_source_record_id IS NULL OR btrim(p_source_record_id) = ''
       OR p_push_context IS NULL OR jsonb_typeof(p_push_context) <> 'object'
       OR COALESCE(p_push_context->>'message', '') = '' THEN
        RAISE EXCEPTION 'invalid branch push payload';
    END IF;
    IF p_push_type = 'business_snapshot' AND NOT EXISTS (
        SELECT 1 FROM public.hotel_push_routes
         WHERE own_hotel_key = p_hotel_key
           AND webhook_env_key = p_webhook_env_key
           AND channel = 'feishu'
           AND push_type = 'business_snapshot'
           AND is_active IS TRUE
    ) THEN
        RAISE EXCEPTION 'inactive business snapshot push route';
    END IF;

    FOR try_count IN 1..8 LOOP
        generated_ref := 'P-' || to_char(now() AT TIME ZONE 'Asia/Shanghai', 'YYYYMMDD-HH24MI')
            || '-' || upper(substr(replace(gen_random_uuid()::text, '-', ''), 1, 8));
        rendered_context := jsonb_set(
            p_push_context,
            '{message}',
            to_jsonb((p_push_context->>'message') || E'\n\n────────────────\n推送编号：' || generated_ref || E'\n反馈方式：在本话题中 @小浪分身'),
            TRUE
        );
        INSERT INTO public.branch_push_records (
            push_ref, hotel_key, push_type, webhook_env_key, source_record_type,
            source_record_id, payload_digest, push_summary, push_context
        ) VALUES (
            generated_ref, p_hotel_key, p_push_type, p_webhook_env_key,
            p_source_record_type, p_source_record_id,
            md5(rendered_context::text),
            left(rendered_context->>'message', 500), rendered_context
        )
        ON CONFLICT DO NOTHING
        RETURNING * INTO rec;
        IF FOUND THEN
            EXIT;
        END IF;
        SELECT * INTO rec
          FROM public.branch_push_records
         WHERE hotel_key = p_hotel_key
           AND push_type = p_push_type
           AND webhook_env_key = p_webhook_env_key
           AND source_record_type = p_source_record_type
           AND source_record_id = p_source_record_id
         FOR UPDATE;
        IF FOUND THEN
            EXIT;
        END IF;
    END LOOP;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'could not reserve branch push';
    END IF;
    IF rec.delivery_status = 'sent' THEN
        RETURN QUERY SELECT rec.push_id, rec.push_ref, FALSE, NULL::UUID, rec.push_context;
        RETURN;
    END IF;
    IF rec.delivery_status = 'sending' AND rec.lease_until > now() THEN
        RETURN QUERY SELECT rec.push_id, rec.push_ref, FALSE, NULL::UUID, rec.push_context;
        RETURN;
    END IF;
    UPDATE public.branch_push_records
       SET delivery_status = 'sending',
           delivery_attempts = delivery_attempts + 1,
           delivery_token = gen_random_uuid(),
           lease_until = now() + interval '60 seconds',
           last_error_code = NULL,
           last_error_summary = NULL,
           updated_at = now()
     WHERE id = rec.id
     RETURNING * INTO rec;
    RETURN QUERY SELECT rec.push_id, rec.push_ref, TRUE, rec.delivery_token, rec.push_context;
END;
$function$;

COMMIT;
