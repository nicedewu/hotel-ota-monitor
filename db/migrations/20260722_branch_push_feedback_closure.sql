BEGIN;

CREATE TABLE IF NOT EXISTS public.branch_push_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    push_id UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    push_ref TEXT NOT NULL UNIQUE,
    hotel_key TEXT NOT NULL REFERENCES public.hotels(hotel_key) ON DELETE RESTRICT,
    push_type TEXT NOT NULL CHECK (push_type IN (
        'business_snapshot', 'price_recommendation', 'price_comparison',
        'inventory_alert', 'operation_notice'
    )),
    webhook_env_key TEXT NOT NULL,
    source_record_type TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    payload_version TEXT NOT NULL DEFAULT 'v1',
    payload_digest TEXT NOT NULL,
    push_summary TEXT NOT NULL,
    push_context JSONB NOT NULL,
    delivery_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (delivery_status IN ('pending', 'sending', 'sent', 'failed')),
    delivery_attempts INTEGER NOT NULL DEFAULT 0 CHECK (delivery_attempts >= 0),
    delivery_token UUID,
    lease_until TIMESTAMPTZ,
    last_error_code TEXT,
    last_error_summary TEXT,
    sent_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (hotel_key, push_type, webhook_env_key, source_record_type, source_record_id)
);

CREATE INDEX IF NOT EXISTS branch_push_records_hotel_status_idx
    ON public.branch_push_records (hotel_key, delivery_status, created_at DESC);
CREATE INDEX IF NOT EXISTS branch_push_records_retry_idx
    ON public.branch_push_records (delivery_status, lease_until)
    WHERE delivery_status IN ('pending', 'sending', 'failed');

ALTER TABLE public.branch_feedbacks
    ADD COLUMN IF NOT EXISTS push_id UUID REFERENCES public.branch_push_records(push_id) ON DELETE RESTRICT,
    ADD COLUMN IF NOT EXISTS candidate_id UUID REFERENCES public.experience_candidates(id) ON DELETE RESTRICT,
    ADD COLUMN IF NOT EXISTS execution_status TEXT,
    ADD COLUMN IF NOT EXISTS action_taken TEXT,
    ADD COLUMN IF NOT EXISTS executed_price NUMERIC(12, 2),
    ADD COLUMN IF NOT EXISTS executed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS result_observed_at TIMESTAMPTZ;

ALTER TABLE public.branch_feedbacks
    DROP CONSTRAINT IF EXISTS branch_feedbacks_feedback_type_check;
ALTER TABLE public.branch_feedbacks
    ADD CONSTRAINT branch_feedbacks_feedback_type_check CHECK (feedback_type = ANY (ARRAY[
        'operating_observation', 'price_action', 'execution_result', 'exception_reason',
        'event_impact', 'room_sale', 'guest_mix', 'other', 'general_feedback',
        'price_execution_result', 'candidate_evidence', 'exception_explanation'
    ]));
ALTER TABLE public.branch_feedbacks
    ADD CONSTRAINT branch_feedbacks_execution_status_check CHECK (
        execution_status IS NULL OR execution_status IN (
            'executed', 'partially_executed', 'not_executed', 'reverted'
        )
    );

CREATE INDEX IF NOT EXISTS branch_feedbacks_push_created_idx
    ON public.branch_feedbacks (push_id, created_at DESC)
    WHERE push_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS branch_feedbacks_candidate_created_idx
    ON public.branch_feedbacks (candidate_id, created_at DESC)
    WHERE candidate_id IS NOT NULL;

CREATE OR REPLACE FUNCTION public.branch_candidate_ref(p_candidate_id UUID)
RETURNS TEXT
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT 'E-' || upper(substr(replace(p_candidate_id::text, '-', ''), 1, 12))
$$;

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

