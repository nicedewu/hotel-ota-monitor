from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.db_writer import DBWriterError, psycopg
from src.historical_facts import fact_decision_rows, load_fact_snapshot_rows, merge_prefer_new
from src.weekly_review_repository import connect


DEFAULT_REVIEW_JSON = Path("output/latest/weekly_pricing_review_payload.json")
DEFAULT_REVIEW_MD = Path("output/latest/weekly_pricing_review.md")


class WeeklyPricingReviewError(RuntimeError):
    pass


def build_weekly_pricing_review(
    week_start: str,
    week_end: str,
    *,
    json_path: Path = DEFAULT_REVIEW_JSON,
    markdown_path: Path = DEFAULT_REVIEW_MD,
) -> dict[str, Any]:
    start, end = _date_range(week_start, week_end)
    if psycopg is None:
        raise WeeklyPricingReviewError("缺少 psycopg 依赖，请执行 pip install -r requirements.txt")
    try:
        with connect() as conn:
            with conn.cursor() as cursor:
                snapshots = load_fact_snapshot_rows(cursor, start_date=start, end_date=end)
                structured_rows = fact_decision_rows(
                    snapshots,
                    start_date=start,
                    end_date=end + timedelta(days=1),
                )
                cursor.execute(
                    """
                    SELECT COALESCE(own_hotel_key, hotel_key) AS hotel_key, stay_date,
                           COALESCE(normalized_room_type, room_category) AS room_category,
                           COALESCE(decision_action, recommended_action) AS action,
                           COALESCE(suggested_price, recommended_price) AS suggested_price,
                           confidence, reason
                    FROM price_decisions
                    WHERE stay_date BETWEEN %s AND %s
                    ORDER BY hotel_key, stay_date, room_category
                    """,
                    (start, end),
                )
                legacy_rows = [dict(row) for row in cursor.fetchall()]
                structured_keys = {(str(row.get("hotel_key") or ""), str(row.get("stay_date") or ""), str(row.get("room_category") or "")) for row in structured_rows}
                legacy_fallback = [
                    row for row in legacy_rows
                    if (str(row.get("hotel_key") or ""), str(row.get("stay_date") or ""), str(row.get("room_category") or "")) not in structured_keys
                ]
                rows = merge_prefer_new(structured_rows, legacy_fallback, ("hotel_key", "stay_date", "room_category"))
                source_tables = []
                if structured_rows:
                    source_tables.append("weekly_reviews.fact_snapshot_json")
                if legacy_fallback:
                    source_tables.append("price_decisions")
    except (DBWriterError, WeeklyPricingReviewError):
        raise
    except Exception as exc:
        raise WeeklyPricingReviewError(f"读取周度价格复盘事实失败: {type(exc).__name__}: {exc}") from exc
    payload = {
        "schema_version": "1.0",
        "week_start": start.isoformat(),
        "week_end": end.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_tables": source_tables or ["price_decisions"],
        "source_mode": "structured_fact" if structured_rows and not legacy_fallback else "structured_fact+legacy_fallback" if structured_rows else "legacy_fallback",
        "summary": {
            "row_count": len(rows),
            "structured_row_count": len(structured_rows),
            "legacy_fallback_row_count": len(legacy_fallback),
            "hotel_count": len({str(row.get('hotel_key')) for row in rows}),
        },
        "rows": rows,
    }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(_render_review_markdown(payload), encoding="utf-8")
    return payload


def _date_range(week_start: str, week_end: str) -> tuple[date, date]:
    try:
        start, end = date.fromisoformat(week_start), date.fromisoformat(week_end)
    except ValueError as exc:
        raise WeeklyPricingReviewError("week-start/week-end 必须是 YYYY-MM-DD") from exc
    if end < start:
        raise WeeklyPricingReviewError("week-end 不能早于 week-start")
    return start, end


def _render_review_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# 周度价格复盘事实汇总",
        f"周期：{payload['week_start']} 至 {payload['week_end']}",
        f"价格决策记录：{payload['summary']['row_count']} 条",
        "",
    ]
    for row in payload["rows"][:60]:
        lines.append(
            f"- {row.get('hotel_key')}｜{row.get('stay_date')}｜{row.get('room_category') or '未分类'}｜"
            f"{row.get('action') or '无动作'}"
        )
    return "\n".join(lines)
