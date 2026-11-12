from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from src.db_writer import _load_database_url, psycopg


class FutureEventAlertError(RuntimeError):
    """A safe operator-facing future event alert failure."""


def _stage(lead_days: int) -> str | None:
    if 15 <= lead_days <= 30:
        return "D-30"
    if 8 <= lead_days <= 14:
        return "D-14"
    if 3 <= lead_days <= 7:
        return "D-7"
    if 1 <= lead_days <= 2:
        return "D-3"
    return None


def _priority(event: dict[str, Any], impact_level: str) -> str | None:
    if not _lodging_eligible(event, impact_level):
        return None
    category = str(event.get("category") or "").lower()
    demand_level = str(event.get("demand_level") or "").lower()
    if impact_level == "high":
        return "high"
    if demand_level == "high" and impact_level in {"high", "medium"}:
        return "high"
    if impact_level in {"high", "medium"} and category in {"esports", "performance", "sports", "acg"}:
        return "medium"
    return None


def _lodging_eligible(event: dict[str, Any], impact_level: str) -> bool:
    """A known hotel association is required; activity popularity is insufficient."""
    return (
        str(impact_level or "").lower() in {"high", "medium"}
        and str(event.get("search_impact_level") or "").lower() != "low"
    )


def _event_rows(cursor: Any, start_date: date, end_date: date) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT 'canonical' AS event_kind, CONCAT('canonical:', e.id) AS event_reference,
               e.event_name, e.venue, e.event_start_date, e.event_end_date, e.category,
               e.demand_level, e.source_confidence, e.canonical_status, 100 AS date_confidence,
               'offline' AS offline_mode,
               c.raw #>> '{entry,impact_level}' AS search_impact_level,
               c.raw -> 'entry' -> 'hotel_relevance' AS hotel_relevance
        FROM event_occurrences e
        LEFT JOIN event_candidates c ON c.id = e.source_row AND c.canonical_event_id = e.id
          AND e.source_kind = 'official_candidate'
        WHERE e.is_business_active = TRUE
          AND e.canonical_status <> 'cancelled'
          AND e.city = '上海'
          AND COALESCE(e.stay_start_date, e.event_start_date) <= %s
          AND COALESCE(e.stay_end_date, e.event_end_date) >= %s
        ORDER BY e.event_start_date, e.event_name
        """,
        (end_date, start_date),
    )
    canonical = [dict(row) for row in cursor.fetchall()]
    cursor.execute(
        """
        SELECT 'candidate' AS event_kind, CONCAT('candidate:', id) AS event_reference,
               event_name, venue, event_start_date, event_end_date, category,
               'normal' AS demand_level, source_confidence, event_status AS canonical_status,
               date_confidence, offline_mode, independent_source_count, aliases, event_identity_key,
               raw #>> '{entry,impact_level}' AS search_impact_level,
               raw -> 'entry' -> 'hotel_relevance' AS hotel_relevance
        FROM event_candidates
        WHERE verification_state = 'corroborated' AND source_confidence = 'corroborated'
          AND event_status = 'probable' AND city = '上海' AND offline_mode IN ('offline', 'hybrid')
          AND independent_source_count >= 2 AND corroborated_at >= now() - interval '14 days'
          AND canonical_event_id IS NULL AND event_start_date <= %s AND event_end_date >= %s
        ORDER BY independent_source_count DESC, id
        """,
        (end_date, start_date),
    )
    candidates = [dict(row) for row in cursor.fetchall()]
    if not candidates:
        return canonical
    cursor.execute(
        """
        SELECT event_name, event_start_date, event_end_date, venue, canonical_status
        FROM event_occurrences
        WHERE city = '上海'
          AND (is_business_active OR source_confidence IN ('official','authoritative','manual')
               OR canonical_status IN ('cancelled','conflict'))
          AND event_start_date BETWEEN %s - interval '90 days' AND %s + interval '90 days'
        """,
        (start_date, end_date),
    )
    return _merge_event_layers(canonical, candidates, [dict(row) for row in cursor.fetchall()])


def _merge_event_layers(canonical: list[dict], candidates: list[dict], authoritative: list[dict]) -> list[dict]:
    from src.event_corroboration import same_event

    result = list(canonical)
    for candidate in candidates:
        # The official record wins even when it carries a cancellation or different dates.
        if any(same_event(candidate, item) for item in authoritative):
            continue
        if any(same_event(candidate, item) and candidate['event_start_date'] <= item['event_end_date']
               and candidate['event_end_date'] >= item['event_start_date'] for item in result):
            continue
        result.append(candidate)
    return sorted(result, key=lambda item: (item['event_start_date'], item['event_name']))


def _active_hotels(cursor: Any) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT h.hotel_key, h.hotel_name, r.webhook_env_key
        FROM hotels h
        JOIN hotel_push_routes r
          ON r.own_hotel_key = h.hotel_key
         AND r.push_type = 'future_event_alert'
         AND r.channel = 'feishu'
         AND r.is_active = TRUE
        WHERE h.hotel_role = 'own' AND h.is_active = TRUE
        ORDER BY h.hotel_key
        """
    )
    return [dict(row) for row in cursor.fetchall()]


