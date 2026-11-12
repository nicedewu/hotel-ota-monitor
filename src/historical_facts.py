from __future__ import annotations

from datetime import date
from typing import Any


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def load_fact_snapshot_rows(
    cursor: Any,
    hotel_key: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[dict[str, Any]]:
    """Load persisted structured weekly facts without touching legacy fact tables."""
    cursor.execute("SELECT to_regclass('public.weekly_reviews') AS relation")
    if not cursor.fetchone()["relation"]:
        return []
    clauses = ["fact_snapshot_json IS NOT NULL"]
    values: list[Any] = []
    if hotel_key:
        clauses.append("hotel_key = %s")
        values.append(hotel_key)
    if start_date is not None:
        clauses.append("week_end >= %s")
        values.append(start_date)
    if end_date is not None:
        clauses.append("week_start <= %s")
        values.append(end_date)
    cursor.execute(
        f"""
        SELECT id, hotel_key, week_start, week_end, generated_at, fact_snapshot_json
        FROM weekly_reviews
        WHERE {" AND ".join(clauses)}
        ORDER BY generated_at DESC NULLS LAST, id DESC
        """,
        values,
    )
    return [dict(row) for row in cursor.fetchall()]


def _fact_items(snapshots: list[dict[str, Any]], key: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    items: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for snapshot in snapshots:
        payload = snapshot.get("fact_snapshot_json")
        if not isinstance(payload, dict):
            continue
        facts = payload.get("facts")
        if not isinstance(facts, dict):
            continue
        values = facts.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            if isinstance(item, dict):
                items.append((snapshot, item))
    return items


def fact_decision_rows(
    snapshots: list[dict[str, Any]],
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    room_category: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Flatten weekly_reviews decision facts to the historical decision shape."""
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for snapshot, item in _fact_items(snapshots, "price_decisions"):
        payload = snapshot.get("fact_snapshot_json") if isinstance(snapshot.get("fact_snapshot_json"), dict) else {}
        stay_date = _as_date(item.get("stay_date"))
        category = str(item.get("room_category") or item.get("normalized_room_type") or "").strip()
        if stay_date is None or not category:
            continue
        if start_date is not None and stay_date < start_date:
            continue
        if end_date is not None and stay_date >= end_date:
            continue
        if room_category and category != room_category:
            continue
        source_hotel_key = str(item.get("hotel_key") or snapshot.get("hotel_key") or "")
        key = (source_hotel_key, stay_date.isoformat(), category)
        if key in seen:
            continue
        seen.add(key)
        raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
        status = raw.get("own_daily_status_source") if isinstance(raw.get("own_daily_status_source"), dict) else {}
        status_raw = status.get("raw") if isinstance(status.get("raw"), dict) else status
        result.append(
            {
                "id": item.get("id"),
                "run_id": item.get("run_id") or payload.get("run_id"),
                "hotel_key": source_hotel_key,
                "stay_date": stay_date,
                "room_category": category,
                "own_price": item.get("current_price"),
                "competitor_min_price": item.get("competitor_min_price"),
                "suggested_price": item.get("suggested_price"),
                "action": item.get("action"),
                "reason": item.get("reason"),
                "decision_at": item.get("decision_at") or snapshot.get("generated_at"),
                "raw": raw,
                "room_total": status.get("room_total"),
                "sold_rooms": status.get("sold_rooms"),
                "available_rooms": status.get("available_rooms"),
                "occupancy_rate": status.get("occupancy_rate", item.get("occupancy_rate")),
                "status_raw": status_raw,
                "_source_ref": f"weekly_reviews.fact_snapshot_json:{snapshot.get('id')}",
            }
        )
        if limit is not None and len(result) >= limit:
            break
    result.sort(key=lambda row: (row["stay_date"], str(row.get("decision_at") or "")), reverse=True)
    return result[:limit] if limit is not None else result


def fact_comparison_records(
    snapshots: list[dict[str, Any]],
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    own_hotel_key: str | None = None,
    room_category: str | None = None,
) -> list[dict[str, Any]]:
    """Return structured comparison records with their persisted source reference."""
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for snapshot, item in _fact_items(snapshots, "price_comparisons"):
        stay_date = _as_date(item.get("stay_date"))
        category = str(item.get("normalized_room_type") or item.get("room_category") or "").strip()
        own_key = str(item.get("own_hotel_key") or snapshot.get("hotel_key") or "").strip()
        if stay_date is None or not category:
            continue
        if start_date is not None and stay_date < start_date:
            continue
        if end_date is not None and stay_date > end_date:
            continue
        if own_hotel_key and own_key != own_hotel_key:
            continue
        if room_category and category != room_category:
            continue
        key = (stay_date.isoformat(), own_key, category, str(item.get("competitor_hotel_key") or ""))
        if key in seen:
            continue
        seen.add(key)
        record = dict(item)
        record["stay_date"] = stay_date
        record["_source_ref"] = f"weekly_reviews.fact_snapshot_json:{snapshot.get('id')}"
        result.append(record)
    result.sort(key=lambda row: (row["stay_date"], str(row.get("normalized_room_type") or "")), reverse=True)
    return result


def fact_comparison_rows(
    snapshots: list[dict[str, Any]],
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    own_hotel_key: str | None = None,
    room_category: str | None = None,
) -> list[dict[str, Any]]:
    """Flatten structured comparison facts to the old price-row shape."""
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for snapshot, item in _fact_items(snapshots, "price_comparisons"):
        stay_date = _as_date(item.get("stay_date"))
        category = str(item.get("normalized_room_type") or item.get("room_category") or "").strip()
        own_key = str(item.get("own_hotel_key") or snapshot.get("hotel_key") or "").strip()
        competitor_key = str(item.get("competitor_hotel_key") or "").strip()
        if stay_date is None or not category or not own_key:
            continue
        if start_date is not None and stay_date < start_date:
            continue
        if end_date is not None and stay_date > end_date:
            continue
        if own_hotel_key and own_key != own_hotel_key:
            continue
        if room_category and category != room_category:
            continue
        source_ref = f"weekly_reviews.fact_snapshot_json:{snapshot.get('id')}"
        collected_at = item.get("collected_at") or snapshot.get("generated_at")
        pairs = (
            (own_key, item.get("own_room_name"), item.get("own_price")),
            (competitor_key, item.get("competitor_room_name"), item.get("competitor_price")),
        )
        for source_key, room_name, price in pairs:
            if not source_key or price in (None, ""):
                continue
            key = (stay_date.isoformat(), source_key, category, str(price))
            if key in seen:
                continue
            seen.add(key)
            result.append(
                {
                    "hotel_key": source_key,
                    "source_hotel_key": source_key,
                    "hotel_name": source_key,
                    "stay_date": stay_date,
                    "room_name": room_name or category,
                    "room_category": category,
                    "price": price,
                    "available": True,
                    "collected_at": collected_at,
                    "raw": {"source_ref": source_ref, "comparison": item},
                    "_source_ref": source_ref,
                }
            )
    result.sort(key=lambda row: (row["stay_date"], row["hotel_key"], row["room_category"], str(row.get("collected_at") or "")), reverse=True)
    return result


def metrics_to_pms_rows(
    metrics_rows: list[dict[str, Any]],
    *,
    required_slot: str | None = None,
) -> list[dict[str, Any]]:
    """Expand daily operating metrics snapshots to daily-status-compatible rows.

    ``required_slot`` lets consumers with a fixed business definition (for
    example, weekly final occupancy) exclude intraday snapshots.
    """
    result: list[dict[str, Any]] = []
    for row in metrics_rows:
        metrics = row.get("metrics")
        if not isinstance(metrics, dict):
            continue
        business_date = row.get("business_date")
        snapshots = metrics.get("snapshots")
        if not isinstance(snapshots, dict):
            continue
        for slot, snapshot in snapshots.items():
            if required_slot is not None and slot != required_slot:
                continue
            if not isinstance(snapshot, dict):
                continue
            total = snapshot.get("room_total")
            sold = snapshot.get("sold_rooms")
            available = snapshot.get("available_rooms")
            if total is None or sold is None or available is None:
                continue
            result.append(
                {
                    "hotel_key": str(metrics.get("hotel_key") or row.get("hotel_key") or ""),
                    "stay_date": business_date,
                    "collected_at": snapshot.get("collected_at") or row.get("updated_at"),
                    "room_total": total,
                    "occupied_rooms": snapshot.get("occupied_rooms"),
                    "available_rooms": available,
                    "sold_rooms": sold,
                    "booking_count": snapshot.get("booking_count"),
                    "occupancy_rate": snapshot.get("occupancy_rate"),
                    "raw": {"source": "hotel_daily_operating_metrics", "snapshot_slot": slot, "metrics": metrics},
                    "_source_ref": f"hotel_daily_operating_metrics:{business_date}:{slot}",
                }
            )
    result.sort(key=lambda row: (str(row.get("stay_date") or ""), str(row.get("collected_at") or "")), reverse=True)
    return result


def merge_prefer_new(
    new_rows: list[dict[str, Any]],
    fallback_rows: list[dict[str, Any]],
    key_fields: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Merge a new fact source over legacy fallback rows by a stable business key."""
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for row in [*new_rows, *fallback_rows]:
        key = tuple(str(row.get(field) or "") for field in key_fields)
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result
