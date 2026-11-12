from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any
from uuid import uuid4

from src.config import load_hotels
from src.db_writer import DBWriterError, Jsonb, _load_database_url, psycopg
from src.exhibition_timing import partition_events_for_day
from src.historical_facts import fact_comparison_rows, fact_decision_rows, load_fact_snapshot_rows
from src.memory_provider import memory_backend_status, remember_hotel
from src.memory_router import memory_router_status, recall_for_pricing_decision
from src.multi_hotel import match_competitor_candidates
from src.utils import resolve_stay_dates


FETCH_STATUS_SQL = """
SELECT hotel_key, stay_date, room_total, occupied_rooms, available_rooms, sold_rooms, booking_count, occupancy_rate, collected_at, raw
FROM own_daily_status
WHERE hotel_key = %s AND stay_date = %s
ORDER BY collected_at DESC
LIMIT 1
"""

FETCH_PRICES_SQL = """
WITH latest_runs AS (
    SELECT DISTINCT ON (source_hotel_key) source_hotel_key, run_id
    FROM ota_room_prices
    WHERE stay_date = %s AND available = TRUE AND price > 0
    ORDER BY source_hotel_key, (run_id IS NOT NULL) DESC, collected_at DESC
)
SELECT p.source_hotel_key AS hotel_key, p.hotel_name, p.room_name,
       p.normalized_room_type AS room_category, p.stay_date, p.price,
       p.collected_at, p.raw_payload AS raw
FROM ota_room_prices p
JOIN latest_runs latest ON latest.source_hotel_key = p.source_hotel_key
WHERE p.stay_date = %s AND p.available = TRUE AND p.price > 0
  AND ((latest.run_id IS NULL AND p.run_id IS NULL) OR p.run_id = latest.run_id)
ORDER BY p.collected_at DESC, p.price ASC
"""

FETCH_PRICES_FOR_RUN_SQL = """
SELECT source_hotel_key AS hotel_key, hotel_name, room_name,
       normalized_room_type AS room_category, stay_date, price,
       collected_at, raw_payload AS raw
FROM ota_room_prices
WHERE stay_date = %s AND run_id = %s AND available = TRUE AND price > 0
ORDER BY collected_at DESC, price ASC
"""

INSERT_DECISION_SQL = """
INSERT INTO price_decisions (
    run_id, decision_at, hotel_key, own_hotel_key, stay_date, room_category,
    normalized_room_type, core_feature_signature, occupancy_rate, own_current_price, current_price,
    competitor_min_price, recommended_price, suggested_price, recommended_action,
    decision_action, confidence, reason, raw, input_snapshot
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

MAX_MANUAL_PRICE_REDUCTION_PCT = 5.0

FETCH_HISTORICAL_DECISIONS_SQL = """
SELECT *
FROM (
    SELECT DISTINCT ON (d.stay_date)
           d.id, d.stay_date, COALESCE(d.normalized_room_type, d.room_category) AS room_category,
           COALESCE(d.current_price, d.own_current_price) AS own_price,
           d.competitor_min_price,
           COALESCE(d.suggested_price, d.recommended_price) AS suggested_price,
           COALESCE(d.decision_action, d.recommended_action) AS action,
           d.reason, d.decision_at, d.raw,
           s.room_total, s.sold_rooms, s.available_rooms, s.occupancy_rate, s.raw AS status_raw
    FROM price_decisions d
    LEFT JOIN LATERAL (
        SELECT room_total, sold_rooms, available_rooms, occupancy_rate, raw
        FROM own_daily_status
        WHERE hotel_key = COALESCE(d.own_hotel_key, d.hotel_key)
          AND stay_date = d.stay_date
        ORDER BY collected_at DESC
        LIMIT 1
    ) s ON TRUE
    WHERE COALESCE(d.own_hotel_key, d.hotel_key) = %s
      AND d.stay_date < %s
      AND COALESCE(d.normalized_room_type, d.room_category) = %s
    ORDER BY d.stay_date, d.decision_at DESC, d.id DESC
) recent
ORDER BY stay_date DESC
LIMIT 30
"""

FETCH_HISTORICAL_OTA_SQL = """
SELECT stay_date, source_hotel_key, MIN(price) AS min_price, MAX(collected_at) AS latest_collected_at
FROM ota_room_prices
WHERE stay_date = ANY(%s)
  AND source_hotel_key = ANY(%s)
  AND COALESCE(normalized_room_type, room_category) = %s
  AND available = TRUE AND price > 0
