from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from statistics import mean
from typing import Any, Iterable

from src.db_writer import DBWriterError, _load_database_url, psycopg


SOURCE_CONFIDENCE_POINTS = {
    "official": 30,
    "authoritative": 25,
    "media": 12,
    "discovery": 5,
}
EVENT_STATUS_POINTS = {
    "promoted": 20,
    "confirmed": 20,
    "probable": 10,
    "discovered": 0,
}
VENUE_IMPACT_POINTS = {
    "high": 20,
    "medium": 10,
    "low": 4,
    "unknown": 0,
}
EVENT_DEMAND_LEVEL_POINTS = {
    "high": 12,
    "medium": 7,
    "low": 3,
    "unknown": 0,
}
VALID_ANOMALY_STATUSES = {
    "single_hotel_observation",
    "city_observation",
    "preliminary",
    "explained",
    "unexplained",
}


class DemandAttributionError(RuntimeError):
    """A safe, operator-facing demand attribution failure."""


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise DemandAttributionError("日期必须为 YYYY-MM-DD") from exc


def _anomaly_key(stay_date: date) -> str:
    return hashlib.sha256(f"future-demand-v1:{stay_date.isoformat()}".encode("utf-8")).hexdigest()


def evaluation_dates(start_date: date, days: int) -> list[date]:
    if not 1 <= days <= 30:
        raise DemandAttributionError("--days 必须在 1 到 30 之间")
    return [start_date + timedelta(days=offset) for offset in range(days)]