def _venue_key(value: Any) -> str:
    text = re.sub(r"[（(].*?[）)]", "", str(value or ""))
    text = text.replace("上海市", "").replace("上海", "")
    return re.sub(r"[\s·、,，。./；;|-]", "", text)


def _venue_matches(first: Any, second: Any) -> bool:
    a, b = _venue_key(first), _venue_key(second)
    return bool(a and b) and (a == b or (min(len(a), len(b)) >= 6 and (a.endswith(b) or b.endswith(a))))


def _venue_impacts(cursor: Any, hotel_keys: list[str], venue: str | None) -> dict[str, str]:
    if not hotel_keys or not venue:
        return {}
    cursor.execute(
        """
        SELECT own_hotel_key, venue, impact_level
        FROM hotel_venue_impact_rules
        WHERE own_hotel_key = ANY(%s)
        """,
        (hotel_keys,),
    )
    rank = {"low": 1, "medium": 2, "high": 3}
    result: dict[str, str] = {}
    for row in cursor.fetchall():
        if not _venue_matches(venue, row["venue"]):
            continue
        hotel_key, impact = str(row["own_hotel_key"]), str(row["impact_level"])
        if rank.get(impact, 0) > rank.get(result.get(hotel_key, ""), 0):
            result[hotel_key] = impact
    return result


def _impact(event: dict[str, Any], hotel_key: str, venue_impacts: dict[str, str]) -> tuple[str, str]:
    configured = venue_impacts.get(hotel_key, "")
    if str(event.get("search_impact_level") or "").lower() == "low":
        return "low", "Qwen 检索判定住宿关联较弱"
    evaluations = event.get("hotel_relevance") or []
    if evaluations:
        matched = [row for row in evaluations if isinstance(row, dict) and row.get("hotel_key") == hotel_key]
        if len(matched) != 1:
            return "", "缺少唯一的本店住宿关联判断"
        evaluation = matched[0]
        level = str(evaluation.get("impact_level") or "unknown").lower()
        reason = str(evaluation.get("reason") or "").strip()
        if level in {"low", "unknown"}:
            return level if level == "low" else "", reason or "本店住宿关联依据不足"
        if level not in {"high", "medium"} or not reason or not evaluation.get("source_indexes"):
            return "", "本店住宿关联判断缺少来源依据"
        if configured:
            rank = {"low": 1, "medium": 2, "high": 3}
            capped = min((configured, level), key=lambda value: rank.get(value, 0))
            return capped, f"场馆影响规则已匹配；Qwen 经营推断：{reason}"
    if configured:
        return configured, "场馆影响规则已匹配"
    return "", "未匹配门店场馆影响规则"


def _alert_key(hotel_key: str, event: dict[str, Any], stage: str) -> str:
    raw = "|".join((hotel_key, str(event["event_reference"]), stage))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _message(plan: dict[str, Any]) -> str:
    event = plan["event"]
    venue = str(event.get("venue") or "待官方补充")
    lines = [
            "【未来大型活动预警】",
            f"门店：{plan['hotel_name']}",
            f"活动：{event['event_name']}",
            f"时间：{event['event_start_date']} 至 {event['event_end_date']}（提前 {plan['lead_days']} 天）",
            f"地点：{venue}",
            f"优先级：{plan['priority']}｜影响依据：{plan['impact_note']}",
    ]
    if str(event.get("source_confidence") or "") == "corroborated":
        lines.append("证据：至少3个独立网站交叉证实，尚未抓取到官方页面。")
    lines.append("建议：核对未来库存、订单节奏和竞品价格；按既有审批流程决定是否调整价格或库存保护。")
    return "\n".join(lines)