GROUP BY stay_date, source_hotel_key
ORDER BY stay_date DESC, source_hotel_key
"""

WEEKDAY_CN = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

def generate_price_decisions(
    stay_date: str | None = None,
    days: int = 1,
    own_hotel_key: str = "own_demo_a",
    config_path: str = "hotels.json",
) -> list[dict[str, Any]]:
    """Append eligible room-category price decisions and return them."""
    decisions, _, _, _ = _generate(stay_date, days, own_hotel_key, config_path)
    return decisions


def generate_price_decision_brief(
    *,
    stay_date: str | None = None,
    days: int = 1,
    own_hotel_key: str = "own_demo_a",
    output_path: str = "output/latest/price_decision_brief.md",
    config_path: str = "hotels.json",
    run_id: str | None = None,
    business_comparison: dict[str, Any] | None = None,
    persist: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, datetime]:
    decisions, skipped, run_id, decision_at = _generate(stay_date, days, own_hotel_key, config_path, run_id, business_comparison, persist)
    _write_brief(decisions, skipped, output_path, run_id, decision_at)
    return decisions, skipped, run_id, decision_at


def add_pricing_lesson(
    *,
    stay_date: str,
    text: str,
    own_hotel_key: str = "own_demo_a",
    source: str = "manual",
    event_name: str = "",
    room_category: str = "",
) -> dict[str, Any]:
    if source not in {"feishu_analysis", "manual", "postmortem"}:
        raise DBWriterError("source 只支持 feishu_analysis/manual/postmortem")
    try:
        parsed_date = date.fromisoformat(stay_date)
    except ValueError as exc:
        raise DBWriterError("date 必须是 YYYY-MM-DD") from exc
    if not text.strip():
        raise DBWriterError("text 不能为空")
    backend = memory_backend_status()
    if not (backend["enabled"] and backend["module_available"] and backend["write_mode"] == "direct"):
        raise DBWriterError("Mnemosyne 当前不是 direct 写入模式，拒绝把 pricing_lesson 仅写入 staging")
    if not event_name:
        if psycopg is None:
            raise DBWriterError("缺少 psycopg 依赖，请执行 pip install -r requirements.txt")
        try:
            with psycopg.connect(_load_database_url(), row_factory=psycopg.rows.dict_row) as connection:
                with connection.cursor() as cursor:
                    events, _ = partition_events_for_day(
                        stay_date,
                        _fetch_hotel_exhibitions(cursor, own_hotel_key, stay_date),
                    )
            event_name = "、".join(str(item.get("event_name") or "") for item in events if item.get("event_name")) or "none"
        except Exception as exc:
            raise DBWriterError(f"读取展会 metadata 失败: {type(exc).__name__}: {exc}") from exc
    metadata = {
        "tenant": "hotel_ops",
        "memory_type": "pricing_lesson",
        "hotel_key": own_hotel_key,
        "hotel_short_name": _hotel_short_name(own_hotel_key),
        "rule_type": "pricing_lesson",
        "scope": "room_type" if room_category else "hotel",
        "priority": 100,
        "stay_date": stay_date,
        "weekday": parsed_date.strftime("%A"),
        "source": source,
        "confidence": "high",
        "valid_from": stay_date,
        "valid_to": None,
        "event_name": event_name,
    }
    if room_category:
        metadata["room_category"] = room_category
    outcome = remember_hotel(
        own_hotel_key,
        text.strip(),
        "pricing_lesson",
        source=source,
        confidence="high",
        metadata=metadata,
    )
    if outcome != "direct":
        raise DBWriterError(f"pricing_lesson 未直接写入 Mnemosyne，状态={outcome}")
    return {"status": outcome, "metadata": metadata}


def review_price_decisions(
    *,
    stay_date: str,
    own_hotel_key: str = "own_demo_a",
    output_path: str = "",
) -> dict[str, Any]:
    try:
        date.fromisoformat(stay_date)
    except ValueError as exc:
        raise DBWriterError("date 必须是 YYYY-MM-DD") from exc
    decisions = load_latest_price_decisions(own_hotel_key, stay_date, 1)
    if not decisions:
        raise DBWriterError(f"{stay_date} 没有可复盘的 price_decisions")
    if psycopg is None:
        raise DBWriterError("缺少 psycopg 依赖，请执行 pip install -r requirements.txt")
    try:
        with psycopg.connect(_load_database_url(), row_factory=psycopg.rows.dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(FETCH_STATUS_SQL, (own_hotel_key, stay_date))
                status = cursor.fetchone()
                events, coverage_events = partition_events_for_day(
                    stay_date,
                    _fetch_hotel_exhibitions(cursor, own_hotel_key, stay_date),
                )
    except Exception as exc:
        raise DBWriterError(f"读取调价复盘事实失败: {type(exc).__name__}: {exc}") from exc
    factors = _status_factors(status)
    sales_rate = factors["actual_sales_rate"]
    reviews: list[dict[str, Any]] = []
    for row in decisions:
        current = _number(row.get("current_price")) or _number(row.get("own_current_price"))
        suggested = _number(row.get("suggested_price")) or _number(row.get("recommended_price"))
        action = str(row.get("decision_action") or row.get("recommended_action") or "未知")
        discount = 0.0 if not current or suggested is None else round(max(0.0, (current - suggested) / current * 100), 2)
        if sales_rate is None:
            assessment = "缺少最终真实销售率，暂不能评价建议效果"
        elif sales_rate >= 0.85 and action in {"维持", "保持", "谨慎上调", "上调"}:
            assessment = "最终销售率较高，结果支持当日价格保护方向"
        elif sales_rate >= 0.85 and discount > 0:
            assessment = "最终销售率较高，需复盘该降幅是否过度"
        elif sales_rate < 0.70 and discount == 0:
            assessment = "最终销售率偏低，需复盘维持/上调是否错过调整窗口"
        else:
            assessment = "结果与建议方向未形成明显冲突，继续积累同房型样本"
        reviews.append({
            "room_category": row.get("normalized_room_type") or row.get("room_category"),
            "action": action,
            "current_price": current,
            "suggested_price": suggested,
            "discount_pct": discount,
            "assessment": assessment,
            "price_decision_id": row.get("id"),
        })
    event_names = "、".join(str(item.get("event_name") or "") for item in events if item.get("event_name")) or "无展会"
    action_summary = "；".join(f"{item['room_category']}={item['action']}({item['discount_pct']}%)，{item['assessment']}" for item in reviews)
    lesson = (
        f"{stay_date} {own_hotel_key} 调价复盘：展会={event_names}；最终真实销售率={_show_rate(sales_rate)}，"
        f"剩余房量={_show(factors['available_rooms'])}；{action_summary}。"
        "销售率和剩余房量是酒店日级结果，不能当作单房型因果结论；后续同星期、同展会、同房型继续验证。"
    )
    report = _json_safe({
        "hotel_key": own_hotel_key,
        "stay_date": stay_date,
        "weekday": _weekday(stay_date)[0],
        "actual_sales_rate": sales_rate if sales_rate is not None else "unknown",
        "remaining_rooms": factors["available_rooms"] if factors["available_rooms"] is not None else "unknown",
        "event_name": event_names,
        "event_coverage": coverage_events,
        "reviews": reviews,
        "lesson_draft": lesson,
        "lesson_metadata": {
            "tenant": "hotel_ops",
            "memory_type": "pricing_lesson",
            "hotel_key": own_hotel_key,
            "hotel_short_name": _hotel_short_name(own_hotel_key),
            "rule_type": "pricing_lesson",
            "scope": "hotel",
            "priority": 50,
            "stay_date": stay_date,
            "weekday": _weekday(stay_date)[0],
            "source": "postmortem",
            "confidence": "medium",
            "valid_from": stay_date,
            "valid_to": None,
            "event_name": event_names,
        },
    })
    output = Path(output_path or f"output/latest/price_decision_review_{stay_date}.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report["output"] = str(output)
    return report


def _hotel_short_name(hotel_key: str) -> str:
    try:
        return next((hotel.name for hotel in load_hotels("hotels.json") if hotel.key == hotel_key), hotel_key)
    except Exception:
        return hotel_key


def _load_published_business_comparison(own_hotel_key: str, stay_date: str, expected_run_id: str | None) -> dict[str, Any] | None:
    manifest_path = Path("output/latest_business_manifest.json")
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload_path = Path(str(manifest["directory"])) / "price_compare.json"
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if payload.get("own_hotel_key") != own_hotel_key or str(payload.get("stay_date") or "") not in {"", stay_date}:
        return None
    if expected_run_id and payload.get("source_run_id") != expected_run_id:
        return None
    if any(payload.get(key) != manifest.get(key) for key in ("comparison_batch_id", "source_run_id", "comparison_version")):
        return None
    return payload


def _build_decision_room_variants(payload: dict[str, Any], own_hotel_key: str, stay_date: str, expected_run_id: str | None) -> tuple[list[dict[str, Any]], str]:
    if not isinstance(payload, dict):
        return [], "missing"
    if payload.get("own_hotel_key") != own_hotel_key or (expected_run_id and payload.get("source_run_id") != expected_run_id):
        return [], "batch_mismatch"
    rows = [row for row in payload.get("summary_by_room_type", []) if str(row.get("stay_date")) == stay_date]
    if not rows:
        return [], "missing"
    return rows, "ok"


def _generate(
    stay_date: str | None,
    days: int,
    own_hotel_key: str,
    config_path: str,
    run_id: str | None = None,
    business_comparison: dict[str, Any] | None = None,
    persist: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, datetime]:
    if not own_hotel_key.strip():
        raise DBWriterError("own_hotel_key 不能为空")
    try:
        stay_dates = resolve_stay_dates(stay_date, days)
    except ValueError as exc:
        raise DBWriterError(str(exc)) from exc
    if psycopg is None or Jsonb is None:
        raise DBWriterError("缺少 psycopg 依赖，请执行 pip install -r requirements.txt")
    hotels = load_hotels(config_path)
    fallback_competitors = {hotel.key for hotel in hotels if hotel.role == "competitor"}
    own_hotel_name = next((hotel.name for hotel in hotels if hotel.key == own_hotel_key), own_hotel_key)

    connection = None
    source_run_id = run_id
    run_id = run_id or uuid4().hex
    decision_at = datetime.now().astimezone()
    router_status = memory_router_status()
    try:
        connection = psycopg.connect(_load_database_url(), row_factory=psycopg.rows.dict_row)
        decisions: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT competitor_hotel_key
                FROM hotel_competitors
                WHERE own_hotel_key = %s AND is_active = TRUE
                ORDER BY priority, competitor_hotel_key
                """,
                (own_hotel_key,),
            )
            competitor_hotel_keys = {row["competitor_hotel_key"] for row in cursor.fetchall()} or fallback_competitors
            for current_date in stay_dates:
                exhibitions = _fetch_hotel_exhibitions(cursor, own_hotel_key, current_date)
                cursor.execute(FETCH_STATUS_SQL, (own_hotel_key, current_date))
                status = cursor.fetchone()
                comparison = business_comparison or _load_published_business_comparison(own_hotel_key, current_date, source_run_id)
                variants, comparison_source_status = _build_decision_room_variants(comparison or {}, own_hotel_key, current_date, source_run_id)
                day_decisions, day_skipped = _build_variant_decisions(
                    stay_date=current_date,
                    own_hotel_key=own_hotel_key,
                    status=status,
                    variants=variants,
                    comparison_source_status=comparison_source_status,
                    exhibitions=exhibitions,
                    router_status=router_status,
                    cursor=cursor,
                    hotel_name=own_hotel_name,
                )
                for decision in day_decisions:
                    decision["run_id"] = run_id
                    decision["decision_at"] = decision_at
                    if persist:
                        cursor.execute(INSERT_DECISION_SQL, _decision_values(decision))
                decisions.extend(day_decisions)
                skipped.extend(day_skipped)
        if persist:
            connection.commit()
        else:
            connection.rollback()
        return decisions, skipped, run_id, decision_at
    except DBWriterError:
        if connection is not None:
            connection.rollback()
        raise
    except Exception as exc:
        if connection is not None:
            connection.rollback()
        raise DBWriterError(f"调价建议数据库读取或写入失败: {type(exc).__name__}: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()


def load_latest_price_decisions(
    hotel_key: str | None = None,
    start_date: str | None = None,
    days: int | None = None,
) -> list[dict[str, Any]]:
    """Load the newest decision for each stay-date, hotel, and room category."""
    if psycopg is None:
        raise DBWriterError("缺少 psycopg 依赖，请执行 pip install -r requirements.txt")

    clauses: list[str] = []
    values: list[Any] = []
    if hotel_key:
        clauses.append("COALESCE(own_hotel_key, hotel_key) = %s")
        values.append(hotel_key)
    if start_date is not None or days is not None:
        try:
            stay_dates = resolve_stay_dates(start_date, days or 1)
        except ValueError as exc:
            raise DBWriterError(str(exc)) from exc
        clauses.append("stay_date BETWEEN %s AND %s")
        values.extend((stay_dates[0], stay_dates[-1]))

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    query = f"""
    SELECT DISTINCT ON (stay_date, COALESCE(own_hotel_key, hotel_key), COALESCE(normalized_room_type, room_category), COALESCE(core_feature_signature, '')) *
    FROM price_decisions
    {where}
    ORDER BY stay_date, COALESCE(own_hotel_key, hotel_key), COALESCE(normalized_room_type, room_category), COALESCE(core_feature_signature, ''), decision_at DESC, id DESC
    """
    try:
        with psycopg.connect(_load_database_url(), row_factory=psycopg.rows.dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, values)
                return list(cursor.fetchall())
    except DBWriterError:
        raise
    except Exception as exc:
        raise DBWriterError("读取最新调价建议失败") from exc


def _build_stay_decisions(
    *,
    stay_date: str,
    own_hotel_key: str,
    competitor_hotel_keys: set[str],
    status: dict[str, Any] | None,
    price_rows: list[dict[str, Any]],
    exhibitions: list[dict[str, Any]],
    memory_context: list[dict[str, Any]] | None = None,
    router_status: dict[str, Any] | None = None,
    cursor: Any | None = None,
    hotel_name: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    exhibitions = _json_safe(exhibitions)
    active_exhibitions, coverage_exhibitions = partition_events_for_day(stay_date, exhibitions)
    active_exhibitions = _json_safe(active_exhibitions)
    coverage_exhibitions = _json_safe(coverage_exhibitions)
    router_status = _json_safe(router_status or memory_router_status())
    own_by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    competitor_by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in _select_latest_prices(price_rows):
        if row["hotel_key"] == own_hotel_key:
            target = own_by_category
        elif row["hotel_key"] in competitor_hotel_keys:
            target = competitor_by_category
        else:
            continue
        target[row["room_category"]].append(row)

    own_lowest = {category: _minimum_row(rows) for category, rows in own_by_category.items()}
    matches = match_competitor_candidates(
        {category: row for category, row in own_lowest.items() if row is not None},
        [row for rows in competitor_by_category.values() for row in rows],
    )
    factors = _status_factors(status)
    event_demand_level = _event_demand_level(active_exhibitions)
    weekday, weekday_cn = _weekday(stay_date)
    decisions: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for category in sorted(own_by_category):
        own_rows = own_by_category.get(category, [])
        match = matches[category]
        competitor_rows = match["competitor_price_rows"]
        own_price = _minimum_price(own_rows)
        competitor_price = match["competitor_min_price"]
        # Pricing is allowed only by the deterministic business matcher.  A
        # same-capacity premium mismatch remains market context, never a price signal.
        decision_competitor_price = competitor_price if match.get("can_drive_pricing") is True else None
        competitor_context = match
        gap_pct = _price_gap_pct(own_price, competitor_price)
        historical_cases = (
            _fetch_historical_similar_cases(
                cursor,
                own_hotel_key,
                stay_date,
                category,
                weekday,
                event_demand_level,
                gap_pct,
                competitor_hotel_keys,
            )
            if cursor is not None and own_price is not None
            else []
        )
        memory_query = _build_memory_query(
            hotel_key=own_hotel_key,
            hotel_name=hotel_name or own_hotel_key,
            stay_date=stay_date,
            weekday=weekday,
            weekday_cn=weekday_cn,
            exhibitions=active_exhibitions,
            room_category=category,
            own_price=own_price,
            competitor_price=competitor_price,
            sales_rate=factors["decision_rate"],
            remaining_rooms=factors["available_rooms"],
            booking_pace=factors["booking_pace"],
            historical_cases=historical_cases,
        )
        room_memory = (
            _json_safe(memory_context)
            if memory_context is not None
            else _json_safe(recall_for_pricing_decision(own_hotel_key, stay_date, category, memory_query, 8))
        )
        decision = _build_decision(
            stay_date=stay_date,
            room_category=category,
            occupancy_rate=factors["occupancy_rate"],
            actual_sales_rate=factors["actual_sales_rate"],
            actual_sold_rooms=factors["actual_sold_rooms"],
            pending_arrival_rooms=factors["pending_arrival_rooms"],
            available_rooms=factors["available_rooms"],
            decision_rate=factors["decision_rate"],
            decision_rate_source=factors["decision_rate_source"],
            own_current_price=own_price,
            competitor_min_price=decision_competitor_price,
            booking_pace=factors["booking_pace"],
            weekday=weekday,
            event_demand_level=event_demand_level,
            memory_lessons=room_memory,
            historical_similar_cases=historical_cases,
        )
        _apply_exhibition_context(decision, active_exhibitions)
        decision.update({key: competitor_context[key] for key in (
            "competitor_min_price",
            "competitor_median_price",
            "competitor_max_price",
            "competitor_available_count",
            "competitor_hotel_count",
            "competitor_price_spread",
        )})
        decision["match_level"] = match["match_level"]
        decision["match_reason"] = match["match_reason"]
        decision["room_difference_note"] = match["room_difference_note"]
        decision["comparison_role"] = match.get("comparison_role")
        decision["can_drive_pricing"] = bool(match.get("can_drive_pricing"))
        decision["core_feature_signature"] = match.get("core_feature_signature")
        if match["match_level"] == "comparable":
            decision["reason"] += f"；相近可比：{match['room_difference_note']}"
            decision["reason_codes"].append("comparable_room_type")
        elif match["match_level"] == "benchmark":
            decision["reason"] = f"仅市场基准：{match['room_difference_note']}，不按此价机械跟价。{decision['reason']}"
            decision["reason_codes"].append("benchmark_only")
        decision["reason"] = _with_competitor_sample_note(decision["reason"], competitor_context)
        decision["hotel_key"] = own_hotel_key
        decision["exhibitions"] = active_exhibitions
        decision["exhibition_coverage"] = coverage_exhibitions
        decision["memory_query"] = memory_query
        decision["memory_context_used"] = len(room_memory)
        decision.update(router_status)
        decision["raw"] = _json_safe({
            "own_price_source": {"table": "ota_room_prices", "rows": own_rows},
            "competitor_price_source": {"table": "ota_room_prices", "rows": competitor_rows},
            "competitor_min_source": competitor_context["competitor_min_source"],
            "competitor_min_price": competitor_context["competitor_min_price"],
            "competitor_median_price": competitor_context["competitor_median_price"],
            "competitor_max_price": competitor_context["competitor_max_price"],
            "competitor_available_count": competitor_context["competitor_available_count"],
            "competitor_hotel_count": competitor_context["competitor_hotel_count"],
            "competitor_price_spread": competitor_context["competitor_price_spread"],
            "competitor_price_rows": competitor_context["competitor_price_rows"],
            "match_level": match["match_level"],
            "match_reason": match["match_reason"],
            "room_difference_note": match["room_difference_note"],
            "own_daily_status_source": status,
            "exhibition_source": {"table": "event_occurrences", "events": active_exhibitions},
            "exhibition_coverage_source": {"table": "event_occurrences", "events": coverage_exhibitions},
            "decision_factors": {key: decision.get(key) for key in (
                "stay_date", "weekday", "event_demand_level", "room_category", "normalized_room_type", "match_level",
                "own_price", "competitor_price", "price_gap", "price_gap_pct", "sales_rate",
                "remaining_rooms", "booking_pace", "historical_similar_cases", "memory_lessons",
                "action", "suggested_price", "discount_pct", "confidence", "reason_codes",
                "memory_summary", "historical_summary", "risk_note",
            )},
            "historical_similar_cases": historical_cases,
            "memory_context": room_memory,
            "memory_query": memory_query,
            "memory_context_used": len(room_memory),
            "memory_router": router_status,
            "decision_rule": decision["decision_rule"],
        })
        (decisions if decision.pop("persist") else skipped).append(decision)
    return decisions, skipped


def _build_variant_decisions(
    *,
    stay_date: str,
    own_hotel_key: str,
    status: dict[str, Any] | None,
    variants: list[dict[str, Any]],
    comparison_source_status: str,
    exhibitions: list[dict[str, Any]],
    router_status: dict[str, Any] | None = None,
    cursor: Any | None = None,
    hotel_name: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    active_exhibitions, coverage_exhibitions = partition_events_for_day(stay_date, _json_safe(exhibitions))
    factors = _status_factors(status)
    event_demand_level = _event_demand_level(active_exhibitions)
    weekday, weekday_cn = _weekday(stay_date)
    router_status = _json_safe(router_status or memory_router_status())
    decisions: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for variant in variants:
        variant_key = str(variant.get("variant_key") or "")
        category = str(variant.get("normalized_room_type") or "other")
        own_price = _number(variant.get("own_min_price"))
        selected = variant.get("selected_match") if isinstance(variant.get("selected_match"), dict) else None
        competitor_price = _number(selected.get("price")) if variant.get("can_drive_pricing") and selected else _number(variant.get("pricing_median_price") or variant.get("pricing_min_price")) if variant.get("can_drive_pricing") else None
        gap_pct = _price_gap_pct(own_price, competitor_price)
        historical_cases = _fetch_historical_similar_cases(cursor, own_hotel_key, stay_date, variant_key, weekday, event_demand_level, gap_pct, set()) if cursor is not None and own_price is not None else []
        memory_query = _build_memory_query(hotel_key=own_hotel_key, hotel_name=hotel_name or own_hotel_key, stay_date=stay_date, weekday=weekday, weekday_cn=weekday_cn, exhibitions=active_exhibitions, room_category=variant_key, own_price=own_price, competitor_price=competitor_price, sales_rate=factors["decision_rate"], remaining_rooms=factors["available_rooms"], booking_pace=factors["booking_pace"], historical_cases=historical_cases)
        room_memory = _json_safe(recall_for_pricing_decision(own_hotel_key, stay_date, variant_key, memory_query, 8))
        decision = _build_decision(stay_date=stay_date, room_category=category, occupancy_rate=factors["occupancy_rate"], actual_sales_rate=factors["actual_sales_rate"], actual_sold_rooms=factors["actual_sold_rooms"], pending_arrival_rooms=factors["pending_arrival_rooms"], available_rooms=factors["available_rooms"], decision_rate=factors["decision_rate"], decision_rate_source=factors["decision_rate_source"], own_current_price=own_price, competitor_min_price=competitor_price, booking_pace=factors["booking_pace"], weekday=weekday, event_demand_level=event_demand_level, memory_lessons=room_memory, historical_similar_cases=historical_cases)
        _apply_exhibition_context(decision, active_exhibitions)
        decision.update({key: variant.get(key) for key in ("match_level", "match_reason", "comparison_role", "can_drive_pricing", "core_feature_signature", "variant_key", "market_min_price", "market_median_price", "market_max_price", "pricing_min_price", "pricing_median_price", "pricing_max_price")})
        decision["comparison_source_status"] = comparison_source_status
        decision["room_difference_note"] = variant.get("room_difference_note")
        decision["hotel_key"] = own_hotel_key
        decision["exhibitions"] = active_exhibitions
        decision["exhibition_coverage"] = coverage_exhibitions
        decision.update(router_status)
        decision["raw"] = _json_safe({"business_comparison": variant, "comparison_source_status": comparison_source_status, "own_daily_status_source": status, "exhibition_source": {"table": "event_occurrences", "events": active_exhibitions}, "exhibition_coverage_source": {"table": "event_occurrences", "events": coverage_exhibitions}, "historical_similar_cases": historical_cases, "memory_context": room_memory, "memory_query": memory_query})
        (decisions if decision.pop("persist") else skipped).append(decision)
    if not variants and comparison_source_status in {"missing", "batch_mismatch"}:
        skipped.append({"stay_date": stay_date, "comparison_source_status": comparison_source_status, "reason": "业务比较缺失或批次不一致，未使用竞品价格"})
    return decisions, skipped


def _select_latest_prices(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        hotel_key = str(row.get("hotel_key") or "").strip()
        room_name = str(row.get("room_name") or "").strip()
        category = _category(row.get("room_category"), room_name)
        price = _number(row.get("price"))
        stay_date = str(row.get("stay_date") or "")
        if not hotel_key or price is None or price <= 0 or not stay_date:
            continue
        normalized = {
            "hotel_key": hotel_key,
            "hotel_name": str(row.get("hotel_name") or ""),
            "room_category": category,
            "room_name": room_name,
            "stay_date": row["stay_date"],
            "price": price,
            "collected_at": row.get("collected_at"),
        }
        key = (hotel_key, category, stay_date)
        if (
            key not in selected
            or _time_key(normalized["collected_at"]) > _time_key(selected[key]["collected_at"])
            or (
                _time_key(normalized["collected_at"]) == _time_key(selected[key]["collected_at"])
                and normalized["price"] < selected[key]["price"]
            )
        ):
            selected[key] = normalized
    return list(selected.values())


def _competitor_context(rows: list[dict[str, Any]]) -> dict[str, Any]:
    prices = sorted(float(row["price"]) for row in rows if _number(row.get("price")) is not None)
    hotel_keys = {str(row.get("hotel_key") or "") for row in rows if row.get("hotel_key")}
    return {
        "competitor_min_price": prices[0] if prices else None,
        "competitor_median_price": median(prices) if prices else None,
        "competitor_max_price": prices[-1] if prices else None,
        "competitor_available_count": len(prices),
        "competitor_hotel_count": len(hotel_keys),
        "competitor_price_spread": round(prices[-1] - prices[0], 2) if prices else None,
        "competitor_price_rows": _json_safe(rows),
    }


def _with_competitor_sample_note(reason: str, context: dict[str, Any]) -> str:
    count = int(context.get("competitor_available_count") or 0)
    if count == 0:
        note = "无竞品有价，按 PMS、展会、周末和历史经验判断。"
    elif count == 1:
        note = "仅 1 家竞品有价，样本较少，谨慎参考。"
    else:
        note = "2 家以上竞品有价，可参考中位价。"
    return f"{reason} {note}".strip()


def _category(room_category: Any, room_name: str) -> str:
    category = str(room_category or "").strip()
    if category:
        return category
    for token in ("大床", "双床", "电竞", "家庭"):
        if token in room_name:
            return token
    return "其他"


def _build_decision(
    *,
    stay_date: str,
    room_category: str,
    occupancy_rate: float | None,
    actual_sales_rate: float | None,
    actual_sold_rooms: float | None,
    pending_arrival_rooms: float | None,
    available_rooms: float | None,
    own_current_price: float | None,
    competitor_min_price: float | None,
    decision_rate: float | None = None,
    decision_rate_source: str = "unknown",
    booking_pace: Any = "unknown",
    weekday: str | None = None,
    event_demand_level: str = "unknown",
    memory_lessons: list[dict[str, Any]] | None = None,
    historical_similar_cases: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    weekday = weekday or _weekday(stay_date)[0]
    memory_lessons = _json_safe(memory_lessons or [])
    historical_similar_cases = _json_safe(historical_similar_cases or [])
    rate = _occupancy_ratio(decision_rate if decision_rate is not None else actual_sales_rate if actual_sales_rate is not None else occupancy_rate)
    price_gap = None if own_current_price is None or competitor_min_price is None else own_current_price - competitor_min_price
    gap_pct = _price_gap_pct(own_current_price, competitor_min_price)
    result: dict[str, Any] = {
        "stay_date": stay_date,
        "weekday": weekday,
        "event_demand_level": event_demand_level or "unknown",
        "room_category": room_category,
        "normalized_room_type": room_category,
        "occupancy_rate": occupancy_rate,
        "actual_sales_rate": actual_sales_rate,
        "decision_rate": decision_rate if decision_rate is not None else rate,
        "decision_rate_source": decision_rate_source if decision_rate is not None else ("order_api" if actual_sales_rate is not None else "occupancy_rate"),
        "sales_rate": rate if rate is not None else "unknown",
        "actual_sold_rooms": actual_sold_rooms,
        "pending_arrival_rooms": pending_arrival_rooms,
        "today_available": available_rooms,
        "available_rooms": available_rooms,
        "remaining_rooms": available_rooms if available_rooms is not None else "unknown",
        "booking_pace": booking_pace if booking_pace not in (None, "") else "unknown",
        "own_current_price": own_current_price,
        "own_price": own_current_price,
        "competitor_min_price": competitor_min_price,
        "competitor_price": competitor_min_price,
        "price_gap": price_gap,
        "price_gap_pct": gap_pct,
        "historical_similar_cases": historical_similar_cases,
        "memory_lessons": memory_lessons,
        "recommended_price": None,
        "recommended_action": "",
        "suggested_price": None,
        "action": "",
        "discount_pct": 0.0,
        "confidence": "low",
        "reason_codes": [],
        "memory_summary": _memory_summary(memory_lessons),
        "historical_summary": _historical_summary(historical_similar_cases),
        "risk_note": "",
        "reason": "",
        "decision_rule": "",
        "persist": False,
    }
    if own_current_price is None:
        result.update(recommended_action="不生成", action="不生成", reason="缺少自有酒店价格", decision_rule="missing_own_price", risk_note="缺少自有 OTA 价格，不能生成可执行价格")
        return result
    result["persist"] = True
    if competitor_min_price is None:
        _set_action(result, "维持", own_current_price, 0, "missing_competitor_keep", "缺少同类竞品价格，维持当前价并等待下一批 OTA 数据", ["missing_competitor_price"])
        result["risk_note"] = "缺少同房型竞品价格；booking_pace=" + str(result["booking_pace"])
        return result
    high_event = str(event_demand_level).lower() in {"high", "very_high", "高", "很高"}
    memory_signals = _memory_signals(memory_lessons, room_category)
    if rate is None:
        if own_current_price < competitor_min_price * 0.90:
            _set_action(result, "谨慎上调", min(competitor_min_price, own_current_price * 1.05), 0, "missing_occupancy_below_competitor", "缺少销售率，但当前价明显低于竞品，仅建议谨慎上调 5% 以内", ["sales_rate_unknown", "own_price_below_competitor"])
        elif own_current_price > competitor_min_price * 1.10:
            _set_action(result, "维持", own_current_price, 0, "missing_occupancy_above_competitor", "缺少销售率，不能仅凭价差降价；先维持并检查房态、流量和最新经营数据", ["sales_rate_unknown", "own_price_above_competitor"])
        else:
            _set_action(result, "维持", own_current_price, 0, "missing_occupancy_keep", "缺少销售率且价差不显著，维持当前价", ["sales_rate_unknown", "price_gap_not_significant"])
    elif actual_sales_rate is not None and rate >= 0.90 and _low_inventory(available_rooms, 5) and pending_arrival_rooms is not None and pending_arrival_rooms > 0:
        if own_current_price <= competitor_min_price * 1.05:
            _set_action(result, "谨慎上调", min(competitor_min_price + 10, own_current_price * 1.08), 0, "pending_arrivals_near_full", "已预订未入住较多，真实销售率高且只剩少量可售房，按接近满房处理，不降价", ["high_sales_rate", "low_inventory", "pending_arrivals"])
        else:
            _set_action(result, "维持", own_current_price, 0, "pending_arrivals_near_full", "已预订未入住较多，真实销售率高且只剩少量可售房，按接近满房处理，不降价", ["high_sales_rate", "low_inventory", "pending_arrivals"])
    elif rate >= 0.95 and _low_inventory(available_rooms, 5) and own_current_price <= competitor_min_price * 1.05:
        _set_action(result, "谨慎上调", min(competitor_min_price + 10, own_current_price * 1.08), 0, "very_high_occupancy_low_inventory", "真实销售率高且库存低，自有价格未明显高于竞品，谨慎上调", ["very_high_sales_rate", "low_inventory"])
    elif rate >= 0.95 and _low_inventory(available_rooms, 5):
        _set_action(result, "维持", own_current_price, 0, "very_high_occupancy_above_competitor", "真实销售率高且库存低，但当前价已高于竞品，不继续上调", ["very_high_sales_rate", "low_inventory", "own_price_above_competitor"])
    elif rate >= 0.90 and _low_inventory(available_rooms, 10):
        suggested = min(competitor_min_price, own_current_price * 1.05) if own_current_price < competitor_min_price * 0.97 else own_current_price
        action = "谨慎上调" if suggested > own_current_price else "维持"
        _set_action(result, action, suggested, 0, "high_occupancy_low_inventory", "真实销售率较高且库存不多，保护库存并避免降价", ["high_sales_rate", "low_inventory"])
    elif high_event and rate >= 0.80:
        _set_action(result, "维持", own_current_price, 0, "high_event_sales_keep", "高需求展会日且真实销售率已达到 80%，维持当前价，不因单一竞品价差降价", ["high_event_demand", "healthy_sales_rate"])
    elif rate >= 0.70 and own_current_price < competitor_min_price * 0.95:
        _set_action(result, "谨慎上调", min(competitor_min_price - 1, own_current_price * 1.05), 0, "medium_occupancy_below_competitor", "真实销售率较好且当前价明显低于竞品，谨慎贴近竞品", ["healthy_sales_rate", "own_price_below_competitor"])
    elif rate >= 0.70:
        discount = 3 if gap_pct is not None and gap_pct >= 20 and not high_event else 0
        if discount:
            _set_action(result, "小降", own_current_price * 0.97, 3, "medium_sales_large_gap_small_cut", "销售率尚可但价差超过 20%，只小降 3%，避免直接统一 9 折", ["healthy_sales_rate", "large_price_gap", "differentiated_pricing"])
        else:
            _set_action(result, "维持", own_current_price, 0, "medium_sales_keep", "销售率尚可，价差或展会需求不足以支持降价，维持观察", ["healthy_sales_rate"])
    else:
        discount = _discount_for_gap(gap_pct)
        reason_codes = ["low_sales_rate", "differentiated_pricing"]
        modifiers: list[str] = []
        if discount and high_event:
            protected = _lower_discount_tier(discount)
            if protected != discount:
                discount = protected
                reason_codes.append("high_event_price_protection")
                modifiers.append("高需求展会将降幅下调一级")
        if discount and memory_signals["event_premium"] and high_event:
            protected = _lower_discount_tier(discount)
            if protected != discount:
                discount = protected
                reason_codes.append("memory_event_premium")
                modifiers.append("Mnemosyne 展会溢价经验再次收窄降幅")
        if discount and memory_signals["hold"] and (rate >= 0.55 or high_event):
            discount = 0
            reason_codes.append("memory_hold_rule")
            modifiers.append("Mnemosyne 明确经验要求当前条件下不降价")
        elif discount and memory_signals["discount_pct"] is not None:
            discount = min(25, max(3, int(memory_signals["discount_pct"])))
            reason_codes.append("memory_explicit_discount_tier")
            modifiers.append("采用 Mnemosyne 中该房型的明确降价档位")
        if discount and _history_protects_price(historical_similar_cases) and rate >= 0.60:
            protected = _lower_discount_tier(discount)
            if protected != discount:
                discount = protected
                reason_codes.append("historical_price_protection")
                modifiers.append("相似历史日维持/上调后最终销售较好，降幅下调一级")
        if memory_signals["differentiated"]:
            reason_codes.append("memory_differentiated_pricing")
            modifiers.append("Mnemosyne 要求按房型和价差差异化，禁止全线 9 折")
        if discount:
            action = _discount_action(discount)
            reason = f"真实销售率偏低，当前价高于竞品 {gap_pct:.1f}%，按价差采用{action} {discount}%"
            if discount == 10:
                reason += "；选择 9 折是因价差落在中降区间，不选 85 折是展会/历史风险仍需保护价格，不选 95 折或维持是当前价差与低销售率已超过小降阈值"
            elif action == "小降":
                reason += "；不采用 9 折或 85 折，因为当前价差只需要 3%~5% 修正"
            elif action == "强降":
                reason += "；不采用 9 折、95 折或维持，因为低销售率与超大价差同时存在"
            if modifiers:
                reason += "；" + "；".join(modifiers)
            _set_action(result, action, own_current_price * (1 - discount / 100), discount, f"low_sales_gap_{action}", reason, reason_codes)
        else:
            reason = "真实销售率偏低，但价格已有优势或经验/展会因素要求保护价格，维持并检查流量、房态和展示"
            if modifiers:
                reason += "；" + "；".join(modifiers)
            _set_action(result, "维持", own_current_price, 0, "low_sales_keep", reason, reason_codes + ["price_protected"])
    if memory_lessons:
        result["reason_codes"] = list(dict.fromkeys([*result["reason_codes"], "memory_lessons_consulted"]))
    if memory_signals["differentiated"] and "memory_differentiated_pricing" not in result["reason_codes"]:
        result["reason_codes"].append("memory_differentiated_pricing")
        result["reason"] += "；Mnemosyne 人工经验要求按房型和竞品价差差异化，禁止全线统一 9 折"
    rate_label = _decision_rate_label(result.get("decision_rate_source"))
    if rate_label != "真实销售率":
        result["reason"] = result["reason"].replace("真实销售率", rate_label)
    result["confidence"] = _decision_confidence(result)
    risks = ["销售率与剩余房量为酒店日级指标，不代表单房型独立销量"]
    if result["booking_pace"] == "unknown":
        risks.append("booking_pace 当前无字段，按 unknown 处理")
    if not historical_similar_cases:
        risks.append("未找到足够的同房型历史相似案例")
    result["risk_note"] = "；".join(risks)
    return result


def _status_factors(status: dict[str, Any] | None) -> dict[str, Any]:
    raw_status = status.get("raw") if status and isinstance(status.get("raw"), dict) else {}
    summary = raw_status.get("summary") if isinstance(raw_status.get("summary"), dict) else {}
    available_rooms = _number(summary.get("today_available"))
    available_rooms = _number(status.get("available_rooms")) if available_rooms is None and status else available_rooms
    actual_sold_rooms = None
    actual_sales_rate = None
    if raw_status.get("sales_rate_source") == "order_api":
        actual_sold_rooms = _number(summary.get("actual_sold_rooms"))
        actual_sales_rate = _number(summary.get("actual_sales_rate"))
    room_total = _number(status.get("room_total")) if status else None
    pending_arrival_rooms = _number(summary.get("pending_arrival_rooms"))
    if pending_arrival_rooms is None and actual_sold_rooms is not None and status:
        occupied_rooms = _number(status.get("occupied_rooms"))
        pending_arrival_rooms = actual_sold_rooms - occupied_rooms if occupied_rooms is not None else None
    inventory_occupied_rate = _number(summary.get("inventory_occupied_rate"))
    if inventory_occupied_rate is None and status and room_total:
        inventory_occupied_rate = _number(status.get("sold_rooms")) / room_total
    if actual_sales_rate is not None:
        decision_rate = actual_sales_rate
        decision_rate_source = "order_api"
    elif inventory_occupied_rate is not None:
        decision_rate = inventory_occupied_rate
        decision_rate_source = "inventory_estimate"
    else:
        decision_rate = _number(status.get("occupancy_rate")) if status else None
        decision_rate_source = "occupancy_rate" if decision_rate is not None else "unknown"
    booking_pace = summary.get("booking_pace", status.get("booking_pace") if status else None)
    return {
        "occupancy_rate": _number(status.get("occupancy_rate")) if status else None,
        "available_rooms": available_rooms,
        "actual_sold_rooms": actual_sold_rooms,
        "actual_sales_rate": actual_sales_rate,
        "pending_arrival_rooms": pending_arrival_rooms,
        "inventory_occupied_rate": inventory_occupied_rate,
        "decision_rate": decision_rate,
        "decision_rate_source": decision_rate_source,
        "booking_pace": booking_pace if booking_pace not in (None, "") else "unknown",
    }


def _weekday(stay_date: str | date) -> tuple[str, str]:
    value = stay_date if isinstance(stay_date, date) else date.fromisoformat(str(stay_date))
    return value.strftime("%A"), WEEKDAY_CN[value.weekday()]


def _event_demand_level(exhibitions: list[dict[str, Any]]) -> str:
    ranking = {"unknown": 0, "low": 1, "medium": 2, "high": 3, "very_high": 4, "低": 1, "中": 2, "高": 3, "很高": 4}
    levels = [str(event.get("demand_level") or "unknown").strip() for event in exhibitions]
    return max(levels, key=lambda value: ranking.get(value.lower(), ranking.get(value, 0)), default="unknown")


def _price_gap_pct(own_price: float | None, competitor_price: float | None) -> float | None:
    if own_price is None or competitor_price is None or competitor_price <= 0:
        return None
    return round((own_price - competitor_price) / competitor_price * 100, 2)


def _set_action(
    result: dict[str, Any],
    action: str,
    suggested_price: float,
    discount_pct: float,
    rule: str,
    reason: str,
    reason_codes: list[str],
) -> None:
    if discount_pct > MAX_MANUAL_PRICE_REDUCTION_PCT:
        original_discount_pct = discount_pct
        current_price = _number(result.get("own_current_price"))
        action = "小降"
        discount_pct = MAX_MANUAL_PRICE_REDUCTION_PCT
        if current_price is not None:
            suggested_price = max(float(suggested_price), current_price * (1 - discount_pct / 100))
        gap_pct = _number(result.get("price_gap_pct"))
        gap_note = f"当前价高于竞品 {gap_pct:.1f}%；" if gap_pct is not None and gap_pct > 0 else ""
        reason = (
            f"{gap_note}原始降幅{original_discount_pct:g}%超过单次{discount_pct:g}%上限，"
            f"已收敛为{discount_pct:g}%试探性降幅，请人工确认"
        )
        rule = f"{rule}_capped_to_{discount_pct:g}pct"
        reason_codes = [*reason_codes, "discount_capped_at_5pct", "manual_review_required"]
    suggested_price = round(float(suggested_price), 2)
    result.update(
        action=action,
        recommended_action=action,
        suggested_price=suggested_price,
        recommended_price=suggested_price,
        discount_pct=round(float(discount_pct), 2),
        decision_rule=rule,
        reason=reason,
        reason_codes=list(dict.fromkeys(reason_codes)),
    )


def _discount_for_gap(gap_pct: float | None) -> int:
    if gap_pct is None or gap_pct <= 0:
        return 0
    if gap_pct <= 5:
        return 3
    if gap_pct <= 12:
        return 5
    if gap_pct <= 25:
        return 8
    if gap_pct <= 35:
        return 10
    if gap_pct <= 50:
        return 12
    if gap_pct <= 75:
        return 15
    if gap_pct <= 100:
        return 20
    return 25


def _discount_action(discount_pct: int) -> str:
    if discount_pct <= 5:
        return "小降"
    if discount_pct <= 12:
        return "中降"
    return "强降"


def _lower_discount_tier(discount_pct: int) -> int:
    tiers = (0, 3, 5, 8, 10, 12, 15, 20, 25)
    index = tiers.index(discount_pct) if discount_pct in tiers else next((i for i, value in enumerate(tiers) if value >= discount_pct), len(tiers) - 1)
    return tiers[max(0, index - 1)]


def _memory_signals(memories: list[dict[str, Any]], room_category: str) -> dict[str, Any]:
    signals: dict[str, Any] = {"differentiated": False, "event_premium": False, "hold": False, "discount_pct": None}
    for item in memories:
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        lesson_room = str(metadata.get("room_category") or "").strip()
        if lesson_room and lesson_room != room_category:
            continue
        content = str(item.get("content") or "")
        if "不要全线9折" in content or "不能全线9折" in content or "差异化调价" in content:
            signals["differentiated"] = True
        if "展会溢价" in content or ("高需求展会" in content and "溢价" in content):
            signals["event_premium"] = True
        if any(token in content for token in ("不降价", "建议维持", "应维持", "维持当前价")):
            signals["hold"] = True
        if "强降" in content:
            signals["discount_pct"] = 20
        elif "中降" in content:
            signals["discount_pct"] = 10
        elif "小降" in content:
            signals["discount_pct"] = 5
    return signals


def _history_protects_price(cases: list[dict[str, Any]]) -> bool:
    return any(
        _occupancy_ratio(_number(item.get("final_sales_rate"))) is not None
        and _occupancy_ratio(_number(item.get("final_sales_rate"))) >= 0.85
        and str(item.get("action") or "") in {"维持", "保持", "上调", "谨慎上调"}
        for item in cases
    )


def _decision_confidence(result: dict[str, Any]) -> str:
    if result.get("decision_rate_source") != "order_api":
        return "low"
    if result.get("sales_rate") == "unknown" or result.get("competitor_price") is None:
        return "low"
    if result.get("booking_pace") != "unknown" and result.get("historical_similar_cases") and result.get("memory_lessons"):
        return "high"
    return "medium"


def _decision_rate_label(source: Any) -> str:
    return {"order_api": "真实销售率", "inventory_estimate": "库存售卖率估算", "occupancy_rate": "当前在住率"}.get(str(source or ""), "销售率")
def _memory_summary(memories: list[dict[str, Any]]) -> str:
    contents = [str(item.get("content") or "").strip() for item in memories]
    return "；".join(content[:160] for content in contents if content) or "未检索到可用 Mnemosyne 经验"


def _historical_summary(cases: list[dict[str, Any]]) -> str:
    if not cases:
        return "未找到同房型历史相似案例"
    return "；".join(
        f"{item['stay_date']} {item.get('weekday_cn') or item.get('weekday')} {item.get('action') or '未知动作'}，最终销售率 {_show_rate(item.get('final_sales_rate'))}，价差 {_show(item.get('price_gap_pct'))}%"
        for item in cases[:3]
    )


def _build_memory_query(
    *,
    hotel_key: str,
    hotel_name: str,
    stay_date: str,
    weekday: str,
    weekday_cn: str,
    exhibitions: list[dict[str, Any]],
    room_category: str,
    own_price: float | None,
    competitor_price: float | None,
    sales_rate: float | None,
    remaining_rooms: float | None,
    booking_pace: Any,
    historical_cases: list[dict[str, Any]],
) -> str:
    event_names = "、".join(str(item.get("event_name") or "") for item in exhibitions if item.get("event_name")) or "无展会"
    demand_level = _event_demand_level(exhibitions)
    gap = None if own_price is None or competitor_price is None else own_price - competitor_price
    gap_pct = _price_gap_pct(own_price, competitor_price)
    return " ".join(
        (
            f"酒店 key={hotel_key} name={hotel_name}",
            f"日期={stay_date} 星期={weekday}/{weekday_cn}",
            f"展会名称={event_names} event_demand_level={demand_level}",
            f"房型分类 room_category normalized_room_type={room_category}",
            f"own_price={_show(own_price)} competitor_price={_show(competitor_price)} price_gap={_show(gap)} price_gap_pct={_show(gap_pct)}%",
            f"sales_rate={_show_rate(sales_rate)} remaining_rooms={_show(remaining_rooms)} booking_pace={booking_pace}",
            f"历史调价经验 historical_similar_cases={_historical_summary(historical_cases)}",
            "人工飞书分析 复盘结论 不要全线9折 不能全线9折 差异化调价 维持 小降 中降 强降 谨慎上调",
        )
    )


def _fetch_historical_similar_cases(
    cursor: Any,
    own_hotel_key: str,
    stay_date: str,
    room_category: str,
    weekday: str,
    event_demand_level: str,
    current_gap_pct: float | None,
    competitor_hotel_keys: set[str],
) -> list[dict[str, Any]]:
    cutoff = date.fromisoformat(stay_date)
    snapshots = load_fact_snapshot_rows(cursor, own_hotel_key, end_date=cutoff)
    structured_rows = fact_decision_rows(
        snapshots,
        end_date=cutoff,
        room_category=room_category,
        limit=30,
    )
    structured_keys = {(str(row.get("stay_date")), str(row.get("room_category") or "")) for row in structured_rows}
    legacy_rows: list[dict[str, Any]] = []
    if len(structured_rows) < 30:
        cursor.execute(FETCH_HISTORICAL_DECISIONS_SQL, (own_hotel_key, stay_date, room_category))
        legacy_rows = [dict(row) for row in cursor.fetchall()]
    rows = structured_rows + [
        row for row in legacy_rows
        if (str(row.get("stay_date")), str(row.get("room_category") or "")) not in structured_keys
    ]
    ranked: list[tuple[int, str, dict[str, Any]]] = []
    for row in rows:
        item = dict(row)
        historical_weekday, historical_weekday_cn = _weekday(item["stay_date"])
        status = {
            "room_total": item.get("room_total"),
            "sold_rooms": item.get("sold_rooms"),
            "available_rooms": item.get("available_rooms"),
            "occupancy_rate": item.get("occupancy_rate"),
            "raw": item.get("status_raw") if isinstance(item.get("status_raw"), dict) else {},
        }
        status_factors = _status_factors(status)
        historical_gap_pct = _price_gap_pct(_number(item.get("own_price")), _number(item.get("competitor_min_price")))
        historical_event = _decision_event_level(item.get("raw"))
        score = 5 if historical_weekday == weekday else 0
        if event_demand_level != "unknown" and historical_event == event_demand_level:
            score += 3
        if current_gap_pct is not None and historical_gap_pct is not None and abs(current_gap_pct - historical_gap_pct) <= 10:
            score += 2
        case = {
            "price_decision_id": item.get("id"),
            "stay_date": str(item["stay_date"]),
            "weekday": historical_weekday,
            "weekday_cn": historical_weekday_cn,
            "event_demand_level": historical_event,
            "room_category": room_category,
            "own_price": _number(item.get("own_price")),
            "competitor_price": _number(item.get("competitor_min_price")),
            "price_gap_pct": historical_gap_pct,
            "suggested_price": _number(item.get("suggested_price")),
            "action": item.get("action"),
            "final_sales_rate": status_factors["actual_sales_rate"],
            "final_remaining_rooms": status_factors["available_rooms"],
            "similarity_score": score,
            "source_refs": [
                item.get("_source_ref") or f"price_decisions:{item.get('id')}",
                f"hotel_daily_operating_metrics:{item['stay_date']}" if item.get("_source_ref") else f"own_daily_status:{item['stay_date']}",
            ],
        }
        ranked.append((score, str(item["stay_date"]), case))
    cases = [item for _, _, item in sorted(ranked, key=lambda value: (value[0], value[1]), reverse=True)[:5]]
    if not cases:
        return []
    dates = [date.fromisoformat(item["stay_date"]) for item in cases]
    hotel_keys = [own_hotel_key, *sorted(competitor_hotel_keys)]
    structured_ota_rows = fact_comparison_rows(
        snapshots,
        start_date=min(dates),
        end_date=max(dates),
        own_hotel_key=own_hotel_key,
        room_category=room_category,
    )
    ota_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    ota_source_dates: set[str] = set()
    for row in structured_ota_rows:
        source_key = str(row.get("source_hotel_key") or row.get("hotel_key") or "")
        if source_key not in hotel_keys:
            continue
        day = str(row["stay_date"])
        bucket = [item for item in ota_by_date[day] if item["hotel_key"] == source_key]
        price = _number(row.get("price"))
        if price is None:
            continue
        if bucket:
            bucket[0]["min_price"] = min(bucket[0]["min_price"], price)
            if str(row.get("collected_at") or "") > bucket[0]["latest_collected_at"]:
                bucket[0]["latest_collected_at"] = str(row.get("collected_at") or "")
        else:
            ota_by_date[day].append({
                "hotel_key": source_key,
                "min_price": price,
                "latest_collected_at": str(row.get("collected_at") or ""),
            })
        ota_source_dates.add(day)
    missing_structured_dates = [day for day in dates if str(day) not in ota_source_dates]
    if missing_structured_dates:
        cursor.execute(FETCH_HISTORICAL_OTA_SQL, (missing_structured_dates, hotel_keys, room_category))
        for row in cursor.fetchall():
            ota_by_date[str(row["stay_date"])].append({
                "hotel_key": row["source_hotel_key"],
                "min_price": _number(row["min_price"]),
                "latest_collected_at": str(row["latest_collected_at"]),
            })
    for item in cases:
        item["ota_price_refs"] = ota_by_date.get(item["stay_date"], [])
        if item["stay_date"] in ota_source_dates:
            item["source_refs"].append(f"weekly_reviews.fact_snapshot_json:comparisons:{item['stay_date']}:{room_category}")
        else:
            item["source_refs"].append(f"ota_room_prices:{item['stay_date']}:{room_category}")
    return cases


def _decision_event_level(raw: Any) -> str:
    if not isinstance(raw, dict):
        return "unknown"
    source = raw.get("exhibition_source") if isinstance(raw.get("exhibition_source"), dict) else {}
    events = source.get("events") if isinstance(source.get("events"), list) else []
    return _event_demand_level(events)


def _minimum_price(rows: list[dict[str, Any]]) -> float | None:
    row = _minimum_row(rows)
    return None if row is None else row["price"]


def _apply_exhibition_context(decision: dict[str, Any], exhibitions: list[dict[str, Any]]) -> None:
    if not exhibitions:
        return
    names = "、".join(str(event.get("event_name") or "") for event in exhibitions)
    decision["reason"] = f"{decision['reason']}；命中展会 {names}，需求等级 {decision.get('event_demand_level', 'unknown')} 已参与价格保护/上调判断"
    decision["decision_rule"] = f"{decision['decision_rule']}+event_demand"


def _minimum_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    return min(rows, key=lambda row: row["price"])


def _occupancy_ratio(value: float | None) -> float | None:
    if value is None:
        return None
    return value / 100 if value > 1 else value


def _low_inventory(value: float | None, limit: int) -> bool:
    return value is None or value <= limit


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _time_key(value: Any) -> str:
    return "" if value is None else str(value)


def _decision_values(decision: dict[str, Any]) -> tuple[Any, ...]:
    raw_payload = dict(decision.get("raw") or {})
    for key in ("match_level", "match_reason", "room_difference_note", "comparison_role", "can_drive_pricing", "variant_key"):
        if decision.get(key) is not None:
            raw_payload[key] = decision[key]
    raw = Jsonb(_json_safe(raw_payload))
    return (
        decision["run_id"], decision["decision_at"], decision["hotel_key"], decision["hotel_key"],
        decision["stay_date"], decision["room_category"], decision["room_category"], decision.get("core_feature_signature"), decision["occupancy_rate"],
        decision["own_current_price"], decision["own_current_price"], decision["competitor_min_price"],
        decision["recommended_price"], decision["recommended_price"], decision["recommended_action"],
        decision["recommended_action"], decision["confidence"], decision["reason"], raw, raw,
    )


def _fetch_hotel_exhibitions(cursor: Any, own_hotel_key: str, stay_date: str) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT e.venue, e.event_name, e.event_start_date, e.event_end_date,
               e.stay_start_date, e.stay_end_date, e.category, e.demand_level,
               r.impact_level AS hotel_impact_level
        FROM event_occurrences e
        JOIN hotel_venue_impact_rules r
          ON r.own_hotel_key = %s AND r.venue = e.venue
        WHERE e.is_business_active = TRUE
          AND e.canonical_status <> 'cancelled'
          AND COALESCE(e.stay_start_date, e.event_start_date) <= %s
          AND COALESCE(e.stay_end_date, e.event_end_date) >= %s
        ORDER BY COALESCE(e.stay_start_date, e.event_start_date), e.event_name
        """,
        (own_hotel_key, stay_date, stay_date),
    )
    events = list(cursor.fetchall())
    return [_with_hotel_event_impact(dict(event)) for event in events]


def _with_hotel_event_impact(event: dict[str, Any]) -> dict[str, Any]:
    global_level = _normalize_event_level(event.get("demand_level"))
    impact_level = str(event.get("hotel_impact_level") or "").strip().lower()
    effective_level = _effective_event_level(global_level, impact_level)
    event["global_event_demand_level"] = global_level
    event["hotel_impact_level"] = impact_level or "unknown"
    event["effective_event_level"] = effective_level
    event["demand_level"] = effective_level
    return _json_safe(event)


def _normalize_event_level(value: Any) -> str:
    text = str(value or "normal").strip().lower()
    return {"high": "high", "very_high": "high", "高": "high", "很高": "high", "medium": "medium", "中": "medium"}.get(text, "normal")


def _effective_event_level(global_level: str, hotel_impact_level: str) -> str:
    levels = ("normal", "medium", "high")
    score = {level: index for index, level in enumerate(levels)}[_normalize_event_level(global_level)]
    delta = {"high": 1, "medium": 0, "low": -1}.get(str(hotel_impact_level or "").lower(), 0)
    return levels[max(0, min(len(levels) - 1, score + delta))]


def _write_brief(
    decisions: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    output_path: str,
    run_id: str,
    decision_at: datetime,
) -> None:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    all_dates = sorted({item["stay_date"] for item in decisions + skipped})
    lines = ["# 调价建议", "", f"调价建议批次：run_id={run_id}", f"决策时间：{decision_at.isoformat(timespec='seconds')}", f"入住日期范围：{' 至 '.join(all_dates) if all_dates else '无'}"]
    for current_date in all_dates:
        lines.extend(["", f"## {current_date}"])
        day_items = [item for item in decisions + skipped if item["stay_date"] == current_date]
        exhibitions = day_items[0].get("exhibitions", []) if day_items else []
        coverage_events = day_items[0].get("exhibition_coverage", []) if day_items else []
        memory_context_used = max((int(item.get("memory_context_used") or 0) for item in day_items), default=0)
        router_status = day_items[0] if day_items else {}
        lines.extend([
            f"- memory_primary_backend={router_status.get('memory_primary_backend', 'staging')}",
            f"- memory_auxiliary_backend={router_status.get('memory_auxiliary_backend', 'none')}",
        ])
        lines.append(f"- 是否使用 Mnemosyne 经验：{'是' if memory_context_used else '否'}（{memory_context_used} 条）")
        if exhibitions:
            lines.append("- 命中展会：")
            for event in exhibitions:
                lines.append(
                    f"  - {event['event_name']}；展会 {event['event_start_date']} 至 {event['event_end_date']}；"
                    f"入住 {event.get('stay_start_date') or '缺失'} 至 {event.get('stay_end_date') or '缺失'}；"
                    f"需求 全局 {event.get('global_event_demand_level') or '缺失'} / 本店 {event.get('hotel_impact_level') or '缺失'} / 最终 {event.get('effective_event_level') or event.get('demand_level') or '缺失'}；类别 {event.get('category') or '缺失'}"
                )
        else:
            lines.append("- 当日无进行中展会")
        if coverage_events:
            lines.append("- 住宿覆盖信息（不参与展会需求、价格保护或上调判断）：")
            for event in coverage_events:
                lines.append(
                    f"  - {event['event_name']}；实际展期 {event['event_start_date']} 至 {event['event_end_date']}；"
                    f"当日状态 {event.get('event_phase_note') or '住宿覆盖信息'}"
                )
        for item in (item for item in decisions if item["stay_date"] == current_date):
            lines.extend([
                "",
                f"### {item['room_category']}",
                f"- 星期 / 展会需求：{item.get('weekday', 'unknown')} / {item.get('event_demand_level', 'unknown')}",
                f"- 当前在住率：{_show(item['occupancy_rate'])}",
                f"- 真实销售率：{_show_rate(item['actual_sales_rate'])}",
                f"- 今日已售 / 剩余房量：{_show(item['actual_sold_rooms'])} / {_show(item['available_rooms'])}",
                f"- booking_pace：{item.get('booking_pace', 'unknown')}",
                f"- 自有最低价 / 竞品最低价：{_show(item['own_current_price'])} / {_show(item['competitor_min_price'])}",
                f"- 价差 / 价差率：{_show(item.get('price_gap'))} / {_show(item.get('price_gap_pct'))}%",
                f"- action：{item['action']}",
                f"- suggested_price / discount_pct：{_show(item['suggested_price'])} / {_show(item['discount_pct'])}%",
                f"- confidence：{item['confidence']}",
                f"- reason_codes：{', '.join(item['reason_codes']) or '无'}",
                f"- 为什么这样调：{item['reason']}",
                f"- Mnemosyne 经验：{'已使用' if item.get('memory_context_used') else '未使用'}；{item.get('memory_summary')}",
                f"- 历史相似经验：{item.get('historical_summary')}",
                f"- 风险：{item.get('risk_note')}",
            ])
        for item in (item for item in skipped if item["stay_date"] == current_date):
            lines.extend(["", f"- 未生成 {item['room_category']} 调价建议：{item['reason']}"])
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _show(value: Any) -> str:
    if value is None:
        return "缺失"
    return f"{value:.2f}".rstrip("0").rstrip(".") if isinstance(value, float) else str(value)


def _show_rate(value: Any) -> str:
    number = _occupancy_ratio(_number(value))
    return "unknown" if number is None else f"{number * 100:.2f}%"


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value
