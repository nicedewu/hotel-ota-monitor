from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from src.future_event_alert import _event_rows, _impact, _lodging_eligible, _priority, _venue_key, _venue_matches


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _snapshot_priority(event: dict[str, Any], impact_level: str) -> str | None:
    """Only known lodging relevance belongs in the operating summary."""
    normalized_impact = str(impact_level or "unknown").strip().lower()
    priority = _priority(event, normalized_impact)
    if priority is not None:
        return priority
    if _lodging_eligible(event, normalized_impact):
        return normalized_impact
    return None


def _hotel_venue_rules(cursor: Any, hotel_key: str) -> dict[str, str]:
    cursor.execute(
        """
        SELECT venue, impact_level
        FROM hotel_venue_impact_rules
        WHERE own_hotel_key = %s
        """,
        (hotel_key,),
    )
    return {str(row["venue"]): str(row["impact_level"]) for row in cursor.fetchall()}


def _snapshot_impact(
    event: dict[str, Any], hotel_key: str, venue_rules: dict[str, str]
) -> tuple[str, str, str]:
    venue = str(event.get("venue") or "")
    exact_impact = venue_rules.get(venue)
    display_venue = venue
    if not exact_impact:
        aliases = [rule_venue for rule_venue in venue_rules if _venue_matches(rule_venue, venue)]
        if aliases:
            display_venue = sorted(aliases)[0]
            exact_impact = venue_rules[display_venue]
    impact_level, impact_note = _impact(event, hotel_key, {hotel_key: exact_impact} if exact_impact else {})
    return impact_level or "unknown", impact_note, display_venue


def _future_events(cursor: Any, hotel_key: str, anchor_date: date, days: int) -> list[dict[str, Any]]:
    events = _event_rows(cursor, anchor_date + timedelta(days=1), anchor_date + timedelta(days=days))
    venue_rules = _hotel_venue_rules(cursor, hotel_key)
    selected: list[dict[str, Any]] = []
    for event in events:
        event_start = event.get("event_start_date")
        if isinstance(event_start, date) and event_start <= anchor_date:
            continue
        impact_level, impact_note, display_venue = _snapshot_impact(event, hotel_key, venue_rules)
        priority = _snapshot_priority(event, impact_level)
        if priority is None:
            continue
        selected.append({
            **event,
            "display_venue": display_venue,
            "impact_level": impact_level,
            "impact_note": impact_note,
            "priority": priority,
        })
    return selected


def _future_demand_signals(cursor: Any, hotel_key: str, anchor_date: date, days: int) -> list[dict[str, Any]]:
    cursor.execute(
        """
        WITH latest AS (
            SELECT DISTINCT ON (stay_date, window_hours)
                   stay_date, window_hours, net_pickup, score, status, current_collected_at
            FROM future_demand_signals
            WHERE hotel_key = %s
              AND stay_date BETWEEN %s AND %s
              AND current_collected_at >= now() - INTERVAL '10 days'
            ORDER BY stay_date, window_hours, current_collected_at DESC
        )
        SELECT stay_date, window_hours, net_pickup, score, status, current_collected_at
        FROM latest
        ORDER BY stay_date, window_hours
        """,
        (hotel_key, anchor_date + timedelta(days=1), anchor_date + timedelta(days=days)),
    )
    grouped: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for row in cursor.fetchall():
        item = dict(row)
        stay_date = item.get("stay_date")
        if isinstance(stay_date, date) and str(item.get("status") or "") != "data_quality_blocked":
            grouped[stay_date].append(item)

    signals: list[dict[str, Any]] = []
    for stay_date, rows in grouped.items():
        rows.sort(key=lambda item: int(_number(item.get("window_hours"))))
        primary = rows[0]
        positive = [item for item in rows if _number(item.get("net_pickup")) > 0]
        if not positive:
            continue
        primary_score = _number(primary.get("score"))
        persistent = len({int(_number(item.get("window_hours"))) for item in positive}) >= 2
        started = _number(primary.get("net_pickup")) > 0 and primary_score >= 40
        confirmed = started and persistent and primary_score >= 55
        state = "持续起量" if confirmed else "开始起量" if started else "观察"
        signals.append({
            "stay_date": stay_date,
            "state": state,
            "confirmed": confirmed,
            "persistent": persistent,
            "primary_score": round(primary_score, 1),
            "net_pickup": round(_number(primary.get("net_pickup")), 1),
            "positive_windows": sorted({int(_number(item.get("window_hours"))) for item in positive}),
            "source_note": "可售库存变化代理信号，不等同于已确认订单。",
        })
    return signals


def _fusion_advice(hotel_key: str, events: list[dict[str, Any]], signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    advice: list[dict[str, Any]] = []
    for event in events:
        start, end = event.get("event_start_date"), event.get("event_end_date")
        if not isinstance(start, date) or not isinstance(end, date):
            continue
        for signal in signals:
            stay_date = signal["stay_date"]
            if not signal["confirmed"] or not start <= stay_date <= end:
                continue
            raw = "|".join((hotel_key, str(event["event_reference"]), stay_date.isoformat()))
            advice.append({
                "advice_key": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                "stay_date": stay_date,
                "event_reference": event["event_reference"],
                "event_name": event["event_name"],
                "venue": event.get("venue"),
                "event_priority": event["priority"],
                "demand_state": signal["state"],
                "demand_score": signal["primary_score"],
                "recommended_action": "核对订单来源、房型售卖速度和竞品价格；如核对一致，按既有审批流程评估低价库存保护或人工调价。",
            })
    return advice


def load_future_event_demand_context(cursor: Any, hotel_key: str, anchor_date: date, days: int = 30) -> dict[str, Any]:
    """Return decision support only. This function never changes prices or sends messages."""
    events = _future_events(cursor, hotel_key, anchor_date, days)
    signals = _future_demand_signals(cursor, hotel_key, anchor_date, days)
    return {
        "anchor_date": anchor_date.isoformat(),
        "lookahead_days": days,
        "events": events,
        "demand_signals": signals,
        "price_control_advice": _fusion_advice(hotel_key, events, signals),
    }
