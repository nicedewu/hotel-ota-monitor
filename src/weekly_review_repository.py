from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from src.db_writer import DBWriterError, Jsonb, _load_database_url, psycopg
from src.exhibition_timing import annotate_events_for_period
from src.historical_facts import (
    fact_comparison_records,
    fact_comparison_rows,
    fact_decision_rows,
    load_fact_snapshot_rows,
    merge_prefer_new,
    metrics_to_pms_rows,
)


class WeeklyReviewRepositoryError(DBWriterError):
    pass


def connect() -> Any:
    if psycopg is None or Jsonb is None:
        raise WeeklyReviewRepositoryError("缺少 psycopg 依赖，请执行 pip install -r requirements.txt")
    return psycopg.connect(_load_database_url(), row_factory=psycopg.rows.dict_row)


def table_exists(cursor: Any, table: str) -> bool:
    cursor.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = %s
        )
        """,
        (table,),
    )
    return bool(cursor.fetchone()["exists"])


def fetch_db_hotel_config() -> dict[str, dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT hotel_key, hotel_name, hotel_role, ota_url, is_active FROM hotels ORDER BY hotel_key")
            hotels = {str(row["hotel_key"]): dict(row) for row in cursor.fetchall()}
            if table_exists(cursor, "hotel_competitors"):
                cursor.execute("SELECT own_hotel_key, competitor_hotel_key FROM hotel_competitors WHERE is_active = TRUE ORDER BY priority, id")
                for row in cursor.fetchall():
                    hotels.setdefault(str(row["own_hotel_key"]), {}).setdefault("competitors", []).append(str(row["competitor_hotel_key"]))
            if table_exists(cursor, "hotel_push_routes"):
                cursor.execute(
                    """
                    SELECT own_hotel_key, webhook_env_key
                    FROM hotel_push_routes
                    WHERE is_active = TRUE AND push_type IN ('weekly_review', 'business_snapshot')
                    ORDER BY CASE WHEN push_type = 'weekly_review' THEN 0 ELSE 1 END, id
                    """
                )
                for row in cursor.fetchall():
                    hotels.setdefault(str(row["own_hotel_key"]), {}).setdefault("webhook_env_key", str(row["webhook_env_key"]))
    return hotels


def fetch_weekly_facts(hotel_key: str, competitor_keys: list[str], week_start: date, week_end: date, venue: str = "") -> dict[str, Any]:
    comparison_start = week_start - timedelta(days=7)
    comparison_dates = [comparison_start + timedelta(days=offset) for offset in range((week_end - comparison_start).days + 1)]
    with connect() as conn:
        with conn.cursor() as cursor:
            fact_snapshots = load_fact_snapshot_rows(
                cursor, hotel_key, comparison_start, week_end
            )
            metrics_source_rows: list[dict[str, Any]] = []
            if table_exists(cursor, "hotel_daily_operating_metrics"):
                cursor.execute(
                    "SELECT business_date, metrics, updated_at FROM hotel_daily_operating_metrics WHERE hotel_key=%s AND business_date BETWEEN %s AND %s ORDER BY business_date",
                    (hotel_key, comparison_start, week_end),
                )
                metrics_source_rows = [dict(row) for row in cursor.fetchall()]
            structured_pms_rows = metrics_to_pms_rows(metrics_source_rows, required_slot="final")
            all_pms_rows = structured_pms_rows
            pms_rows = [row for row in all_pms_rows if row["stay_date"] >= week_start]
            previous_pms_rows = [row for row in all_pms_rows if row["stay_date"] < week_start]
            final_dates = {str(row.get("stay_date") or "") for row in all_pms_rows}
            current_final_missing_dates = [day.isoformat() for day in comparison_dates if day >= week_start and day.isoformat() not in final_dates]
            previous_final_missing_dates = [day.isoformat() for day in comparison_dates if day < week_start and day.isoformat() not in final_dates]

            price_keys = [hotel_key, *competitor_keys]
            cursor.execute(
                """
                SELECT source_hotel_key AS hotel_key, hotel_name, stay_date, room_name,
                       COALESCE(normalized_room_type, room_category) AS room_category,
                       price, available, collected_at, raw_payload AS raw
                FROM ota_room_prices
                WHERE source_hotel_key = ANY(%s)
                  AND stay_date BETWEEN %s AND %s
                  AND available = TRUE AND price > 0
                ORDER BY stay_date, source_hotel_key, room_category, collected_at DESC, price ASC
                """,
                (price_keys, week_start, week_end),
            )
            legacy_price_rows = [dict(row) for row in cursor.fetchall()]
            structured_price_rows = fact_comparison_rows(
                fact_snapshots,
                start_date=week_start,
                end_date=week_end,
                own_hotel_key=hotel_key,
            )
            price_rows = merge_prefer_new(
                structured_price_rows,
                legacy_price_rows,
                ("stay_date", "hotel_key", "room_category", "price"),
            )

            structured_comparisons = fact_comparison_records(
                fact_snapshots,
                start_date=week_start,
                end_date=week_end,
                own_hotel_key=hotel_key,
            )
            comparisons_missing = not table_exists(cursor, "price_comparisons") and not structured_comparisons
            comparisons: list[dict[str, Any]] = list(structured_comparisons)
            if not comparisons_missing:
                cursor.execute(
                    """
                    SELECT *
                    FROM price_comparisons
                    WHERE own_hotel_key = %s AND stay_date BETWEEN %s AND %s
                    ORDER BY stay_date, competitor_hotel_key, normalized_room_type, collected_at DESC
                    """,
                    (hotel_key, week_start, week_end),
                )
                comparisons = merge_prefer_new(
                    structured_comparisons,
                    [dict(row) for row in cursor.fetchall()],
                    ("stay_date", "normalized_room_type", "competitor_hotel_key", "collected_at"),
                )

            structured_decisions = fact_decision_rows(
                fact_snapshots,
                start_date=week_start,
                end_date=week_end,
            )
            cursor.execute(
                """
                SELECT id, run_id, COALESCE(own_hotel_key, hotel_key) AS hotel_key, stay_date,
                       COALESCE(normalized_room_type, room_category) AS room_category,
                       occupancy_rate, COALESCE(current_price, own_current_price) AS current_price,
                       competitor_min_price, COALESCE(suggested_price, recommended_price) AS suggested_price,
                       COALESCE(decision_action, recommended_action) AS action,
                       confidence, reason, decision_at, COALESCE(input_snapshot, raw) AS raw
                FROM price_decisions
                WHERE COALESCE(own_hotel_key, hotel_key) = %s AND stay_date BETWEEN %s AND %s
                ORDER BY stay_date, room_category, decision_at DESC, id DESC
                """,
                (hotel_key, week_start, week_end),
            )
            legacy_decisions = [dict(row) for row in cursor.fetchall()]
            decisions = merge_prefer_new(
                structured_decisions,
                legacy_decisions,
                ("stay_date", "room_category"),
            )

            event_values: tuple[Any, ...]
            event_filter = ""
            if venue:
                event_filter = "AND (venue = %s OR venue IS NULL OR venue = '')"
                event_values = (week_end, week_start, venue)
            else:
                event_values = (week_end, week_start)
            cursor.execute(
                f"""
                SELECT event_name, venue, event_start_date, event_end_date, stay_start_date, stay_end_date,
                       category, demand_level
                FROM event_occurrences
                WHERE is_business_active = TRUE
                  AND canonical_status <> 'cancelled'
                  AND COALESCE(stay_start_date, event_start_date) <= %s
                  AND COALESCE(stay_end_date, event_end_date) >= %s
                  AND (stay_start_date IS NOT NULL OR event_start_date IS NOT NULL)
                  {event_filter}
                ORDER BY COALESCE(stay_start_date, event_start_date), event_name
                """,
                event_values,
            )
            events = annotate_events_for_period(week_start, week_end, [dict(row) for row in cursor.fetchall()])

            previous_reviews: list[dict[str, Any]] = []
            if table_exists(cursor, "weekly_reviews"):
                cursor.execute(
                    """
                    SELECT week_start, week_end, hotel_key, analysis_mode, report_json
                    FROM weekly_reviews
                    WHERE hotel_key = %s AND week_end < %s
                    ORDER BY week_end DESC
                    LIMIT 4
                    """,
                    (hotel_key, week_start),
                )
                previous_reviews = [dict(row) for row in cursor.fetchall()]
            daily_metrics: list[dict[str, Any]] = []
            if table_exists(cursor, "hotel_daily_operating_metrics"):
                cursor.execute(
                    "SELECT business_date, metrics FROM hotel_daily_operating_metrics WHERE hotel_key=%s AND business_date BETWEEN %s AND %s ORDER BY business_date",
                    (hotel_key, week_start, week_end),
                )
                daily_metrics = [dict(row) for row in cursor.fetchall()]
            change_events: list[dict[str, Any]] = []
            if table_exists(cursor, "inventory_change_events"):
                cursor.execute(
                    "SELECT stay_date, current_at, elapsed_minutes, sold_before, sold_after, available_before, available_after, room_type_deltas FROM inventory_change_events WHERE hotel_key=%s AND stay_date BETWEEN %s AND %s ORDER BY current_at",
                    (hotel_key, week_start, week_end),
                )
                change_events = [dict(row) for row in cursor.fetchall()]
            occupancy_alerts: list[dict[str, Any]] = []
            if table_exists(cursor, "occupancy_alerts"):
                cursor.execute(
                    """
                    SELECT alert_at, stay_date, alert_type, webhook_sent, raw_context
                    FROM occupancy_alerts
                    WHERE hotel_key = %s AND stay_date BETWEEN %s AND %s
                    ORDER BY alert_at
                    """,
                    (hotel_key, week_start, week_end),
                )
                occupancy_alerts = [dict(row) for row in cursor.fetchall()]
    return {
        "pms_rows": pms_rows,
        "previous_pms_rows": previous_pms_rows,
        "pms_current_week_final_missing_dates": current_final_missing_dates,
        "pms_previous_week_final_missing_dates": previous_final_missing_dates,
        "price_rows": price_rows,
        "price_comparisons": comparisons,
        "price_comparisons_missing": comparisons_missing,
        "price_decisions": decisions,
        "exhibition_events": events,
        "previous_reviews": previous_reviews,
        "daily_operating_metrics": daily_metrics,
        "inventory_change_events": change_events,
        "occupancy_alerts": occupancy_alerts,
        "source_status": {
            "pms": "hotel_daily_operating_metrics.metrics.snapshots.final",
            "price_rows": "weekly_reviews.fact_snapshot_json" if structured_price_rows else "ota_room_prices_fallback",
            "price_comparisons": "weekly_reviews.fact_snapshot_json" if structured_comparisons else "price_comparisons_fallback",
            "price_decisions": "weekly_reviews.fact_snapshot_json" if structured_decisions else "price_decisions_fallback",
        },
    }


def write_weekly_review(row: dict[str, Any]) -> dict[str, Any]:
    with connect() as conn:
        with conn.cursor() as cursor:
            if not table_exists(cursor, "weekly_reviews"):
                return {"status": "table_missing", "id": None}
            values = {
                **row,
                "report_json": Jsonb(json.loads(json.dumps(row["report_json"], ensure_ascii=False, default=str))),
                "fact_snapshot_json": Jsonb(json.loads(json.dumps(row["fact_snapshot_json"], ensure_ascii=False, default=str))),
            }
            cursor.execute(
                """
                UPDATE weekly_reviews SET
                    generated_at = now(), week_end = %(week_end)s, hotel_name = %(hotel_name)s,
                    review_type = %(review_type)s, report_markdown = %(report_markdown)s,
                    report_json = %(report_json)s, run_id = %(run_id)s,
                    analysis_mode = %(analysis_mode)s, analysis_status = %(analysis_status)s,
                    analysis_degraded = %(analysis_degraded)s, analysis_model = %(analysis_model)s,
                    analysis_model_version = %(analysis_model_version)s, analysis_schema_version = %(analysis_schema_version)s,
                    analysis_duration_ms = %(analysis_duration_ms)s, analysis_quality_status = %(analysis_quality_status)s,
                    analysis_error_code = %(analysis_error_code)s, analysis_error_message = %(analysis_error_message)s,
                    fact_snapshot_json = %(fact_snapshot_json)s, fact_quality_passed = %(fact_quality_passed)s,
                    notify_status = %(notify_status)s
                WHERE id = (
                    SELECT id FROM weekly_reviews
                    WHERE hotel_key = %(hotel_key)s AND week_start = %(week_start)s
                    ORDER BY generated_at DESC, id DESC LIMIT 1
                )
                """,
                values,
            )
            if not cursor.rowcount:
                cursor.execute(
                """
                INSERT INTO weekly_reviews (
                    week_start, week_end, hotel_key, hotel_name, review_type,
                    report_markdown, report_json, notify_status, run_id,
                    analysis_mode, analysis_status, analysis_degraded, analysis_model, analysis_model_version,
                    analysis_schema_version, analysis_duration_ms, analysis_quality_status,
                    analysis_error_code, analysis_error_message, fact_snapshot_json, fact_quality_passed
                ) VALUES (
                    %(week_start)s, %(week_end)s, %(hotel_key)s, %(hotel_name)s, %(review_type)s,
                    %(report_markdown)s, %(report_json)s, %(notify_status)s, %(run_id)s,
                    %(analysis_mode)s, %(analysis_status)s, %(analysis_degraded)s, %(analysis_model)s, %(analysis_model_version)s,
                    %(analysis_schema_version)s, %(analysis_duration_ms)s, %(analysis_quality_status)s,
                    %(analysis_error_code)s, %(analysis_error_message)s, %(fact_snapshot_json)s, %(fact_quality_passed)s
                )
                RETURNING id
                """,
                    values,
                )
                review_id = cursor.fetchone()["id"]
            else:
                cursor.execute("SELECT id FROM weekly_reviews WHERE hotel_key=%s AND week_start=%s ORDER BY generated_at DESC, id DESC LIMIT 1", (row["hotel_key"], row["week_start"]))
                review_id = cursor.fetchone()["id"]
        conn.commit()
    return {"status": "written", "id": review_id}


def fetch_weekly_review_states(week_start: date, hotel_keys: list[str]) -> dict[str, dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cursor:
            if not table_exists(cursor, "weekly_reviews"):
                return {}
            cursor.execute(
                """
                SELECT DISTINCT ON (hotel_key) id, hotel_key, hotel_name, report_markdown, report_json,
                       notify_status, run_id, analysis_mode, analysis_status, fact_quality_passed
                FROM weekly_reviews
                WHERE week_start = %s AND hotel_key = ANY(%s)
                ORDER BY hotel_key, generated_at DESC, id DESC
                """,
                (week_start, hotel_keys),
            )
            return {str(row["hotel_key"]): dict(row) for row in cursor.fetchall()}


def fetch_weekly_reviews_for_delivery(week_start: date, hotel_keys: list[str]) -> dict[str, dict[str, Any]]:
    return fetch_weekly_review_states(week_start, hotel_keys)


def upsert_admin_review(row: dict[str, Any]) -> str:
    with connect() as conn:
        with conn.cursor() as cursor:
            if not table_exists(cursor, "weekly_admin_reviews"):
                return "table_missing"
            values = {
                **row,
                "hotel_keys": Jsonb(row["hotel_keys"]),
                "metrics": Jsonb(json.loads(json.dumps(row["metrics"], ensure_ascii=False, default=str))),
                "quality_checks": Jsonb(json.loads(json.dumps(row["quality_checks"], ensure_ascii=False, default=str))),
            }
            cursor.execute(
                """
                INSERT INTO weekly_admin_reviews (
                    week_start, week_end, timezone, expected_hotel_count, actual_hotel_count,
                    complete_hotel_count, degraded_hotel_count, blocked_hotel_count,
                    generation_status, push_status, report_version, hotel_keys, metrics,
                     report_text, quality_checks, generated_at, generation_error
                ) VALUES (
                    %(week_start)s, %(week_end)s, %(timezone)s, %(expected_hotel_count)s, %(actual_hotel_count)s,
                    %(complete_hotel_count)s, %(degraded_hotel_count)s, %(blocked_hotel_count)s,
                    %(generation_status)s, 'pending', %(report_version)s, %(hotel_keys)s, %(metrics)s,
                     %(report_text)s, %(quality_checks)s, now(), %(generation_error)s
                )
                ON CONFLICT (week_start, report_version) DO UPDATE SET
                    week_end = EXCLUDED.week_end, timezone = EXCLUDED.timezone,
                    expected_hotel_count = EXCLUDED.expected_hotel_count,
                    actual_hotel_count = EXCLUDED.actual_hotel_count,
                    complete_hotel_count = EXCLUDED.complete_hotel_count,
                    degraded_hotel_count = EXCLUDED.degraded_hotel_count,
                    blocked_hotel_count = EXCLUDED.blocked_hotel_count,
                    generation_status = EXCLUDED.generation_status,
                    hotel_keys = EXCLUDED.hotel_keys, metrics = EXCLUDED.metrics,
                    report_text = EXCLUDED.report_text, quality_checks = EXCLUDED.quality_checks,
                    generated_at = now(),
                    generation_error = EXCLUDED.generation_error, updated_at = now()
                WHERE weekly_admin_reviews.push_status <> 'pushed'
                RETURNING id
                """,
                values,
            )
            changed = cursor.fetchone()
        conn.commit()
    return "written" if changed else "pushed_preserved"


def get_admin_review(week_start: date, report_version: str = "v1") -> dict[str, Any] | None:
    with connect() as conn:
        with conn.cursor() as cursor:
            if not table_exists(cursor, "weekly_admin_reviews"):
                return None
            cursor.execute(
                "SELECT * FROM weekly_admin_reviews WHERE week_start = %s AND report_version = %s",
                (week_start, report_version),
            )
            row = cursor.fetchone()
            return dict(row) if row else None


def claim_admin_review_for_push(week_start: date, report_version: str = "v1") -> dict[str, Any] | None:
    with connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE weekly_admin_reviews
                SET push_status = 'pushing', push_error = NULL, updated_at = now()
                WHERE week_start = %s AND report_version = %s
                  AND generation_status IN ('ready_to_push', 'degraded_ready')
                  AND push_status IN ('pending', 'push_failed')
                RETURNING *
                """,
                (week_start, report_version),
            )
            row = cursor.fetchone()
        conn.commit()
    return dict(row) if row else None