def _insert_plan(cursor: Any, plan: dict[str, Any]) -> int | None:
    cursor.execute(
        """
        INSERT INTO future_event_alerts (
            alert_key, hotel_key, event_kind, event_reference, event_name,
            event_start_date, event_end_date, alert_stage, priority, payload, delivery_status
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, 'pending')
        ON CONFLICT (alert_key) DO NOTHING
        RETURNING id
        """,
        (
            plan["alert_key"], plan["hotel_key"], plan["event"]["event_kind"], plan["event"]["event_reference"],
            plan["event"]["event_name"], plan["event"]["event_start_date"], plan["event"]["event_end_date"],
            plan["stage"], plan["priority"], json.dumps(plan, ensure_ascii=False, default=str),
        ),
    )
    row = cursor.fetchone()
    return int(row["id"]) if row else None


def _mark_delivery(cursor: Any, alert_id: int, status: str, error: str = "") -> None:
    cursor.execute(
        """
        UPDATE future_event_alerts
        SET delivery_status = %s, delivery_error = NULLIF(%s, ''),
            sent_at = CASE WHEN %s = 'sent' THEN now() ELSE sent_at END,
            updated_at = now()
        WHERE id = %s
        """,
        (status, error[:500], status, alert_id),
    )


def _send(plan: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as handle:
        handle.write(_message(plan))
        message_file = Path(handle.name)
    try:
        subprocess.run(
            [
                sys.executable, "scripts/notify_feishu.py", "--mode", "event-alert", "--file", str(message_file),
                "--webhook-env-key", plan["webhook_env_key"],
                "--branch-push-hotel-key", plan["hotel_key"],
                "--branch-push-type", "future_event_alert",
                "--branch-push-source-type", "future_event_alert",
                "--branch-push-source-id", plan["alert_key"],
            ],
            check=True,
            timeout=45,
        )
    finally:
        message_file.unlink(missing_ok=True)


def run_future_event_alerts(*, anchor_date: date, days: int, push: bool, dry_run: bool = False) -> dict[str, Any]:
    if not 1 <= days <= 90:
        raise FutureEventAlertError("--days 必须在 1 到 90 之间")
    if psycopg is None:
        raise FutureEventAlertError("缺少 psycopg 依赖，无法生成未来活动预警")
    start_date = anchor_date + timedelta(days=1)
    end_date = anchor_date + timedelta(days=days)
    summary: dict[str, Any] = {"events": 0, "eligible": 0, "created": 0, "sent": 0, "skipped_existing": 0, "failures": [], "plans": []}
    try:
        with psycopg.connect(_load_database_url(), row_factory=psycopg.rows.dict_row) as connection:
            with connection.cursor() as cursor:
                events = _event_rows(cursor, start_date, end_date)
                hotels = _active_hotels(cursor)
                summary["events"] = len(events)
                for event in events:
                    event_start = event.get("event_start_date")
                    if not isinstance(event_start, date):
                        continue
                    lead_days = (event_start - anchor_date).days
                    stage = _stage(lead_days)
                    if stage is None:
                        continue
                    impacts = _venue_impacts(cursor, [str(hotel["hotel_key"]) for hotel in hotels], event.get("venue"))
                    for hotel in hotels:
                        hotel_key = str(hotel["hotel_key"])
                        impact_level, impact_note = _impact(event, hotel_key, impacts)
                        priority = _priority(event, impact_level)
                        if priority is None:
                            continue
                        plan = {
                            "hotel_key": hotel_key,
                            "hotel_name": str(hotel.get("hotel_name") or hotel_key),
                            "webhook_env_key": str(hotel["webhook_env_key"]),
                            "event": event,
                            "lead_days": lead_days,
                            "stage": stage,
                            "priority": priority,
                            "impact_level": impact_level,
                            "impact_note": impact_note,
                        }
                        plan["alert_key"] = _alert_key(hotel_key, event, stage)
                        summary["eligible"] += 1
                        summary["plans"].append(plan)
                        if dry_run:
                            continue
                        alert_id = _insert_plan(cursor, plan)
                        if alert_id is None:
                            summary["skipped_existing"] += 1
                            continue
                        summary["created"] += 1
                        if not push:
                            continue
                        try:
                            _send(plan)
                        except (OSError, subprocess.SubprocessError) as exc:
                            _mark_delivery(cursor, alert_id, "failed", str(exc))
                            summary["failures"].append(f"{hotel_key}/{event['event_name']}: 发送失败")
                            continue
                        _mark_delivery(cursor, alert_id, "sent")
                        summary["sent"] += 1
            connection.commit()
    except FutureEventAlertError:
        raise
    except Exception as exc:
        raise FutureEventAlertError(f"未来活动预警处理失败：{type(exc).__name__}") from exc
    return summary
