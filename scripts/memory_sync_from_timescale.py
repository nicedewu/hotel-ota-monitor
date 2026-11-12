#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db_writer import DBWriterError, _load_database_url, psycopg
from src.memory_provider import contains_sensitive_data, remember_hotel


DEFAULT_OUTPUT = Path("output/memory_staging/timescale_experience_candidates.jsonl")
WRITE_CONFIRMATION = "确认写入Mnemosyne"


def read_optional_rows(cursor: Any, table: str, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    cursor.execute("SELECT to_regclass(%s) AS table_name", (f"public.{table}",))
    found = cursor.fetchone()
    table_name = found.get("table_name") if isinstance(found, dict) else found[0]
    if table_name is None:
        print(f"跳过不存在的表: {table}")
        return []
    cursor.execute(query, params)
    return [dict(row) for row in cursor.fetchall()]


def fetch_source_rows(connection: Any, start_date: date) -> dict[str, list[dict[str, Any]]]:
    with connection.cursor() as cursor:
        statuses = read_optional_rows(
            cursor,
            "own_daily_status",
            """
            SELECT DISTINCT ON (hotel_key, stay_date)
                   hotel_key, stay_date, room_total, occupied_rooms, available_rooms,
                   sold_rooms, occupancy_rate, raw
            FROM own_daily_status
            WHERE stay_date BETWEEN %s AND CURRENT_DATE
            ORDER BY hotel_key, stay_date, collected_at DESC
            """,
            (start_date,),
        )
        prices = read_optional_rows(
            cursor,
            "ota_room_prices",
            """
            SELECT source_hotel_key AS hotel_key, stay_date,
                   MIN(price) AS min_price, MAX(price) AS max_price
            FROM ota_room_prices
            WHERE stay_date BETWEEN %s AND CURRENT_DATE AND available = TRUE AND price > 0
            GROUP BY source_hotel_key, stay_date
            ORDER BY stay_date DESC, source_hotel_key
            """,
            (start_date,),
        )
        decisions = read_optional_rows(
            cursor,
            "price_decisions",
            """
            SELECT COALESCE(own_hotel_key, hotel_key) AS hotel_key, stay_date,
                   COALESCE(normalized_room_type, room_category) AS room_type,
                   COALESCE(decision_action, recommended_action) AS decision,
                   COALESCE(current_price, own_current_price) AS current_price,
                   COALESCE(suggested_price, recommended_price) AS suggested_price,
                   reason, decision_at
            FROM price_decisions
            WHERE stay_date BETWEEN %s AND CURRENT_DATE
            ORDER BY decision_at DESC
            LIMIT 500
            """,
            (start_date,),
        )
        events = read_optional_rows(
            cursor,
            "event_occurrences",
            """
            SELECT event_name, event_start_date, event_end_date,
                   COALESCE(stay_start_date, event_start_date) AS stay_start_date,
                   COALESCE(stay_end_date, event_end_date) AS stay_end_date,
                   demand_level, category
            FROM event_occurrences
            WHERE is_business_active = TRUE
              AND canonical_status <> 'cancelled'
              AND COALESCE(stay_end_date, event_end_date) >= %s
              AND COALESCE(stay_start_date, event_start_date) <= CURRENT_DATE
            ORDER BY COALESCE(stay_start_date, event_start_date)
            """,
            (start_date,),
        )
        mappings = read_optional_rows(
            cursor,
            "hotel_competitors",
            """
            SELECT own_hotel_key, competitor_hotel_key
            FROM hotel_competitors
            WHERE is_active = TRUE
            ORDER BY priority, competitor_hotel_key
            """,
        )
    return {"statuses": statuses, "prices": prices, "decisions": decisions, "events": events, "mappings": mappings}


def extract_candidates(rows: dict[str, list[dict[str, Any]]], start_date: date) -> list[dict[str, Any]]:
    prices: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows["prices"]:
        prices[(str(row.get("hotel_key") or ""), _date_text(row.get("stay_date")))] = row

    competitors: dict[str, list[str]] = defaultdict(list)
    for row in rows["mappings"]:
        competitors[str(row["own_hotel_key"])].append(str(row["competitor_hotel_key"]))
    if not competitors:
        competitors["own_demo_a"].append("competitor_demo_a")

    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(hotel_key: str, memory_type: str, content: str, confidence: str, metadata: dict[str, Any]) -> None:
        key = (hotel_key, memory_type, content)
        if key in seen or contains_sensitive_data(content, metadata):
            return
        seen.add(key)
        candidates.append(
            {
                "scope": "hotel",
                "hotel_key": hotel_key,
                "memory_type": memory_type,
                "content": content,
                "source": "timescale",
                "confidence": confidence,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "metadata": _json_safe(metadata),
            }
        )

    for status in rows["statuses"]:
        hotel_key = str(status.get("hotel_key") or "")
        stay_date = _date_text(status.get("stay_date"))
        raw = status.get("raw") if isinstance(status.get("raw"), dict) else {}
        summary = raw.get("summary") if isinstance(raw.get("summary"), dict) else {}
        occupancy = _ratio(_number(status.get("occupancy_rate")))
        sold = _number(summary.get("actual_sold_rooms"))
        sold = _number(status.get("sold_rooms")) if sold is None else sold
        available = _number(summary.get("today_available"))
        available = _number(status.get("available_rooms")) if available is None else available
        total = _number(status.get("room_total"))
        sales_rate = _ratio(_number(summary.get("actual_sales_rate")))
        if sales_rate is None and sold is not None and total:
            sales_rate = sold / total
        own_price = prices.get((hotel_key, stay_date))
        metadata = {
            "stay_date": stay_date,
            "occupancy_rate": occupancy,
            "actual_sales_rate": sales_rate,
            "sold_count": sold,
            "available_count": available,
            "price_range": _price_range(own_price),
        }
        if sales_rate is not None and sales_rate >= 0.90:
            add(
                hotel_key,
                "pricing_case",
                f"{stay_date} 真实销售率达到 {sales_rate:.1%}，今日可售为 {_show_count(available)}；高销售率时应优先保护库存，不因当前在住率偏低直接降价。",
                "high",
                metadata,
            )
        if occupancy is not None and occupancy < 0.80 and available is not None and available <= 5:
            add(
                hotel_key,
                "pricing_case",
                f"{stay_date} PMS 当前在住率为 {occupancy:.1%}，但今日可售仅 {_show_count(available)}；当前在住率不等于真实销售率，应结合已售和待入住判断。",
                "high",
                metadata,
            )

        mapped_prices = [prices.get((key, stay_date)) for key in competitors.get(hotel_key, [])]
        mapped_prices = [row for row in mapped_prices if row is not None]
        competitor_min = min((_number(row.get("min_price")) for row in mapped_prices), default=None)
        own_min = _number(own_price.get("min_price")) if own_price else None
        if sales_rate is not None and sales_rate >= 0.90 and own_min is not None and competitor_min is not None and competitor_min < own_min:
            add(
                hotel_key,
                "pricing_case",
                f"{stay_date} 竞品最低价低于自有最低价，但真实销售率仍为 {sales_rate:.1%}；该案例不支持仅因竞品低价而跟随降价。",
                "high",
                {**metadata, "competitor_min_price": competitor_min},
            )
        missing = []
        if own_price is None:
            missing.append("自有 OTA 价格")
        if competitors.get(hotel_key) and not mapped_prices:
            missing.append("绑定竞品 OTA 价格")
        if missing:
            add(
                hotel_key,
                "data_quality_case",
                f"{stay_date} 缺少{'、'.join(missing)}，本次不应生成确定性调价建议。",
                "high",
                metadata,
            )

    latest_actions: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows["decisions"]:
        action = str(row.get("decision") or "")
        if "上调" not in action and "下调" not in action:
            continue
        key = (str(row.get("hotel_key") or ""), str(row.get("room_type") or "其他"), "上调" if "上调" in action else "下调")
        latest_actions.setdefault(key, row)
    for (hotel_key, room_type, direction), row in latest_actions.items():
        stay_date = _date_text(row.get("stay_date"))
        add(
            hotel_key,
            "pricing_case",
            f"{stay_date} {room_type} 出现明确{direction}建议；复盘时应结合真实销售率、可售库存、OTA 竞品价和展会因素验证效果。",
            "medium",
            {
                "stay_date": stay_date,
                "decision": row.get("decision"),
                "current_price": _number(row.get("current_price")),
                "suggested_price": _number(row.get("suggested_price")),
                "room_type": room_type,
            },
        )

    event_days: dict[str, dict[str, Any]] = {}
    for event in rows["events"]:
        start = max(start_date, _as_date(event.get("stay_start_date"), start_date))
        end = min(date.today(), _as_date(event.get("stay_end_date"), start))
        for offset in range(max(0, (end - start).days) + 1):
            event_days[(start + timedelta(days=offset)).isoformat()] = event
    own_keys = {str(status.get("hotel_key") or "") for status in rows["statuses"]}
    for hotel_key in own_keys:
        normal_prices = [
            _number(row.get("min_price"))
            for row in rows["prices"]
            if str(row.get("hotel_key") or "") == hotel_key and _date_text(row.get("stay_date")) not in event_days
        ]
        normal_prices = [price for price in normal_prices if price is not None]
        if not normal_prices:
            continue
        baseline = statistics.median(normal_prices)
        for (price_hotel, stay_date), price_row in prices.items():
            event = event_days.get(stay_date)
            event_price = _number(price_row.get("min_price"))
            if price_hotel != hotel_key or not event or event_price is None or event_price < baseline * 1.15:
                continue
            add(
                hotel_key,
                "event_pricing_case",
                f"{stay_date} 展会期自有最低价较近 30 天非展会日中位价高至少 15%；可作为展会溢价复盘候选，不直接固化为自动涨价规则。",
                "medium",
                {
                    "stay_date": stay_date,
                    "price_range": _price_range(price_row),
                    "normal_day_median": baseline,
                    "event_name": event.get("event_name"),
                    "decision": "event_premium_candidate",
                },
            )
    return candidates[:100]


def run_sync(days: int = 30, output_path: Path = DEFAULT_OUTPUT, *, dry_run: bool = False, confirm_write: str = "") -> dict[str, Any]:
    if days <= 0:
        raise ValueError("days 必须大于 0")
    if psycopg is None:
        raise DBWriterError("缺少 psycopg 依赖，请执行 pip install -r requirements.txt")
    direct = os.getenv("MNEMOSYNE_ENABLED", "false").lower() == "true" and os.getenv("MNEMOSYNE_WRITE_MODE", "staging").lower() == "direct"
    if direct and not dry_run and confirm_write != WRITE_CONFIRMATION:
        raise ValueError(f"直接写入需要中文二次确认：--confirm-write {WRITE_CONFIRMATION}")
    start_date = date.today() - timedelta(days=days - 1)
    with psycopg.connect(_load_database_url(), row_factory=psycopg.rows.dict_row) as connection:
        rows = fetch_source_rows(connection, start_date)
    candidates = extract_candidates(rows, start_date)
    if not dry_run:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            "".join(json.dumps(item, ensure_ascii=False, default=str) + "\n" for item in candidates),
            encoding="utf-8",
        )
    direct_writes = 0
    if direct and not dry_run:
        for item in candidates:
            if remember_hotel(
                str(item["hotel_key"]),
                str(item["content"]),
                str(item["memory_type"]),
                source="timescale",
                confidence=str(item["confidence"]),
                metadata=dict(item["metadata"]),
            ) == "direct":
                direct_writes += 1
    return {
        "days": days,
        "start_date": start_date.isoformat(),
        "tables": {key: len(value) for key, value in rows.items()},
        "candidates": len(candidates),
        "memory_types": dict(_counts(candidates)),
        "output": str(output_path),
        "dry_run": dry_run,
        "direct_writes": direct_writes,
    }


def _counts(records: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for record in records:
        counts[str(record["memory_type"])] += 1
    return counts


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ratio(value: float | None) -> float | None:
    if value is None:
        return None
    return value / 100 if value > 1 else value


def _date_text(value: Any) -> str:
    return value.isoformat() if isinstance(value, (date, datetime)) else str(value or "")


def _as_date(value: Any, fallback: date) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return fallback


def _price_range(row: dict[str, Any] | None) -> list[float] | None:
    if not row:
        return None
    low, high = _number(row.get("min_price")), _number(row.get("max_price"))
    return None if low is None or high is None else [low, high]


def _show_count(value: float | None) -> str:
    return "未知" if value is None else f"{value:g} 间"


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


def main() -> None:
    parser = argparse.ArgumentParser(description="从 TimescaleDB 只读抽取长期经营经验候选")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--confirm-write", default="", help="直接写入时必须填写中文确认词")
    args = parser.parse_args()
    try:
        result = run_sync(args.days, args.output, dry_run=args.dry_run, confirm_write=args.confirm_write)
    except (DBWriterError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