def finish_admin_review_push(week_start: date, status: str, error: str = "", report_version: str = "v1") -> str:
    with connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE weekly_admin_reviews
                SET push_status = %s, pushed_at = CASE WHEN %s = 'pushed' THEN now() ELSE pushed_at END,
                    push_error = NULLIF(%s, ''), updated_at = now()
                WHERE week_start = %s AND report_version = %s AND push_status = 'pushing'
                """,
                (status, status, error[:500], week_start, report_version),
            )
            updated = cursor.rowcount
        conn.commit()
    return "updated" if updated else "not_found"


def update_weekly_review_notify_status(run_id: str, hotel_key: str, status: str) -> str:
    with connect() as conn:
        with conn.cursor() as cursor:
            if not table_exists(cursor, "weekly_reviews"):
                return "table_missing"
            cursor.execute(
                "UPDATE weekly_reviews SET notify_status = %s WHERE run_id = %s AND hotel_key = %s",
                (status, run_id, hotel_key),
            )
            updated = cursor.rowcount
        conn.commit()
    return "updated" if updated else "not_found"


def data_health(stay_date: date, hotel_keys: list[str]) -> dict[str, Any]:
    with connect() as conn:
        with conn.cursor() as cursor:
            keys = hotel_keys or []
            result: dict[str, Any] = {"date": stay_date.isoformat(), "hotels_checked": len(keys), "warnings": []}
            for table, alias in (
                ("own_daily_status", "own_daily_status_rows"),
                ("ota_room_prices", "ota_room_prices_rows"),
                ("price_decisions", "price_decisions_rows"),
            ):
                key_column = "COALESCE(own_hotel_key, hotel_key)" if table == "price_decisions" else "COALESCE(source_hotel_key, hotel_key)" if table == "ota_room_prices" else "hotel_key"
                cursor.execute(
                    f"SELECT COUNT(*) AS count FROM {table} WHERE stay_date = %s AND ({not bool(keys)} OR {key_column} = ANY(%s))",
                    (stay_date, keys),
                )
                result[alias] = int(cursor.fetchone()["count"])
            if table_exists(cursor, "price_comparisons"):
                cursor.execute(
                    "SELECT COUNT(*) AS count FROM price_comparisons WHERE stay_date = %s AND (%s OR own_hotel_key = ANY(%s))",
                    (stay_date, not bool(keys), keys),
                )
                result["price_comparisons_rows"] = int(cursor.fetchone()["count"])
            else:
                result["price_comparisons_rows"] = "table_missing"
                result["warnings"].append("price_comparisons table missing")
            cursor.execute(
                """
                SELECT COUNT(*) AS count FROM event_occurrences
                WHERE is_business_active = TRUE
                  AND canonical_status <> 'cancelled'
                  AND COALESCE(stay_start_date, event_start_date) <= %s
                  AND COALESCE(stay_end_date, event_end_date) >= %s
                """,
                (stay_date, stay_date),
            )
            result["exhibition_events_rows"] = int(cursor.fetchone()["count"])
    return result