CREATE OR REPLACE FUNCTION public.mark_branch_push_delivery(
    p_push_id UUID,
    p_delivery_token UUID,
    p_status TEXT,
    p_error_code TEXT DEFAULT NULL,
    p_error_summary TEXT DEFAULT NULL
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public', 'pg_temp'
AS $function$
BEGIN
    IF p_status NOT IN ('sent', 'failed') THEN
        RAISE EXCEPTION 'invalid delivery status';
    END IF;
    UPDATE public.branch_push_records
       SET delivery_status = p_status,
           sent_at = CASE WHEN p_status = 'sent' THEN now() ELSE sent_at END,
           lease_until = NULL,
           last_error_code = CASE WHEN p_status = 'failed' THEN left(COALESCE(p_error_code, 'delivery_error'), 128) ELSE NULL END,
           last_error_summary = CASE WHEN p_status = 'failed' THEN left(COALESCE(p_error_summary, ''), 500) ELSE NULL END,
           updated_at = now()
     WHERE push_id = p_push_id
       AND delivery_token = p_delivery_token
       AND delivery_status = 'sending';
    RETURN FOUND;
END;
$function$;

CREATE OR REPLACE FUNCTION public.resolve_branch_push_by_ref(
    p_hotel_key TEXT,
    p_push_ref TEXT
)
RETURNS TABLE (
    push_id UUID,
    push_ref TEXT,
    push_type TEXT,
    push_summary TEXT,
    push_context JSONB,
    sent_at TIMESTAMPTZ
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO 'public', 'pg_temp'
AS $$
    SELECT r.push_id, r.push_ref, r.push_type, r.push_summary, r.push_context, r.sent_at
      FROM public.branch_push_records r
     WHERE r.hotel_key = p_hotel_key
       AND r.push_ref = upper(btrim(p_push_ref))
       AND r.delivery_status = 'sent'
$$;

CREATE OR REPLACE FUNCTION public.submit_branch_feedback_linked(
    p_hotel_key TEXT,
    p_source_chat_id TEXT,
    p_source_open_id TEXT,
    p_source_message_id TEXT,
    p_feedback_type TEXT,
    p_feedback_text TEXT,
    p_related_stay_date DATE DEFAULT NULL,
    p_related_room_category TEXT DEFAULT NULL,
    p_push_ref TEXT DEFAULT NULL,
    p_execution_status TEXT DEFAULT NULL,
    p_action_taken TEXT DEFAULT NULL,
    p_executed_price NUMERIC DEFAULT NULL,
    p_executed_at TIMESTAMPTZ DEFAULT NULL,
    p_result_observed_at TIMESTAMPTZ DEFAULT NULL,
    p_structured_payload JSONB DEFAULT '{}'::jsonb
)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public', 'pg_temp'
AS $function$
DECLARE
    feedback_uuid UUID;
    linked_push_id UUID;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.hotels
         WHERE hotel_key = p_hotel_key AND hotel_role = 'own' AND is_active IS TRUE
    ) THEN
        RAISE EXCEPTION 'invalid active own hotel_key';
    END IF;
    IF p_feedback_type NOT IN ('general_feedback', 'price_execution_result', 'candidate_evidence', 'exception_explanation')
       OR p_source_chat_id IS NULL OR btrim(p_source_chat_id) = ''
       OR p_source_open_id IS NULL OR btrim(p_source_open_id) = ''
       OR p_source_message_id IS NULL OR btrim(p_source_message_id) = ''
       OR p_feedback_text IS NULL OR btrim(p_feedback_text) = ''
       OR p_structured_payload IS NULL OR jsonb_typeof(p_structured_payload) <> 'object' THEN
        RAISE EXCEPTION 'invalid linked branch feedback';
    END IF;
    IF p_push_ref IS NOT NULL AND btrim(p_push_ref) <> '' THEN
        SELECT r.push_id INTO linked_push_id
          FROM public.branch_push_records r
         WHERE r.hotel_key = p_hotel_key
           AND r.push_ref = upper(btrim(p_push_ref))
           AND r.delivery_status = 'sent';
        IF linked_push_id IS NULL THEN
            RAISE EXCEPTION 'push ref is not available for this hotel';
        END IF;
    END IF;
    IF p_execution_status IS NOT NULL AND p_execution_status NOT IN ('executed', 'partially_executed', 'not_executed', 'reverted') THEN
        RAISE EXCEPTION 'invalid execution status';
    END IF;
    INSERT INTO public.branch_feedbacks (
        id, hotel_key, source_app, source_chat_id, source_open_id, source_message_id,
        feedback_type, feedback_text, related_stay_date, related_room_category,
        push_id, execution_status, action_taken, executed_price, executed_at,
        result_observed_at, structured_payload, evidence_refs, status, submitted_at
    ) VALUES (
        gen_random_uuid(), p_hotel_key, 'feishu_branch', p_source_chat_id,
        p_source_open_id, p_source_message_id, p_feedback_type, p_feedback_text,
        p_related_stay_date, p_related_room_category, linked_push_id,
        p_execution_status, p_action_taken, p_executed_price, p_executed_at,
        p_result_observed_at, p_structured_payload, '[]'::jsonb, 'submitted', now()
    )
    ON CONFLICT (source_app, source_message_id) DO NOTHING
    RETURNING id INTO feedback_uuid;
    IF feedback_uuid IS NULL THEN
        SELECT id INTO feedback_uuid
          FROM public.branch_feedbacks
         WHERE source_app = 'feishu_branch'
           AND source_message_id = p_source_message_id;
    END IF;
    RETURN feedback_uuid;
END;
$function$;

CREATE OR REPLACE FUNCTION public.add_branch_candidate_evidence(
    p_hotel_key TEXT,
    p_source_chat_id TEXT,
    p_source_open_id TEXT,
    p_source_message_id TEXT,
    p_candidate_ref TEXT,
    p_evidence_type TEXT,
    p_evidence_text TEXT,
    p_related_date DATE DEFAULT NULL,
    p_related_room_category TEXT DEFAULT NULL,
    p_source_push_ref TEXT DEFAULT NULL,
    p_metric_before TEXT DEFAULT NULL,
    p_metric_after TEXT DEFAULT NULL
)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public', 'pg_temp'
AS $function$
DECLARE
    candidate_row public.experience_candidates%ROWTYPE;
    feedback_uuid UUID;
    prior_candidate_id UUID;
BEGIN
    SELECT * INTO candidate_row
      FROM public.experience_candidates
     WHERE hotel_key = p_hotel_key
       AND public.branch_candidate_ref(id) = upper(btrim(p_candidate_ref))
       AND status IN ('pending_review', 'needs_more_evidence')
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'candidate is unavailable for this hotel';
    END IF;
    IF p_evidence_type IS NULL OR btrim(p_evidence_type) = ''
       OR p_evidence_text IS NULL OR btrim(p_evidence_text) = '' THEN
        RAISE EXCEPTION 'candidate evidence is required';
    END IF;
    feedback_uuid := public.submit_branch_feedback_linked(
        p_hotel_key, p_source_chat_id, p_source_open_id, p_source_message_id,
        'candidate_evidence', p_evidence_text, p_related_date,
        p_related_room_category, p_source_push_ref, NULL, NULL, NULL, NULL, NULL,
        jsonb_strip_nulls(jsonb_build_object(
            'evidence_type', p_evidence_type,
            'metric_before', p_metric_before,
            'metric_after', p_metric_after
        ))
    );
    SELECT candidate_id INTO prior_candidate_id
      FROM public.branch_feedbacks WHERE id = feedback_uuid FOR UPDATE;
    IF prior_candidate_id IS NULL THEN
        UPDATE public.branch_feedbacks
           SET candidate_id = candidate_row.id,
               updated_at = now()
         WHERE id = feedback_uuid;
        UPDATE public.experience_candidates
           SET evidence_refs = evidence_refs || jsonb_build_array(jsonb_build_object(
                   'feedback_id', feedback_uuid,
                   'evidence_type', p_evidence_type
               )),
               status = CASE WHEN status = 'needs_more_evidence' THEN 'pending_review' ELSE status END,
               updated_at = now()
         WHERE id = candidate_row.id;
        IF candidate_row.status = 'needs_more_evidence' THEN
            INSERT INTO public.experience_candidate_reviews (
                id, candidate_id, action, comment, actor_id, review_payload
            ) VALUES (
                gen_random_uuid(), candidate_row.id, 'resubmit', '门店已补充证据',
                p_source_open_id, jsonb_build_object('feedback_id', feedback_uuid)
            );
        END IF;
    ELSIF prior_candidate_id <> candidate_row.id THEN
        RAISE EXCEPTION 'source message is already linked to another candidate';
    END IF;
    RETURN feedback_uuid;
END;
$function$;

CREATE OR REPLACE FUNCTION public.list_branch_candidate_status(
    p_hotel_key TEXT,
    p_candidate_ref TEXT DEFAULT NULL,
    p_limit INTEGER DEFAULT 5
)
RETURNS TABLE (
    candidate_ref TEXT,
    experience_summary TEXT,
    status TEXT,
    mnemosyne_sync_status TEXT,
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    latest_review_comment TEXT
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO 'public', 'pg_temp'
AS $$
    SELECT public.branch_candidate_ref(c.id), c.experience_summary, c.status,
           c.mnemosyne_sync_status, c.created_at, c.updated_at, latest.comment
      FROM public.experience_candidates c
      LEFT JOIN LATERAL (
          SELECT r.comment
            FROM public.experience_candidate_reviews r
           WHERE r.candidate_id = c.id
           ORDER BY r.created_at DESC, r.id DESC
           LIMIT 1
      ) latest ON TRUE
     WHERE c.hotel_key = p_hotel_key
       AND (p_candidate_ref IS NULL OR public.branch_candidate_ref(c.id) = upper(btrim(p_candidate_ref)))
     ORDER BY c.updated_at DESC, c.id DESC
     LIMIT LEAST(GREATEST(COALESCE(p_limit, 5), 1), 5)
$$;

REVOKE ALL ON TABLE public.branch_push_records FROM PUBLIC;
REVOKE ALL ON FUNCTION public.reserve_branch_push(TEXT, TEXT, TEXT, TEXT, TEXT, JSONB) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.mark_branch_push_delivery(UUID, UUID, TEXT, TEXT, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.resolve_branch_push_by_ref(TEXT, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.submit_branch_feedback_linked(TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, DATE, TEXT, TEXT, TEXT, TEXT, NUMERIC, TIMESTAMPTZ, TIMESTAMPTZ, JSONB) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.add_branch_candidate_evidence(TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, DATE, TEXT, TEXT, TEXT, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.list_branch_candidate_status(TEXT, TEXT, INTEGER) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION public.reserve_branch_push(TEXT, TEXT, TEXT, TEXT, TEXT, JSONB) TO hotel_agent;
GRANT EXECUTE ON FUNCTION public.mark_branch_push_delivery(UUID, UUID, TEXT, TEXT, TEXT) TO hotel_agent;
GRANT EXECUTE ON FUNCTION public.resolve_branch_push_by_ref(TEXT, TEXT) TO hotel_branch_agent;
GRANT EXECUTE ON FUNCTION public.submit_branch_feedback_linked(TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, DATE, TEXT, TEXT, TEXT, TEXT, NUMERIC, TIMESTAMPTZ, TIMESTAMPTZ, JSONB) TO hotel_branch_agent;
GRANT EXECUTE ON FUNCTION public.add_branch_candidate_evidence(TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, DATE, TEXT, TEXT, TEXT, TEXT) TO hotel_branch_agent;
GRANT EXECUTE ON FUNCTION public.list_branch_candidate_status(TEXT, TEXT, INTEGER) TO hotel_branch_agent;

COMMIT;
