ALTER TABLE public.branch_push_records
    DROP CONSTRAINT IF EXISTS branch_push_records_push_type_check;

ALTER TABLE public.branch_push_records
    ADD CONSTRAINT branch_push_records_push_type_check
    CHECK (push_type = ANY (ARRAY[
        'business_snapshot'::text,
        'price_recommendation'::text,
        'price_comparison'::text,
        'inventory_alert'::text,
        'operation_notice'::text,
        'weekly_review'::text
    ]));