def build_city_anomaly(stay_date: date, signals: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate availability-proxy signals without calling them orders."""

    raw_signals = [dict(item) for item in signals]
    by_hotel: dict[str, list[dict[str, Any]]] = {}
    for signal in raw_signals:
        hotel_key = str(signal.get("hotel_key") or "")
        if hotel_key:
            by_hotel.setdefault(hotel_key, []).append(signal)

    # Retain every comparable window, but keep one short-window observation per
    # hotel in the city total so the same hotel's 24h/72h changes are not
    # double-counted.  A positive long window becomes persistence evidence.
    valid: list[dict[str, Any]] = []
    data_quality_blocked_signals = 0
    for hotel_key, hotel_signals in sorted(by_hotel.items()):
        ordered = sorted(hotel_signals, key=lambda item: int(_number(item.get("window_hours"))))
        primary = ordered[0]
        if str(primary.get("status")) == "data_quality_blocked":
            data_quality_blocked_signals += 1
            continue
        if _number(primary.get("net_pickup")) <= 0:
            continue
        positive_window_hours = sorted(
            {
                int(_number(item.get("window_hours")))
                for item in ordered
                if str(item.get("status")) != "data_quality_blocked"
                and _number(item.get("net_pickup")) > 0
            }
        )
        valid.append(
            {
                **primary,
                "hotel_key": hotel_key,
                "positive_window_hours": positive_window_hours,
                "persistence_confirmed": len(positive_window_hours) >= 2,
            }
        )

    hotel_keys = [str(signal["hotel_key"]) for signal in valid]
    total_pickup = round(sum(_number(signal.get("net_pickup")) for signal in valid))
    if not valid:
        return {
            "anomaly_key": _anomaly_key(stay_date),
            "stay_date": stay_date,
            "hotel_keys": [],
            "signal_count": 0,
            "total_net_pickup": 0,
            "score": 0,
            "status": "city_observation",
            "features": {
                "source": "availability_proxy",
                "sales_claimed": False,
                "data_quality_blocked_signals": data_quality_blocked_signals,
            },
        }

    mean_score = mean(_number(signal.get("score")) for signal in valid)
    breadth_points = min(20, max(0, len(hotel_keys) - 1) * 10)
    pickup_points = min(20, total_pickup * 2)
    persistent_hotel_count = sum(1 for signal in valid if signal["persistence_confirmed"])
    persistence_points = min(10, persistent_hotel_count * 5)
    score = min(100, round(mean_score + breadth_points + pickup_points + persistence_points))
    if len(hotel_keys) < 2:
        status = "single_hotel_observation"
    elif score >= 70:
        status = "preliminary"
    else:
        status = "city_observation"
    return {
        "anomaly_key": _anomaly_key(stay_date),
        "stay_date": stay_date,
        "hotel_keys": hotel_keys,
        "signal_count": len(valid),
        "total_net_pickup": total_pickup,
        "score": score,
        "status": status,
        "features": {
            "source": "availability_proxy",
            "sales_claimed": False,
            "mean_signal_score": round(mean_score, 2),
            "breadth_points": breadth_points,
            "pickup_points": pickup_points,
            "persistence_points": persistence_points,
            "persistent_hotel_count": persistent_hotel_count,
            "hotel_signal_summaries": {
                str(signal["hotel_key"]): {
                    "score": round(_number(signal.get("score")), 2),
                    "net_pickup": round(_number(signal.get("net_pickup")), 2),
                    "positive_window_hours": signal["positive_window_hours"],
                    "persistence_confirmed": signal["persistence_confirmed"],
                }
                for signal in valid
            },
            "data_quality_blocked_signals": data_quality_blocked_signals,
        },
    }


def rank_event_evidence(anomaly: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    """Rank date-matched causes; ranking does not confirm business causality."""

    source_confidence = str(event.get("source_confidence") or "discovery").lower()
    event_status = str(event.get("event_status") or "discovered").lower()
    date_confidence = max(0, min(100, int(_number(event.get("date_confidence")))))
    venue_confidence = max(0, min(100, int(_number(event.get("venue_confidence")))))
    demand_level = str(event.get("demand_level") or "unknown").lower()
    duration_days = _event_duration_days(event)
    duration_points = min(8, max(0, duration_days - 1) * 4)
    venue_points, venue_impacts = _venue_impact_summary(event)
    demand_level_points = EVENT_DEMAND_LEVEL_POINTS.get(demand_level, 0)
    score = min(
        100,
        30
        + SOURCE_CONFIDENCE_POINTS.get(source_confidence, 0)
        + EVENT_STATUS_POINTS.get(event_status, 0)
        + round(date_confidence * 0.15)
        + round(venue_confidence * 0.05)
        + duration_points
        + venue_points
        + demand_level_points,
    )
    reasons = [
        "活动日期与异常入住日重合",
        f"来源可信度：{source_confidence}",
        f"活动状态：{event_status}",
        f"日期置信度：{date_confidence}",
        f"活动持续天数：{duration_days}",
        f"场馆证据置信度：{venue_confidence}",
    ]
    if venue_impacts:
        reasons.append(f"受影响酒店的场馆规则：{venue_impacts}")
    if demand_level != "unknown":
        reasons.append(f"正式活动需求级别：{demand_level}")
    return {
        "event_kind": event["event_kind"],
        "event_reference": event["event_reference"],
        "event_name": event["event_name"],
        "venue": event.get("venue"),
        "category": event.get("category") or "other",
        "source_confidence": source_confidence,
        "event_status": event_status,
        "date_confidence": date_confidence,
        "venue_confidence": venue_confidence,
        "offline_mode": str(event.get("offline_mode") or "unknown").lower(),
        "hotel_venue_impacts": venue_impacts,
        "attribution_score": score,
        "reason": reasons,
    }


def _event_duration_days(event: dict[str, Any]) -> int:
    start = event.get("event_start_date")
    end = event.get("event_end_date")
    if not start or not end:
        return 1
    try:
        return max(1, (_parse_date(end) - _parse_date(start)).days + 1)
    except DemandAttributionError:
        return 1


def _venue_impact_summary(event: dict[str, Any]) -> tuple[int, dict[str, str]]:
    raw_impacts = event.get("hotel_venue_impacts")
    if not isinstance(raw_impacts, dict):
        return 0, {}
    impacts = {
        str(hotel_key): str(level).lower()
        for hotel_key, level in raw_impacts.items()
        if str(level).lower() in VENUE_IMPACT_POINTS
    }
    points = max((VENUE_IMPACT_POINTS[level] for level in impacts.values()), default=0)
    return points, impacts


def build_shadow_alert_card(anomaly: dict[str, Any], attributions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Produce a review card only; this function never sends a notification or changes pricing."""

    top = attributions[0] if attributions else None
    hotel_keys = [str(item) for item in anomaly.get("hotel_keys") or []]
    hotel_count = len(hotel_keys)
    anomaly_score = int(_number(anomaly.get("score")))
    attribution_score = int(_number(top.get("attribution_score"))) if top else 0
    summaries = anomaly.get("features", {}).get("hotel_signal_summaries", {})
    summaries = summaries if isinstance(summaries, dict) else {}
    impacts = top.get("hotel_venue_impacts", {}) if top else {}
    impacts = impacts if isinstance(impacts, dict) else {}
    target_reasons: dict[str, str] = {}

    # Strong multi-hotel demand is actionable even when reverse search cannot
    # yet explain it.  The event is an explanation, never a prerequisite.
    strong_city_demand = hotel_count >= 2 and anomaly_score >= 70
    if strong_city_demand:
        target_reasons = {hotel_key: "多店强需求异常" for hotel_key in hotel_keys}

    for hotel_key in hotel_keys:
        summary = summaries.get(hotel_key, {})
        store_score = _number(summary.get("score")) if isinstance(summary, dict) else 0
        persistent = bool(summary.get("persistence_confirmed")) if isinstance(summary, dict) else False
        if hotel_count == 1 and store_score >= 55 and persistent:
            target_reasons[hotel_key] = "单店强需求异常，短长窗口持续下降"

    reliable_event = bool(
        top
        and str(top.get("source_confidence")) in {"official", "authoritative"}
        and int(_number(top.get("date_confidence"))) >= 90
        and str(top.get("event_status")) in {"confirmed", "promoted"}
        and str(top.get("offline_mode")) in {"offline", "hybrid"}
    )
    if reliable_event:
        for hotel_key in hotel_keys:
            summary = summaries.get(hotel_key, {})
            store_score = _number(summary.get("score")) if isinstance(summary, dict) else 0
            impact = str(impacts.get(hotel_key) or "unknown").lower()
            if 45 <= store_score < 55 and impact == "high":
                target_reasons.setdefault(hotel_key, "中等需求异常且本店场馆影响高")

    eligible = bool(target_reasons)
    if top is None and not eligible:
        return None

    alert_score = anomaly_score if top is None else min(100, round(anomaly_score * 0.7 + attribution_score * 0.3))
    priority = "high" if strong_city_demand or any("强需求" in reason for reason in target_reasons.values()) else "medium" if eligible else "low"
    event_name = str(top.get("event_name")) if top else "原因待核实"
    summary = f"{hotel_count} 家酒店出现可售库存变化代理信号，净变化 {anomaly['total_net_pickup']}。"
    if top:
        summary += f"可能关联：{event_name}（归因证据 {attribution_score}/100）。"
    else:
        summary += "暂未找到高可信活动原因，建议优先核查本店订单、取消和房型。"
    return {
        "mode": "shadow",
        "eligible_for_business_alert": eligible,
        "priority": priority,
        "alert_score": alert_score,
        "title": f"需求异常影子预警｜{anomaly['stay_date']}｜{event_name}",
        "summary": summary,
        "target_hotel_keys": sorted(target_reasons),
        "target_reasons": target_reasons,
        "evidence": list(top.get("reason") or []) if top else ["未找到高可信活动归因"],
        "recommended_action": "人工核对订单来源、房型售卖速度和竞品价格；确认后再进入既有调价审批流程。",
        "guardrails": [
            "影子预警仅基于可售库存变化代理，不代表已确认订单或已证实因果关系。",
            "本卡片不发送飞书，不自动提升活动状态，不自动调价。",
        ],
    }


def _require_database() -> None:
    if psycopg is None:
        raise DemandAttributionError("缺少 psycopg 依赖，无法读取需求信号")


def _fetch_latest_signals(cursor: Any, start_date: date, end_date: date) -> list[dict[str, Any]]:
    cursor.execute(
        """
        WITH latest_signals AS (
            SELECT DISTINCT ON (hotel_key, stay_date, window_hours)
                   hotel_key, stay_date, window_hours, current_collected_at,
                   net_pickup, score, status, features
            FROM future_demand_signals
            WHERE stay_date BETWEEN %s AND %s
              AND current_collected_at >= now() - INTERVAL '10 days'
            ORDER BY hotel_key, stay_date, window_hours, current_collected_at DESC
        )
        SELECT hotel_key, stay_date, window_hours, current_collected_at,
               net_pickup, score, status, features
        FROM latest_signals
        ORDER BY stay_date, hotel_key, window_hours
        """,
        (start_date, end_date),
    )
    return [dict(row) for row in cursor.fetchall()]


def _fetch_event_evidence(cursor: Any, stay_date: date) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT
            'canonical' AS event_kind,
            CONCAT('canonical:', e.id) AS event_reference,
            e.event_name,
            e.venue,
            e.category,
            COALESCE(e.demand_level, 'unknown') AS demand_level,
            COALESCE(e.stay_start_date, e.event_start_date) AS event_start_date,
            COALESCE(e.stay_end_date, e.event_end_date) AS event_end_date,
            e.source_confidence,
            e.canonical_status AS event_status,
            100 AS date_confidence,
            100 AS venue_confidence,
            'offline' AS offline_mode
        FROM event_occurrences e
        WHERE e.is_business_active = TRUE
          AND e.canonical_status <> 'cancelled'
          AND COALESCE(e.stay_start_date, e.event_start_date) <= %s
          AND COALESCE(e.stay_end_date, e.event_end_date) >= %s
        ORDER BY e.event_start_date, e.event_name
        """,
        (stay_date, stay_date),
    )
    return [dict(row) for row in cursor.fetchall()]


def _fetch_hotel_venue_impacts(cursor: Any, hotel_keys: list[str], venue: str | None) -> dict[str, str]:
    if not hotel_keys or not venue:
        return {}
    cursor.execute(
        """
        SELECT own_hotel_key, impact_level
        FROM hotel_venue_impact_rules
        WHERE own_hotel_key = ANY(%s)
          AND venue = %s
        """,
        (hotel_keys, venue),
    )
    return {str(row["own_hotel_key"]): str(row["impact_level"]) for row in cursor.fetchall()}


def _persist_anomaly(cursor: Any, anomaly: dict[str, Any], attributions: list[dict[str, Any]]) -> int:
    cursor.execute(
        """
        INSERT INTO demand_anomalies (
            anomaly_key, stay_date, affected_hotels, signal_count,
            total_net_pickup, score, status, features, evaluated_at
        ) VALUES (
            %(anomaly_key)s, %(stay_date)s, %(affected_hotels)s::jsonb, %(signal_count)s,
            %(total_net_pickup)s, %(score)s, %(status)s, %(features)s::jsonb, now()
        )
        ON CONFLICT (anomaly_key) DO UPDATE SET
            affected_hotels = EXCLUDED.affected_hotels,
            signal_count = EXCLUDED.signal_count,
            total_net_pickup = EXCLUDED.total_net_pickup,
            score = EXCLUDED.score,
            status = EXCLUDED.status,
            features = EXCLUDED.features,
            evaluated_at = now(),
            updated_at = now()
        RETURNING id
        """,
        {
            **anomaly,
            "affected_hotels": json.dumps(anomaly["hotel_keys"], ensure_ascii=False),
            "features": json.dumps(anomaly["features"], ensure_ascii=False, default=str),
        },
    )
    row = cursor.fetchone()
    if row is None:
        raise DemandAttributionError("需求异常写入未返回 ID")
    anomaly_id = int(row["id"])
    cursor.execute("DELETE FROM demand_event_attributions WHERE anomaly_id = %s", (anomaly_id,))
    for rank, item in enumerate(attributions, start=1):
        cursor.execute(
            """
            INSERT INTO demand_event_attributions (
                anomaly_id, event_kind, event_reference, event_name, venue, category,
                source_confidence, event_status, attribution_score, rank, reason, evaluated_at
            ) VALUES (
                %(anomaly_id)s, %(event_kind)s, %(event_reference)s, %(event_name)s, %(venue)s, %(category)s,
                %(source_confidence)s, %(event_status)s, %(attribution_score)s, %(rank)s, %(reason)s::jsonb, now()
            )
            """,
            {
                **item,
                "anomaly_id": anomaly_id,
                "rank": rank,
                "reason": json.dumps(item["reason"], ensure_ascii=False),
            },
        )
    return anomaly_id


def evaluate_demand_anomalies(
    start: str | date,
    *,
    days: int,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    _require_database()
    start_date = _parse_date(start)
    end_date = start_date + timedelta(days=days - 1)
    stay_dates = evaluation_dates(start_date, days)
    try:
        with psycopg.connect(_load_database_url(), row_factory=psycopg.rows.dict_row) as connection:
            with connection.cursor() as cursor:
                rows = _fetch_latest_signals(cursor, start_date, end_date)
                grouped: dict[date, list[dict[str, Any]]] = {}
                for row in rows:
                    stay_date = _parse_date(row["stay_date"])
                    grouped.setdefault(stay_date, []).append(row)
                results: list[dict[str, Any]] = []
                for stay_date in stay_dates:
                    anomaly = build_city_anomaly(stay_date, grouped.get(stay_date, []))
                    evidence = _fetch_event_evidence(cursor, stay_date) if anomaly["signal_count"] else []
                    for event in evidence:
                        event["hotel_venue_impacts"] = _fetch_hotel_venue_impacts(
                            cursor, anomaly["hotel_keys"], event.get("venue")
                        )
                    attributions = sorted(
                        (rank_event_evidence(anomaly, event) for event in evidence),
                        key=lambda item: item["attribution_score"],
                        reverse=True,
                    )[:3]
                    if anomaly["status"] == "preliminary":
                        anomaly["status"] = "explained" if attributions and attributions[0]["attribution_score"] >= 70 else "unexplained"
                    shadow_alert = build_shadow_alert_card(anomaly, attributions)
                    if shadow_alert is not None:
                        anomaly["features"]["shadow_alert"] = shadow_alert
                    result = {
                        **anomaly,
                        "attributions": attributions,
                        "shadow_alert": shadow_alert,
                        "dry_run": dry_run,
                    }
                    if not dry_run:
                        result["anomaly_id"] = _persist_anomaly(cursor, anomaly, attributions)
                    results.append(result)
                return results
    except DBWriterError as exc:
        raise DemandAttributionError(str(exc)) from exc
    except DemandAttributionError:
        raise
    except Exception as exc:
        raise DemandAttributionError(f"需求异常归因失败：{type(exc).__name__}") from exc


def list_demand_anomalies(start: str | date, *, days: int) -> list[dict[str, Any]]:
    _require_database()
    if not 1 <= days <= 30:
        raise DemandAttributionError("--days 必须在 1 到 30 之间")
    start_date = _parse_date(start)
    end_date = start_date + timedelta(days=days - 1)
    try:
        with psycopg.connect(_load_database_url(), row_factory=psycopg.rows.dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, stay_date, affected_hotels, signal_count, total_net_pickup,
                           score, status, features, evaluated_at
                    FROM demand_anomalies
                    WHERE stay_date BETWEEN %s AND %s
                    ORDER BY stay_date, score DESC
                    """,
                    (start_date, end_date),
                )
                anomalies = [dict(row) for row in cursor.fetchall()]
                for anomaly in anomalies:
                    cursor.execute(
                        """
                        SELECT event_kind, event_reference, event_name, venue, category,
                               source_confidence, event_status, attribution_score, rank, reason
                        FROM demand_event_attributions
                        WHERE anomaly_id = %s
                        ORDER BY rank
                        """,
                        (anomaly["id"],),
                    )
                    anomaly["attributions"] = [dict(row) for row in cursor.fetchall()]
                return anomalies
    except DBWriterError as exc:
        raise DemandAttributionError(str(exc)) from exc
    except Exception as exc:
        raise DemandAttributionError(f"需求异常查询失败：{type(exc).__name__}") from exc
