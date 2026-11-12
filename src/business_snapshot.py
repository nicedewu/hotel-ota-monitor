from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from math import isfinite
from pathlib import Path
from statistics import median
from typing import Any
from uuid import uuid4

from src.config import HotelConfig, load_hotels
from src.advice import (
    generate_business_snapshot_advice,
    summarize_competitor_price_for_business_snapshot,
)
from src.db_writer import _load_database_url, psycopg
from src.exhibition_timing import annotate_events_for_day
from src.future_event_alert import _lodging_eligible
from src.event_demand_context import _hotel_venue_rules, _snapshot_impact
from src.hermes_json import normalize_hermes_json_output
from src.memory_provider import recall_hotel
from src.utils import ensure_dir, now_iso, resolve_stay_dates

logger = logging.getLogger(__name__)


_TEMPORARY_PINNED_EVENT_NAMES = frozenset({
    "2026LPL总决赛暨15周年庆",
    "2026无畏契约全球冠军赛",
})
_TEMPORARY_PINNED_EVENT_UNTIL = date(2026, 10, 18)



class BusinessSnapshotError(RuntimeError):
    """A user-facing report generation failure."""


_HERMES_ANALYSIS_DIMENSIONS = (
    "销售节奏",
    "库存结构",
    "价格验证",
    "展会转化",
    "入住兑现",
)
_HERMES_ANALYSIS_MAX_ITEMS = 3


def _snapshot_slot(generated_at: str) -> str:
    """Use the actual generation time so delayed scheduled jobs do not mislabel the snapshot."""
    try:
        hour = datetime.fromisoformat(generated_at).hour
    except (TypeError, ValueError):
        return "morning"
    if hour < 12:
        return "morning"
    if hour < 18:
        return "afternoon"
    return "evening"


_HERMES_SECRET_VALUE = re.compile(
    r"(?i)\b(api[_ -]?key|authorization|token|secret|password)\b\s*[:=]\s*\S+"
)



def build_business_snapshot_report(
    *,
    stay_date: str | None,
    days: int,
    detail_level: str,
    config_path: str,
    output_path: str,
    own_hotel_key: str = "own_demo_a",
    slot: str = "morning",
    run_id: str | None = None,
) -> dict[str, Any]:
    if detail_level != "full":
        raise BusinessSnapshotError("business-snapshot-report 当前只支持 --detail-level full")
    stay_dates = resolve_stay_dates(stay_date, days)
    own_hotels, competitor_hotels = _load_hotel_scope(config_path, own_hotel_key)
    alerts: list[str] = []
    source_run_id = run_id
    run_id = run_id or uuid4().hex
    generated_at = now_iso()
    actual_slot = _snapshot_slot(generated_at)

    db_data = _read_database_snapshot(stay_dates, own_hotels, competitor_hotels, alerts, own_hotel_key, source_run_id)
    future_event_demand_context = db_data.get("future_event_demand_context") or {
        "lookahead_days": 30, "events": [], "demand_signals": [], "price_control_advice": []
    }
    evening_warning = _evening_warning_summary(own_hotel_key, stay_dates[0]) if actual_slot == "evening" else ""
    exhibition_context = _build_exhibition_context(
        stay_dates[0],
        db_data["exhibitions"],
        db_data["exhibition_available"],
        db_data.get("exhibition_rule_available", False),
    )
    hermes = _read_json(Path("output/latest/hermes_payload.json"))
    fallback_own, fallback_competitors = _rooms_from_hermes(hermes, stay_dates)

    own_rows = db_data["own_prices"] or fallback_own
    competitor_rows = db_data["competitor_prices"] or fallback_competitors
    competitor_supply_alerts = _competitor_room_supply_alerts(
        db_data.get("room_availability_history") or [],
        own_hotel_key,
        competitor_hotels,
        stay_dates[0],
    )
    pms_summary = _pms_summary(db_data["pms_status"], alerts)
    room_type_inventory = _pms_room_type_details(db_data["pms_status"])
    previous_pms_summary = _pms_summary(db_data.get("previous_pms_status"), []) if db_data.get("previous_pms_status") else {}
    pms_change = _pms_change_summary(
        pms_summary,
        previous_pms_summary,
        room_type_inventory,
        _pms_room_type_details(db_data.get("previous_pms_status")),
    )
    comparison_path = Path("output/hotel_sets") / own_hotel_key / stay_dates[0] / "price_compare.json"
    business_compare = _read_json(comparison_path)
    compare_warnings = [str(warning) for warning in (business_compare.get("warnings") or []) if "满房" in str(warning)]
    compares = _business_compare_details(business_compare, stay_dates)
    if not compares:
        compares = _price_compare_details(stay_dates, own_rows, competitor_rows, alerts)
    recommendations = _recommendations(compares, pms_summary, db_data["decisions"], alerts, pms_change=pms_change)
    business_snapshot = {
        "occupancy_rate": _ratio(pms_summary.get("occupancy_rate")),
        "actual_sales_rate": _ratio(pms_summary.get("actual_sales_rate")),
        "actual_sold_rooms": pms_summary.get("actual_sold_rooms"),
        "pending_arrival_rooms": pms_summary.get("pending_arrival_rooms"),
        "today_available": pms_summary.get("today_available"),
        "total_available": pms_summary.get("available_rooms"),
        "living_count": pms_summary.get("occupied_rooms"),
        "sales_status_note": pms_summary.get("sales_status_note"),
        "sales_rate_source": pms_summary.get("sales_rate_source"),
        "room_type_inventory": room_type_inventory,
    }
    price_summary = summarize_competitor_price_for_business_snapshot(
        {
            "decisions": db_data["decisions"],
            "comparisons": compares,
            "summary_by_room_type": business_compare.get("summary_by_room_type", []),
            "own_prices": own_rows,
        },
        business_snapshot,
    )
    rule_advice = [
        *generate_business_snapshot_advice(business_snapshot, price_summary),
        *_exhibition_rule_advice(exhibition_context),
    ]
    if pms_summary.get("sales_rate_source") != "order_api":
        rule_advice = [line.replace("真实销售率", "库存售卖率估算") for line in rule_advice]
    raw_memory_lessons = _recall_business_memory(own_hotel_key, stay_dates[0])
    memory_lessons = _sanitize_memory_lessons(raw_memory_lessons)
    previous_report = _read_json(Path("output/hotel_sets") / own_hotel_key / stay_dates[0] / "business_snapshot_report.json")
    previous_action_context = _previous_business_action_context(
        previous_report, own_hotel_key, stay_dates[0], generated_at, run_id,
    )
    hermes_input = {
        "hotel_key": own_hotel_key,
        "hotel_name": own_hotels[0].name if own_hotels else own_hotel_key,
        "stay_date": stay_dates[0],
        "business_snapshot": business_snapshot,
        "price_summary": price_summary,
        "recommendations": recommendations,
        "exhibition_context": exhibition_context,
        "event_signal": exhibition_context.get("event_signal", {}),
        "future_event_demand_context": future_event_demand_context,
        "analysis_context": {
            "generated_at": generated_at,
            "current_period": {"morning": "上午", "afternoon": "下午", "evening": "晚间"}[actual_slot],
            "pms_change": pms_change,
            "pms_history": _pms_history_context(
                pms_summary, db_data.get("recent_pms_statuses") or [],
                db_data.get("historical_pms_statuses") or [],
                room_type_inventory,
            ),
        },
        "memory_lessons": memory_lessons,
        "previous_action_context": previous_action_context,
    }
    analysis_diagnostics = _new_hermes_analysis_diagnostics(own_hotel_key, run_id)
    try:
        hermes_decision = generate_hermes_business_analysis(
            hermes_input,
            price_summary,
            diagnostics=analysis_diagnostics,
        )
        prior_actions = previous_action_context.get("actions") or []
        hermes_decision["actions"] = [
            action for action in hermes_decision["actions"]
            if not any(
                action["kind"] == prior.get("kind")
                and action.get("room_name") == prior.get("room_name")
                and action.get("evidence") == prior.get("evidence")
                for prior in prior_actions
            )
        ]
        hermes_analysis = "\n".join(_render_hermes_decision(hermes_decision))
        analysis_source = "hermes"
    except Exception as exc:
        hermes_decision = None
        hermes_analysis = "系统经营分析暂不可用，已改用数据规则。"
        analysis_source = "rules"
        analysis_diagnostics.setdefault("fallback_reason", "unknown_analysis_error")
        analysis_diagnostics["hermes_error_type"] = type(exc).__name__
        alerts.append(hermes_analysis)
        logger.warning(
            "Hermes snapshot fallback hotel=%s run_id=%s reason=%s returncode=%s stderr=%s",
            own_hotel_key,
            run_id,
            analysis_diagnostics["fallback_reason"],
            analysis_diagnostics.get("hermes_returncode"),
            analysis_diagnostics.get("hermes_stderr_summary", ""),
        )

    report = {
        "metadata": {
            "run_id": run_id,
            "generated_at": generated_at,
            "snapshot_date": stay_dates[0],
            "days": days,
            "scope": "today" if days == 1 else "range",
            "status": "success" if not alerts else "warning",
            "own_hotel_key": own_hotel_key,
            "slot": actual_slot,
            "scheduled_slot": slot,
            "memory_context_used": len(memory_lessons),
            "memory_context_filtered": len(raw_memory_lessons) - len(memory_lessons),
        },
        "hotels": {
            "own": [_hotel_json(hotel) for hotel in own_hotels],
            "competitors": [_hotel_json(hotel) for hotel in competitor_hotels],
        },
        "pms_summary": pms_summary,
        "pms_change": pms_change,
        "pms_room_type_details": room_type_inventory,
        "ota_own_room_details": own_rows,
        "ota_competitor_room_details": competitor_rows,
        "competitor_supply_alerts": competitor_supply_alerts,
        "price_compare_details": compares,
        "compare_warnings": compare_warnings,
        "price_summary": price_summary,
        "exhibition_context": exhibition_context,
        "future_event_demand_context": future_event_demand_context,
        "recommendations": recommendations,
        "business_advice": rule_advice,
        "hermes_decision": hermes_decision,
        "hermes_analysis": hermes_analysis,
        "analysis_source": analysis_source,
        "analysis_diagnostics": analysis_diagnostics,
        "evening_warning": evening_warning,
        "alerts": alerts,
    }
    report["feishu_message"] = render_feishu_message(report)

    output = Path(output_path)
    ensure_dir(output.parent)
    payload = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    output.write_text(payload, encoding="utf-8")
    _archive_report(output, report, payload)
    return report


def _load_hotel_scope(config_path: str, own_hotel_key: str) -> tuple[list[HotelConfig], list[HotelConfig]]:
    try:
        from src.multi_hotel import _hotel_config, load_competitors_for_hotel, load_hotel
        from src.multi_hotel import _connect as connect

        with connect() as conn:
            own = load_hotel(conn, own_hotel_key)
            if own:
                return [_hotel_config(own)], [_hotel_config(row) for row in load_competitors_for_hotel(conn, own_hotel_key)]
    except Exception:
        logger.warning("酒店配置数据库不可用，已回退 hotels.json")
    hotels = load_hotels(config_path)
    own_hotels = [hotel for hotel in hotels if hotel.role == "own" and hotel.key == own_hotel_key]
    own = own_hotels[0] if own_hotels else None
    competitor_keys = set(own.competitors) if own and own.competitors else {hotel.key for hotel in hotels if hotel.role == "competitor"}
    return own_hotels, [hotel for hotel in hotels if hotel.key in competitor_keys]


def _previous_business_action_context(
    report: dict[str, Any], hotel_key: str, stay_date: str, generated_at: str, run_id: str,
) -> dict[str, Any]:
    metadata = report.get("metadata")
    decision = report.get("hermes_decision")
    if not isinstance(metadata, dict) or not isinstance(decision, dict):
        return {}
    if metadata.get("own_hotel_key") != hotel_key or metadata.get("snapshot_date") != stay_date or metadata.get("run_id") == run_id:
        return {}
    try:
        previous_time = datetime.fromisoformat(str(metadata.get("generated_at")))
        current_time = datetime.fromisoformat(generated_at)
        if previous_time.tzinfo is None or current_time.tzinfo is None or previous_time >= current_time:
            return {}
    except (TypeError, ValueError):
        return {}
    actions = decision.get("actions")
    if not isinstance(actions, list):
        return {}
    return {
        "generated_at": metadata["generated_at"],
        "actions": [
            {key: action.get(key) for key in ("kind", "room_name", "suggested_price", "evidence", "reason", "recheck")}
            for action in actions[:2] if isinstance(action, dict)
        ],
        "execution_confirmed": False,
    }


def _archive_report(output: Path, report: dict[str, Any], payload: str) -> None:
    if output.is_absolute() or not output.parts or output.parts[0] != "output":
        return
    metadata = report.get("metadata") if isinstance(report.get("metadata"), dict) else {}
    snapshot_date = str(metadata.get("snapshot_date") or "").strip()
    run_id = str(metadata.get("run_id") or "").strip()
    if not snapshot_date or not run_id:
        return
    archive = Path("output/archive") / snapshot_date / run_id / "business_snapshot_report.json"
    ensure_dir(archive.parent)
    archive.write_text(payload, encoding="utf-8")


def _recall_business_memory(hotel_key: str, stay_date: str) -> list[str]:
    try:
        rows = recall_hotel(
            hotel_key,
            f"{stay_date} 经营快照 调价规则 库存异常 竞品价差 特殊策略 推送建议",
            limit=5,
        )
    except Exception:
        return []
    return [str(row.get("content") or "").strip()[:500] for row in rows if str(row.get("content") or "").strip()]


_EVENT_MEMORY_MARKERS = ("展会", "开幕", "闭幕", "举行中", "ChinaJoy", "数码互动娱乐")


def _sanitize_memory_lessons(lessons: list[str]) -> list[str]:
    """Keep general operating lessons out of the structured event channel."""
    cleaned: list[str] = []
    for lesson in lessons or []:
        text = str(lesson or "").strip()[:500]
        if not text:
            continue
        folded = text.casefold()
        if any(marker.casefold() in folded for marker in _EVENT_MEMORY_MARKERS):
            continue
        cleaned.append(text)
    return cleaned


def render_feishu_message(report: dict[str, Any]) -> str:
    metadata = report.get("metadata", {})
    pms = report.get("pms_summary", {})
    own_hotels = report.get("hotels", {}).get("own", [])
    slot = "event" if metadata.get("scheduled_slot") == "event" else str(metadata.get("slot") or "morning")
    titles = {"morning": "上午经营快照", "afternoon": "下午经营变化", "evening": "晚间经营收口", "event": "事件联动经营快照"}
    advice_lines = _business_advice_lines(report)
    sold_rooms_label = "今日已售房" if pms.get("sales_rate_source") == "order_api" else "库存估算已售房"
    sales_rate_label = pms.get("sales_rate_label") or "销售率"
    overview_lines = [
        f"在住：{_room_count_text(pms.get('occupied_rooms'))}｜在住率：{_rate_text(pms.get('occupancy_rate'))}",
        f"销售：{sold_rooms_label}{_room_count_text(pms.get('actual_sold_rooms'))}｜{sales_rate_label}：{_rate_text(pms.get('actual_sales_rate'))}",
        f"库存：可售{_room_count_text(pms.get('available_rooms'))}｜{_inventory_status(pms.get('available_rooms'))}",
    ]
    if pms.get("pending_arrival_rooms") not in (None, ""):
        overview_lines.append(f"未入住预订：{_room_count_text(pms.get('pending_arrival_rooms'))}")
    inventory_attention_lines = _inventory_attention_lines(report)
    recent_event_lines = _recent_event_lines(
        report.get("exhibition_context", {}),
        report.get("future_event_demand_context", {}),
    )
    lines = [
        f"🏨 {_names(own_hotels)}｜{titles.get(slot, '经营快照')}",
        f"📅 {metadata.get('snapshot_date', '暂无')} {_snapshot_time_text(metadata, slot)}",
        "",
        "一、经营概况",
        *overview_lines,
        *inventory_attention_lines,
        "",
        "二、竞品价格对比",
        *[f"⚠️ {warning}" for warning in report.get("compare_warnings") or []],
        *_competitor_supply_alert_lines(report.get("competitor_supply_alerts") or []),
        *_price_summary_lines(report.get("price_summary", {})),
        "",
        "三、近期展会与经营提醒",
        *recent_event_lines,
    ]
    if advice_lines:
        lines.extend(["", "四、经营建议", *advice_lines])
    if slot == "evening" and report.get("evening_warning"):
        lines[3:3] = [f"⚠️ 过去30分钟库存变化较快：{report['evening_warning']}", ""]
    return "\n".join(lines)


def _future_event_demand_lines(context: dict[str, Any]) -> list[str]:
    """Backward-compatible entry point for callers that only have future context."""
    return _recent_event_lines({}, context)


def _recent_event_lines(today_context: dict[str, Any], future_context: dict[str, Any]) -> list[str]:
    today_context = today_context if isinstance(today_context, dict) else {}
    future_context = future_context if isinstance(future_context, dict) else {}
    today_events = [
        event for event in (today_context.get("events") or [])
        if isinstance(event, dict) and str(event.get("event_phase") or "") == "active"
    ]
    events = [event for event in (future_context.get("events") or []) if isinstance(event, dict)]
    signals = [item for item in (future_context.get("demand_signals") or []) if isinstance(item, dict)]
    advice = [item for item in (future_context.get("price_control_advice") or []) if isinstance(item, dict)]
    lines = _today_event_lines(today_events)
    groups = _future_event_groups(events)
    anchor_date = _context_anchor_date(future_context)
    near_groups = [group for group in groups if not anchor_date or group["start"] <= anchor_date + timedelta(days=7)]
    later_groups = [
        group for group in groups
        if group not in near_groups and group["impact_level"] in {"high", "unknown"}
    ]
    if near_groups:
        selected_near_groups = sorted(
            near_groups,
            key=lambda group: (
                0 if group["impact_level"] == "high" else 1,
                group["start"],
                group["venue"],
            ),
        )[:3]
        selected_near_groups.sort(key=lambda group: (group["start"], group["venue"]))
        lines.extend(["", "未来7天：", *_future_group_lines(selected_near_groups)])
    if later_groups:
        pinned_count = sum(_is_temporarily_pinned_group(group) for group in later_groups)
        display_count = max(1, pinned_count)
        lines.extend(["", "未来8—30天重点活动：", *_future_group_lines(later_groups[:display_count])])
    if not groups:
        lines.extend(["", "未来30天：暂无与本店明显相关的已确认展会或大型活动。"])
    lines.extend(["", _event_business_reminder(advice, signals, bool(groups))])
    return lines


def _today_event_lines(events: list[dict[str, Any]]) -> list[str]:
    if not events:
        return ["今天：暂无与本店明显相关的进行中活动。"]
    groups = _future_event_groups(events)
    if not groups:
        return ["今天：暂无与本店明显相关的进行中活动。"]
    groups.sort(
        key=lambda group: (
            0 if _is_temporarily_pinned_group(group) else 1,
            {"high": 0, "medium": 1, "low": 2, "unknown": 3}.get(group["impact_level"], 3),
            group["start"],
            group["venue"],
        )
    )
    lines = ["今天："]
    return [*lines, *_future_group_lines(groups[:2])]


def _future_event_groups(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        start = _event_date(event.get("event_start_date"))
        end = _event_date(event.get("event_end_date"))
        if start is None or end is None:
            continue
        impact_level = str(
            event.get("impact_level")
            or event.get("hotel_impact_level")
            or event.get("effective_event_level")
            or "unknown"
        ).strip().lower()
        if not _lodging_eligible(event, impact_level):
            continue
        duration_days = (end - start).days + 1
        category = str(event.get("category") or "").strip().lower()
        if duration_days > 20 and impact_level != "high" and category not in {
            "esports", "sports", "performance", "acg",
        }:
            continue
        key = (
            start.isoformat(),
            end.isoformat(),
            _show(event.get("display_venue") or event.get("venue")),
            impact_level,
        )
        grouped[key].append(event)
    result = [
        {
            "start": date.fromisoformat(start),
            "end": date.fromisoformat(end),
            "venue": venue,
            "impact_level": impact_level,
            "events": rows,
        }
        for (start, end, venue, impact_level), rows in grouped.items()
    ]
    return sorted(
        result,
        key=lambda group: (
            0 if _is_temporarily_pinned_group(group) else 1,
            group["start"],
            0 if group["impact_level"] == "high" else 1,
            group["venue"],
        ),
    )


def _is_temporarily_pinned_group(group: dict[str, Any]) -> bool:
    if date.today() > _TEMPORARY_PINNED_EVENT_UNTIL:
        return False
    return any(
        str(event.get("event_name") or "").strip() in _TEMPORARY_PINNED_EVENT_NAMES
        for event in group.get("events") or []
        if isinstance(event, dict)
    )


def _future_group_lines(groups: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for group in groups:
        lines.extend(
            [
                f"• {_event_date_text(group['start'], group['end'])}｜{group['venue']}｜{_event_impact_text(group['impact_level'])}",
                f"  活动：{_event_names_text(group['events'])}",
            ]
        )
        notes = list(dict.fromkeys(str(event.get("impact_note") or "").strip()
                                   for event in group["events"] if event.get("impact_note")))
        if notes:
            lines.append("  依据：" + "；".join(note.replace("\n", " ")[:160] for note in notes[:2]))
    return lines


def _event_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _context_anchor_date(context: dict[str, Any]) -> date | None:
    return _event_date(context.get("anchor_date"))


def _event_date_text(start: date, end: date) -> str:
    start_text = f"{start.month}月{start.day}日"
    return start_text if start == end else f"{start_text}—{end.month}月{end.day}日"


def _event_names_text(events: list[dict[str, Any]]) -> str:
    names = list(dict.fromkeys(
        (
            str(event.get("event_name") or "").strip()
            + ("（多源证实，待官方核验）" if event.get("source_confidence") == "corroborated" else "")
        )
        for event in events
    ))
    names = [name for name in names if name]
    chinese_names = [name for name in names if any("\u4e00" <= char <= "\u9fff" for char in name)]
    names = chinese_names or names
    names.sort(key=lambda name: (len(name), name))
    selected = names[:3]
    suffix = "等" if len(names) > len(selected) else ""
    return "、".join(selected) + suffix if selected else "活动名称待确认"


def _event_impact_text(impact_level: Any) -> str:
    return {
        "high": "本店影响较大",
        "medium": "本店影响一般",
        "low": "本店影响较小",
        "unknown": "本店影响待判断",
    }.get(str(impact_level or "").strip().lower(), "本店影响待判断")


def _event_business_reminder(advice: list[dict[str, Any]], signals: list[dict[str, Any]], has_events: bool) -> str:
    if advice:
        item = advice[0]
        event_name = _show(item.get("event_name"))
        return f"经营提醒：{event_name}期间需求有上升迹象，请人工核对订单、库存和竞品价格；不自动调价。"
    if has_events:
        return "经营提醒：以上活动仅作需求参考；是否调价仍需人工核对订单、库存和竞品价格。"
    if signals:
        return "经营提醒：系统会继续观察库存变化；该信号不代表已确认订单，也不自动调价。"
    return "经营提醒：暂无需要特别处理的活动或需求变化。"


def _legacy_future_event_demand_lines(context: dict[str, Any]) -> list[str]:
    events = context.get("events") if isinstance(context, dict) else []
    signals = context.get("demand_signals") if isinstance(context, dict) else []
    advice = context.get("price_control_advice") if isinstance(context, dict) else []
    events = events if isinstance(events, list) else []
    signals = signals if isinstance(signals, list) else []
    advice = advice if isinstance(advice, list) else []
    if not events and not signals and not advice:
        return ["未来30天暂无需要特别处理的事件需求信号。"]
    lines: list[str] = []
    for item in advice[:3]:
        if not isinstance(item, dict):
            continue
        lines.append(f"⚠️ 重点｜{_show(item.get('stay_date'))} {item.get('event_name')}｜{item.get('demand_state')}（信号 {item.get('demand_score')}）")
        lines.append(f"建议：{item.get('recommended_action')}")
    advised_dates = {str(item.get("stay_date")) for item in advice if isinstance(item, dict)}
    signal_by_date = {str(item.get("stay_date")): item for item in signals if isinstance(item, dict)}
    for event in events[:3]:
        if not isinstance(event, dict):
            continue
        event_dates = [key for key in signal_by_date if str(event.get("event_start_date")) <= key <= str(event.get("event_end_date"))]
        if any(key in advised_dates for key in event_dates):
            continue
        demand_text = "当前未观察到明显起量"
        if event_dates:
            demand_text = str(signal_by_date[event_dates[0]].get("state") or demand_text)
        lines.append(f"关注｜{_show(event.get('event_start_date'))}至{_show(event.get('event_end_date'))} {event.get('event_name')}｜{demand_text}")
    event_ranges = [(str(item.get("event_start_date")), str(item.get("event_end_date"))) for item in events if isinstance(item, dict)]
    for signal in signals[:3]:
        if not isinstance(signal, dict):
            continue
        stay_date = str(signal.get("stay_date"))
        if any(start <= stay_date <= end for start, end in event_ranges):
            continue
        lines.append(f"需求观察｜{stay_date} {signal.get('state')}（信号 {signal.get('primary_score')}）")
    lines.append("说明：需求信号来自可售库存变化代理，不等同于已确认订单；系统不自动调价。")
    return lines


def _snapshot_time_text(metadata: dict[str, Any], slot: str) -> str:
    generated_at = str(metadata.get("generated_at") or "").strip()
    if generated_at:
        try:
            return datetime.fromisoformat(generated_at.replace("Z", "+00:00")).strftime("%H:%M")
        except ValueError:
            pass
    return {"morning": "10:00", "afternoon": "14:30", "evening": "18:30"}.get(slot, "")


def _evening_warning_summary(hotel_key: str, stay_date: str) -> str:
    if psycopg is None:
        return ""
    try:
        with psycopg.connect(_load_database_url(), row_factory=psycopg.rows.dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT message FROM occupancy_alerts
                    WHERE hotel_key = %s AND stay_date = %s
                      AND COALESCE(current_collected_at, alert_at) >= NOW() - INTERVAL '30 minutes'
                      AND COALESCE(raw_context->>'severity', 'warning') <> 'critical'
                      AND COALESCE(raw_context->>'status', raw_context->>'event_status', '')
                          NOT IN ('recovered', 'stable', 'filtered', 'suppressed', 'confirmed_no_push')
                    GROUP BY message
                    ORDER BY MAX(current_collected_at) DESC NULLS LAST, MAX(alert_at) DESC
                    LIMIT 3
                    """,
                    (hotel_key, stay_date),
                )
                return "；".join(str(row["message"]) for row in cursor.fetchall() if row.get("message"))
    except Exception:
        return ""


_HERMES_ALLOWED_EVIDENCE_KINDS = {
    "review_price": {"comparison"},
    "hold_price": {"comparison"},
    "prioritize_sales": {"inventory"},
    "trial_price": {"decision"},
    "prepare_event": {"future_event"},
    "review_future_demand": {"demand_signal"},
}


def _raise_hermes_analysis_error(
    details: dict[str, Any],
    reason: str,
    code: str,
    message: str,
) -> None:
    details["fallback_reason"] = reason
    details["hermes_failure_code"] = code
    details["hermes_failure_message"] = message
    raise BusinessSnapshotError(message)


def _parse_hermes_analysis_output(
    raw_output: str, details: dict[str, Any], evidence: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    raw_output, output_wrapper = normalize_hermes_json_output(raw_output)
    details["output_wrapper"] = output_wrapper
    if not raw_output:
        _raise_hermes_analysis_error(
            details, "empty_output", "empty_output", "Hermes 分析未返回有效结果",
        )
    if len(raw_output) > 4000:
        _raise_hermes_analysis_error(
            details, "too_long_output", "output_too_long", "Hermes 分析输出过长",
        )

    forbidden = ("/home/", "C:\\", "DATABASE_URL", "API Key", "webhook", "系统提示词", "Skill", "Agent")
    if any(marker.lower() in raw_output.lower() for marker in forbidden):
        _raise_hermes_analysis_error(
            details, "forbidden_content", "forbidden_content", "Hermes 分析包含禁止内容",
        )

    parsed = False
    try:
        payload = json.loads(raw_output)
        parsed = True
    except json.JSONDecodeError:
        payload = None
        if raw_output.startswith("{") and raw_output.endswith("]"):
            try:
                payload = json.loads(raw_output + "}")
                parsed = True
                details["output_recovery"] = "missing_final_brace"
            except json.JSONDecodeError:
                pass
        if not parsed:
            _raise_hermes_analysis_error(
                details, "invalid_json", "invalid_json", "Hermes 分析 JSON 格式无效",
            )
    if not isinstance(payload, dict) or set(payload) != {"actions"}:
        _raise_hermes_analysis_error(
            details, "invalid_json_schema", "json_shape", "Hermes 分析 JSON 结构无效",
        )
    items = payload["actions"]
    if not isinstance(items, list) or len(items) > 2:
        _raise_hermes_analysis_error(
            details, "invalid_json_schema", "action_count", "Hermes 分析条目数量无效",
        )
    allowed = _HERMES_ALLOWED_EVIDENCE_KINDS
    actions: list[dict[str, Any]] = []
    seen_targets: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {"kind", "target", "evidence", "reason", "recheck"}:
            _raise_hermes_analysis_error(
                details, "invalid_json_schema", "action_fields", "Hermes 动作结构无效",
            )
        kind, target = item["kind"], item["target"]
        refs = item["evidence"]
        if not isinstance(kind, str) or kind not in allowed:
            _raise_hermes_analysis_error(
                details, "invalid_json_schema", "action_kind", "Hermes 动作类型无效",
            )
        if not isinstance(target, str) or target not in evidence:
            _raise_hermes_analysis_error(
                details, "invalid_json_schema", "action_target", "Hermes 动作目标无效",
            )
        if evidence[target]["kind"] not in allowed[kind]:
            _raise_hermes_analysis_error(
                details, "invalid_json_schema", "target_evidence_kind", "动作 target 的证据类型不允许",
            )
        if kind in {"review_price", "hold_price"}:
            own, rival = evidence[target].get("own_price"), evidence[target].get("competitor_price")
            if own is None or rival is None or abs(own - rival) < max(20, own * 0.05):
                _raise_hermes_analysis_error(
                    details, "unsafe_business_analysis", "weak_price_signal", "价差不足以驱动价格动作",
                )
        if target in seen_targets:
            _raise_hermes_analysis_error(
                details, "invalid_json_schema", "duplicate_target", "Hermes 动作目标重复",
            )
        if not isinstance(refs, list) or not 1 <= len(refs) <= 3:
            _raise_hermes_analysis_error(
                details, "invalid_json_schema", "evidence_list", "Hermes evidence 列表无效",
            )
        if any(not isinstance(ref, str) or ref not in evidence for ref in refs):
            _raise_hermes_analysis_error(
                details, "invalid_json_schema", "unknown_evidence", "Hermes 引用了不存在的证据",
            )
        if target not in refs:
            _raise_hermes_analysis_error(
                details, "invalid_json_schema", "target_missing_from_evidence", "evidence 必须包含 target",
            )
        referenced = [evidence[ref] for ref in refs]
        if kind == "prioritize_sales":
            room_baseline_ok = (evidence[target].get("baseline_delta") or 0) >= 2
            overview = next((fact for fact in referenced if fact.get("kind") == "overview"), {})
            history = next((fact for fact in referenced if fact.get("kind") == "history"), {})
            current_rate = _number(overview.get("actual_sales_rate"))
            baseline_rate = _number(history.get("median_sales_rate"))
            store_pace_ok = (
                (evidence[target].get("available") or 0) >= 3
                and current_rate is not None and baseline_rate is not None
                and baseline_rate - current_rate >= 0.08
            )
            if not room_baseline_ok and not store_pace_ok:
                _raise_hermes_analysis_error(
                    details, "unsafe_business_analysis", "missing_inventory_signal",
                    "该房型没有房型历史偏离，也没有全店售卖节奏偏慢的组合证据",
                )
        if kind == "prepare_event" and not evidence[target].get("event_actionable"):
            _raise_hermes_analysis_error(
                details, "unsafe_business_analysis", "event_not_actionable",
                "活动日期、影响或对应入住日信号不足以形成今日准备动作",
            )
        if kind == "review_future_demand" and not evidence[target].get("confirmed"):
            _raise_hermes_analysis_error(
                details, "unsafe_business_analysis", "demand_signal_not_confirmed",
                "未来入住日库存代理信号尚未达到复核动作条件",
            )
        if kind not in {"prepare_event", "review_future_demand"} and any(
            evidence[ref]["kind"] in {"future_event", "demand_signal"} for ref in refs
        ):
            _raise_hermes_analysis_error(
                details, "invalid_json_schema", "future_evidence_action",
                "未来活动或入住日信号只能支持对应日期的准备动作",
            )
        if kind in {"hold_price", "review_price", "trial_price"} and any(evidence[ref]["kind"] == "reference" for ref in refs):
            _raise_hermes_analysis_error(
                details, "invalid_json_schema", "reference_price", "参考比价不能驱动价格动作",
            )
        if kind in {"hold_price", "prioritize_sales"} and evidence[target].get("available") == 0:
            _raise_hermes_analysis_error(
                details, "invalid_json_schema", "sold_out_action", "售罄房型不生成价格动作",
            )
        target_room = evidence[target].get("room_name")
        if target_room and kind in {"hold_price", "review_price", "trial_price"} and any(
            fact.get("available") == 0 and _room_name_key(fact.get("room_name")) == _room_name_key(target_room)
            for fact in evidence.values()
        ):
            _raise_hermes_analysis_error(
                details, "invalid_json_schema", "sold_out_room_action", "已知售罄房型不能生成价格动作",
            )
        if target_room and kind in {"hold_price", "review_price", "trial_price"} and any(
            evidence[ref].get("room_name")
            and _room_name_key(evidence[ref]["room_name"]) != _room_name_key(target_room)
            for ref in refs
        ):
            _raise_hermes_analysis_error(
                details, "invalid_json_schema", "cross_room_evidence", "不同房型的证据不能混用",
            )
        if evidence[target]["kind"] == "overview" and any(
            evidence[ref]["kind"] in {"comparison", "decision", "inventory"} for ref in refs
        ):
            _raise_hermes_analysis_error(
                details, "invalid_json_schema", "overview_action", "单房型证据不能驱动全店价格动作",
            )
        reason = " ".join(str(item["reason"]).split()) if isinstance(item["reason"], str) else ""
        recheck = " ".join(str(item["recheck"]).split()) if isinstance(item["recheck"], str) else ""
        evidence_text = " ".join(evidence[ref]["text"] for ref in dict.fromkeys(refs))
        reason_claims = reason.replace("不等同成交", "")
        confirmed_future_signal = any(
            fact.get("confirmed") or fact.get("has_confirmed_signal") for fact in referenced
        )
        checked_reason_claims = reason_claims
        if (
            kind in {"prepare_event", "review_future_demand"}
            and confirmed_future_signal
            and "持续起量" in evidence_text
        ):
            checked_reason_claims = checked_reason_claims.replace("持续起量", "")
        unsupported_trend_claim = bool(re.search(
            r"价差.{0,4}(扩大|拉大|收窄)|消耗|转化|失守|成交|价格敏感|需求.{0,4}(增加|上升|下降)|持续",
            checked_reason_claims,
        ))
        if (
            not reason or len(reason) > 180 or not recheck or len(recheck) > 120
            or _has_ungrounded_reason_numbers(reason + " " + recheck, evidence_text)
            or re.search(r"新增订单|订单增加|展会带动|活动带动|已执行|已调价|调价见效", reason)
            or unsupported_trend_claim
            or re.search(r"各房型|最大|最多|最明显|第一|最高|最低", reason)
            or ("展会" in reason and not any(evidence[ref]["kind"] in {"event", "future_event"} for ref in refs))
            or reason in {"库存偏紧", "库存需要复查", "价差需要复查", "关注库存变化"}
        ):
            _raise_hermes_analysis_error(
                details, "unsafe_business_analysis", "unsafe_reason", "Hermes 动作理由超出证据",
            )
        seen_targets.add(target)
        actions.append({
            "kind": kind, "target": target,
            "evidence": [evidence[ref]["text"] for ref in dict.fromkeys(refs)],
            "room_name": evidence[target].get("room_name"),
            "target_label": evidence[target].get("target_label"),
            "stay_date": evidence[target].get("stay_date"),
            "suggested_price": evidence[target].get("suggested_price"),
            "reason": reason, "recheck": recheck,
        })
    details["fallback_reason"] = None
    details["hermes_failure_code"] = None
    details["hermes_failure_message"] = None
    return {"actions": actions}


def _business_evidence(snapshot: dict[str, Any], price_summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    business = snapshot.get("business_snapshot") or {}
    change = (snapshot.get("analysis_context") or {}).get("pms_change") or {}
    facts: dict[str, dict[str, Any]] = {}
    available = _number(business.get("today_available"))
    if available is not None:
        facts["overview"] = {
            "kind": "overview", "sales_rate_source": business.get("sales_rate_source"),
            "text": f"全店今日可售{_room_count_text(available)}（库存口径，不代表订单数）",
        }
        rate = _ratio(business.get("actual_sales_rate"))
        if rate is not None:
            facts["overview"]["actual_sales_rate"] = rate
            label = "真实销售率" if business.get("sales_rate_source") == "order_api" else "库存售卖率估算"
            facts["overview"]["text"] += f"，{label}{rate * 100:.1f}%"
    delta = _number(change.get("available_rooms_delta"))
    if delta is not None:
        facts["change"] = {
            "kind": "change",
            "text": f"{_evidence_time(change.get('previous_collected_at'))}至{_evidence_time(change.get('current_collected_at'))}全店可售净变化{delta:+g}间（不等于新增订单）",
        }
    for index, row in enumerate(business.get("room_type_inventory") or []):
        if not isinstance(row, dict):
            continue
        count = _number(row.get("available"))
        name = str(row.get("roomType") or row.get("room_type") or "").strip()
        if count is None or not isfinite(count) or count < 0 or not name:
            continue
        facts[f"inventory:{index}"] = {
            "kind": "inventory", "room_name": name, "available": count,
            "text": f"{name}今日可售{_room_count_text(count)}",
        }
        history = ((snapshot.get("analysis_context") or {}).get("pms_history") or {})
        baseline = (history.get("room_baselines") or {}).get(_room_inventory_key(row)) or {}
        median_available = _number(baseline.get("median_available"))
        if median_available is not None and baseline.get("sample_count", 0) >= 3:
            facts[f"inventory:{index}"]["baseline_delta"] = count - median_available
            facts[f"inventory:{index}"]["text"] += (
                f"；同星期同时段{baseline['sample_count']}天可售中位数"
                f"{_room_count_text(median_available)}，偏离{_signed_number(count - median_available)}间（仅供相对比较）"
            )
        recent = ((snapshot.get("analysis_context") or {}).get("pms_history") or {}).get("recent_samples") or []
        points = []
        for sample in recent:
            for past_room in sample.get("room_type_inventory") or []:
                if _room_inventory_key(past_room) == _room_inventory_key(row):
                    past_available = _number(past_room.get("available"))
                    if past_available is not None:
                        try:
                            stamp = datetime.fromisoformat(str(sample.get("collected_at")))
                            time_text = stamp.astimezone(timezone(timedelta(hours=8))).strftime("%H:%M") if stamp.tzinfo else stamp.strftime("%H:%M")
                        except ValueError:
                            continue
                        points.append(f"{time_text}为{past_available:g}间")
                    break
        if len(points) >= 2:
            facts[f"inventory:{index}"]["text"] += "；近期可售：" + "、".join(points[-2:])
    for index, row in enumerate(price_summary.get("comparisons") or []):
        if not isinstance(row, dict):
            continue
        own, rival = _number(row.get("own_price")), _number(row.get("competitor_price"))
        name = str(row.get("own_room_name") or "").strip()
        if own is None or rival is None or not isfinite(own) or not isfinite(rival) or own <= 0 or rival <= 0 or not name:
            continue
        facts[f"comparison:{index}"] = {
            "kind": "comparison", "room_name": _verified_pms_room_name(name, business.get("room_type_inventory") or []) or name,
            "own_price": own, "competitor_price": rival,
            "text": f"{name}我方挂牌{_money(own)}，直接可比竞品{_money(rival)}，价差{_money(abs(own - rival))}（{'我方高' if own > rival else '我方低' if own < rival else '同价'}）",
        }
    for index, row in enumerate(price_summary.get("reference_comparisons") or []):
        if isinstance(row, dict) and row.get("own_room_name"):
            facts[f"reference:{index}"] = {
                "kind": "reference", "room_name": row["own_room_name"],
                "text": f"参考比价：{row['own_room_name']}{_money(row.get('own_price'))}，竞品{_money(row.get('competitor_price'))}；{row.get('room_difference_note') or '产品属性不同'}（不驱动调价）",
            }
    for index, row in enumerate(snapshot.get("recommendations") or []):
        if not isinstance(row, dict):
            continue
        own, suggested = _number(row.get("own_price")), _number(row.get("suggested_adjustment"))
        if (
            not row.get("has_direct_comparison") or own is None or suggested is None
            or not isfinite(own) or not isfinite(suggested) or own <= 0 or suggested <= 0
            or suggested < round(own * 0.95, 2) or own == suggested
            or row.get("action") in {None, "", "人工确认", "人工复核", "维持", "保持", "维持价格", "保持价格", "暂不调整"}
        ):
            continue
        facts[f"decision:{index}"] = {
            "kind": "decision", "room_name": row.get("room_name") or row.get("room_category"),
            "suggested_price": suggested,
            "text": f"{row.get('room_name') or row.get('room_category')}现价{_money(own)}，系统候选试探价{_money(suggested)}（须人工确认）",
        }
    history = ((snapshot.get("analysis_context") or {}).get("pms_history") or {})
    baseline_rate = _number(history.get("median_sales_rate"))
    if baseline_rate is not None and history.get("sample_count", 0) >= 3:
        facts["history"] = {
            "kind": "history", "median_sales_rate": baseline_rate,
            "text": f"本店同星期同时段{history['sample_count']}天的同口径售卖率中位数{baseline_rate * 100:.1f}%（未控制活动或促销差异）",
        }
    signal = _compact_current_event_signal(snapshot.get("event_signal"))
    if signal.get("rules_available"):
        for index, row in enumerate(signal["events"]):
            facts[f"event:{index}"] = {
                "kind": "event", "text": f"今日{row.get('event_name')}进行中，本店影响{_event_level_text(row.get('effective_event_level'))}（仅为需求背景）",
            }
    future = snapshot.get("future_event_demand_context") or {}
    anchor_date = _event_date(future.get("anchor_date"))
    linked_signal_dates: set[str] = set()
    for index, row in enumerate(future.get("events") or []):
        start = _event_date(row.get("event_start_date"))
        end = _event_date(row.get("event_end_date"))
        duration_days = (end - start).days + 1 if start and end and end >= start else None
        duration_valid = duration_days is not None and duration_days <= 45
        matched_signals = [
            signal for signal in (row.get("matched_demand_signals") or [])
            if isinstance(signal, dict)
        ]
        confirmed_signals = [signal for signal in matched_signals if signal.get("confirmed")]
        near_high_impact = bool(
            duration_valid and anchor_date and start
            and 0 <= (start - anchor_date).days <= 7
            and str(row.get("impact_level") or "").lower() == "high"
        )
        actionable = bool(duration_valid and (confirmed_signals or near_high_impact))
        event_names = _names_from_event(row)
        text = (
            f"{row.get('event_start_date')}至{row.get('event_end_date')}，{event_names}，"
            f"本店影响{_event_level_text(row.get('impact_level'))}"
        )
        if not duration_valid:
            text += "；活动日期跨度异常，本批不能据此生成动作"
        elif confirmed_signals:
            signal_dates = list(dict.fromkeys(str(signal.get("stay_date")) for signal in confirmed_signals))
            linked_signal_dates.update(signal_dates)
            text += (
                f"；对应入住日{'、'.join(signal_dates[:3])}的库存代理信号达到持续起量条件"
                "（多个观察窗口为正，不等同已确认订单）"
            )
        elif matched_signals:
            text += "；对应入住日仅有未确认的库存代理观察信号"
        else:
            text += "；尚无对应入住日的确认库存代理信号"
        facts[f"future_event:{index}"] = {
            "kind": "future_event", "text": text,
            "target_label": f"{row.get('event_start_date')} {event_names}",
            "stay_date": confirmed_signals[0].get("stay_date") if confirmed_signals else row.get("event_start_date"),
            "event_actionable": actionable,
            "has_confirmed_signal": bool(confirmed_signals),
            "event_duration_valid": duration_valid,
        }
    for index, row in enumerate(future.get("demand_signals") or []):
        if not isinstance(row, dict) or not row.get("confirmed"):
            continue
        stay_date = str(row.get("stay_date") or "").strip()
        if not stay_date or stay_date in linked_signal_dates:
            continue
        state = str(row.get("state") or "持续起量")
        facts[f"demand_signal:{index}"] = {
            "kind": "demand_signal", "confirmed": True,
            "stay_date": stay_date, "target_label": f"{stay_date}入住日",
            "text": (
                f"{stay_date}未来入住日库存代理信号达到{state}条件，多个观察窗口为正"
                "（不等同已确认订单，尚未归因于具体活动）"
            ),
        }
    return facts


def _verified_pms_room_name(ota_name: str, inventories: list[dict[str, Any]]) -> str | None:
    base = re.split(r"[（(【\[]", ota_name, maxsplit=1)[0].strip()
    matches = [
        str(row.get("roomType") or row.get("room_type") or "").strip()
        for row in inventories if isinstance(row, dict)
        and _room_name_key(row.get("roomType") or row.get("room_type")) == _room_name_key(base)
    ]
    return matches[0] if len(matches) == 1 else None


_REASON_NUMBER = re.compile(r"(?<![\d.])[+-]?\d+(?:\.\d+)?(?:[%％])?")


def _number_tokens(text: str) -> set[tuple[float, bool]]:
    tokens: set[tuple[float, bool]] = set()
    for raw in _REASON_NUMBER.findall(str(text or "")):
        is_percent = raw.endswith(("%", "％"))
        numeric = raw[:-1] if is_percent else raw
        try:
            tokens.add((float(numeric), is_percent))
        except ValueError:
            continue
    return tokens


def _has_ungrounded_reason_numbers(text: str, evidence_text: str) -> bool:
    allowed = _number_tokens(evidence_text)
    return any(token not in allowed for token in _number_tokens(text))


def _names_from_event(row: dict[str, Any]) -> str:
    return "、".join(str(name) for name in (row.get("event_names") or [])[:2]) or "活动待确认"


def _model_evidence_catalog(evidence: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    comparisons = sorted(
        (item for item in evidence.items() if item[1]["kind"] == "comparison"),
        key=lambda item: abs(item[1]["own_price"] - item[1]["competitor_price"]), reverse=True,
    )[:5]
    compared_rooms = {_room_name_key(item[1].get("room_name")) for item in comparisons}
    inventories = [item for item in evidence.items() if item[1]["kind"] == "inventory"]
    matching = [item for item in inventories if _room_name_key(item[1].get("room_name")) in compared_rooms]
    remaining = sorted(
        (item for item in inventories if item not in matching and item[1].get("available", 0) > 0),
        key=lambda item: (abs(item[1].get("baseline_delta") or 0), item[1]["available"]), reverse=True,
    )[:max(0, 8 - len(matching))]
    retained = [*comparisons, *matching[:8], *remaining]
    for kind, limit in (
        ("overview", 1), ("change", 1), ("history", 1), ("decision", 2),
        ("event", 2), ("future_event", 3), ("demand_signal", 3),
    ):
        retained.extend([item for item in evidence.items() if item[1]["kind"] == kind][:limit])
    return dict(retained)


def _render_hermes_decision(decision: dict[str, Any] | None) -> list[str]:
    if not isinstance(decision, dict):
        return []
    lines: list[str] = []
    titles = {
        "review_price": "复核同日同权益报价，决定是否调整",
        "hold_price": "暂守当前价格",
        "prioritize_sales": "优先展示该房型并承接适配需求",
        "trial_price": "人工确认后可试调至{price}",
        "prepare_event": "今天核对活动入住日的房型、在售价格与低价库存安排",
        "review_future_demand": "今天复核该入住日的房型、在售价格与低价库存安排",
    }
    for index, item in enumerate(decision.get("actions") or [], start=1):
        title = titles[item["kind"]].format(price=_money(item.get("suggested_price")))
        room_name = item.get("room_name") or ""
        target_label = item.get("target_label") or room_name or "本店"
        facts = [fact.removeprefix(room_name) if room_name else fact for fact in item["evidence"]]
        lines.extend([
            f"{index}. {target_label}：{title}。",
            f"   判断：{item['reason'].rstrip('。；;')}。依据：{'；'.join(facts)}。复查：{item['recheck'].rstrip('。；;')}。",
        ])
    return lines


_HERMES_UNSAFE_ANALYSIS_MARKERS = ("调价", "上调", "下调", "涨价", "降价", "小降", "中降", "强降", "维持", "保持", "建议", "推荐", "加价", "减价", "现价", "¥", "%")


def _hermes_analysis_is_safe(text: str) -> bool:
    value = re.sub(r"(?m)^\s*\d+[.)]\s*", "", str(text or ""))
    return not re.search(r"\d", value) and not any(marker in value for marker in _HERMES_UNSAFE_ANALYSIS_MARKERS)


def _hermes_evidence_limits(snapshot: dict[str, Any]) -> dict[str, Any]:
    business = snapshot.get("business_snapshot") if isinstance(snapshot.get("business_snapshot"), dict) else {}
    analysis_context = snapshot.get("analysis_context") if isinstance(snapshot.get("analysis_context"), dict) else {}
    change = analysis_context.get("pms_change") if isinstance(analysis_context.get("pms_change"), dict) else {}
    sales_source = str(business.get("sales_rate_source") or "unknown")
    limits = [
        "单个采集区间只能说明区间净变化，不能证明加速、持续趋势或变化原因。",
        "待入住变化不能单独区分办理入住、取消、改期、锁房或房态调整。",
        "当前挂牌价不是该采集区间内订单的确认成交价，不能据此判断价格敏感度。",
        "只有活动日期与需求信号日期重合且信号 confirmed=true 时，才允许归因于活动。",
    ]
    dependent_metrics: list[list[str]] = []
    if sales_source != "order_api":
        dependent_metrics.append(["actual_sold_rooms", "actual_sales_rate", "total_available"])
        limits.insert(0, "已售、售卖率和可售库存属于同一库存估算关系，不能作为相互独立的证据。")
    return {
        "sales_rate_source": sales_source,
        "dependent_metric_groups": dependent_metrics,
        "observed_change_fields": sorted(key for key, value in change.items() if value not in (None, [], {})),
        "interpretation_limits": limits,
    }


def _compact_current_event_signal(signal: Any) -> dict[str, Any]:
    signal = signal if isinstance(signal, dict) else {}
    relevant_events = []
    for event in signal.get("events") or []:
        if not isinstance(event, dict):
            continue
        impact = str(event.get("effective_event_level") or event.get("hotel_impact_level") or "").lower()
        if impact not in {"medium", "high"}:
            continue
        relevant_events.append({
            "event_name": event.get("event_name"),
            "venue": event.get("venue"),
            "effective_event_level": impact,
        })
    return {
        "active": bool(relevant_events),
        "rules_available": bool(signal.get("rules_available")),
        "events": relevant_events[:3],
    }


def _compact_future_event_context(context: Any) -> dict[str, Any]:
    context = context if isinstance(context, dict) else {}
    events = [item for item in (context.get("events") or []) if isinstance(item, dict)]
    signals = [item for item in (context.get("demand_signals") or []) if isinstance(item, dict)]
    groups = _future_event_groups(events)[:5]
    compact_signals = []
    for signal in signals:
        stay_date = _event_date(signal.get("stay_date"))
        if stay_date is None or not signal.get("confirmed"):
            continue
        compact_signals.append({
            "stay_date": stay_date.isoformat(),
            "state": signal.get("state"),
            "confirmed": True,
            "persistent": bool(signal.get("persistent")),
        })
    compact_signals.sort(key=lambda item: item["stay_date"])
    compact_events: list[dict[str, Any]] = []
    for group in groups:
        matched_signals = []
        for signal in signals:
            signal_date = _event_date(signal.get("stay_date"))
            if signal_date is None or not (group["start"] <= signal_date <= group["end"]):
                continue
            matched_signals.append({
                "stay_date": signal_date.isoformat(),
                "state": signal.get("state"),
                "confirmed": bool(signal.get("confirmed")),
                "persistent": bool(signal.get("persistent")),
            })
        compact_events.append({
            "event_names": list(dict.fromkeys(
                str(item.get("event_name") or "").strip()
                for item in group.get("events") or []
                if isinstance(item, dict) and str(item.get("event_name") or "").strip()
            ))[:3],
            "venue": group["venue"],
            "event_start_date": group["start"].isoformat(),
            "event_end_date": group["end"].isoformat(),
            "impact_level": group["impact_level"],
            "matched_demand_signals": matched_signals[:5],
            "attribution_allowed": any(item["confirmed"] for item in matched_signals),
        })
    return {
        "anchor_date": context.get("anchor_date"),
        "events": compact_events,
        "demand_signals": compact_signals[:5],
        "price_control_advice": [
            item for item in (context.get("price_control_advice") or []) if isinstance(item, dict)
        ][:3],
    }


def _prepare_hermes_model_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    model_snapshot = {
        key: value
        for key, value in snapshot.items()
        if key not in {"exhibition_context", "stay_date"}
    }
    model_snapshot["event_signal"] = _compact_current_event_signal(model_snapshot.get("event_signal"))
    model_snapshot["future_event_demand_context"] = _compact_future_event_context(
        model_snapshot.get("future_event_demand_context")
    )
    analysis_context = dict(model_snapshot.get("analysis_context") or {})
    analysis_context["evidence_limits"] = _hermes_evidence_limits(model_snapshot)
    model_snapshot["analysis_context"] = analysis_context
    return model_snapshot


def generate_hermes_business_analysis(
    snapshot: dict[str, Any],
    price_summary: dict[str, Any] | None,
    *,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    details = diagnostics if diagnostics is not None else {}
    hermes_cli = Path(os.getenv("HERMES_CLI", str(Path.home() / "bin/hermes-hotel-analysis")))
    details["hermes_executable"] = str(hermes_cli)
    details["hermes_python"] = os.getenv(
        "HERMES_ANALYSIS_PYTHON",
        str(Path.home() / ".hermes/runtimes/current-analysis/bin/python"),
    )
    total_started_at = time.monotonic()
    details["hermes_version"] = _hermes_version(hermes_cli)
    if not hermes_cli.is_file():
        details["fallback_reason"] = "hermes_cli_not_found"
        raise BusinessSnapshotError("Hermes CLI 不可用")

    # 展会日期与阶段由本地展会区块统一渲染；模型只接收已关联日期并去重后的影响信号。
    model_snapshot = _prepare_hermes_model_snapshot(snapshot)
    evidence = _business_evidence(model_snapshot, price_summary or {})
    context = dict(model_snapshot.get("analysis_context") or {})
    history = dict(context.get("pms_history") or {})
    structured_input = {
        "stay_date": snapshot.get("stay_date"),
        "sales_rate_source": (model_snapshot.get("business_snapshot") or {}).get("sales_rate_source"),
        "pms_change": context.get("pms_change") or {},
        "history": {key: history.get(key) for key in ("sample_count", "median_sales_rate", "limitation")},
        "memory_lessons": model_snapshot.get("memory_lessons") or [],
        "previous_action_context": model_snapshot.get("previous_action_context") or {},
        "evidence_catalog": _model_evidence_catalog(evidence),
    }
    prompt = "\n".join(
        [
            "你是酒店经营参谋。综合本店库存、可用历史基线、有效直接竞品价差、近期活动及未来入住日库存代理信号，选择今天最值得店长处理的零至两件事。缺少竞品只限制竞品驱动的价格动作，不能据此忽略有证据的销售安排或未来入住日准备。仅复述库存或泛泛说关注时输出空数组。",
            "只输出 JSON：{\"actions\":[{\"kind\":\"review_price\",\"target\":\"comparison:0\",\"evidence\":[\"comparison:0\"],\"reason\":\"为什么先做\",\"recheck\":\"下一批核对什么\"}]}。target 和 evidence 都填 evidence_catalog 中的 ID，evidence 须含 target，最多三个。",
            "动作与证据类型必须严格遵守：review_price 和 hold_price 只能使用有效 comparison；trial_price 只能使用系统给出的 decision；prioritize_sales 使用 inventory，并须具备同房型历史偏离，或同时引用 overview 与 history 证明全店售卖节奏明显低于可比时段；prepare_event 只能使用标记为可行动的 future_event；review_future_demand 只能使用已确认的 demand_signal。change、event、reference 只能作为背景依据。",
            "价格动作不能跨房型、不能引用未来活动、未来入住日信号或参考比价；已售罄房型不调价或促销。prepare_event 与 review_future_demand 是今天执行的准备工作，作用于证据中的未来入住日期，不等同于自动调价。库存减少不等于新增订单，挂牌价不等于成交价，库存估算销售率不等于订单率；历史少于三天不能作基线。只有日期跨度有效、门店影响明确且临近的高影响活动，或活动日期与已确认库存代理信号重合时，才能生成活动准备动作。",
            "reason 说明为什么今天处理该房型或入住日期，180字以内；recheck 写下一批能验证的变化条件，120字以内。每条动作的 reason 和 recheck 中所有数字都必须出现在该条 evidence 引用的证据文本中；提及房型余量须引用同房型 inventory，提及候选试探价须引用同房型 decision，无法合法引用时删去该数字。只能核对可获取的挂牌价和房态，不得要求竞品成交价；不得根据库存变化推断订单、价格效果或活动因果，不能说价差扩大、库存消耗、需求上升，不能声称在全部房型中排名。已确认的未来入住日信号只表示多个库存观察窗口达到规则条件，仍不等同订单。上一批动作不代表已执行，不重复没有新证据的动作。不要调用工具或输出内部信息。",
            json.dumps(structured_input, ensure_ascii=False, default=str),
        ]
    )
    retryable_reasons = {"empty_output", "too_long_output", "invalid_json", "invalid_json_schema", "unsafe_business_analysis"}
    details["hermes_attempt_details"] = []
    details["hermes_prompt_chars"] = len(prompt)
    details["hermes_failure_code"] = None
    details["hermes_failure_message"] = None
    details.setdefault("analysis_retry_used", False)
    for attempt in range(2):
        details["hermes_attempts"] = attempt + 1
        details["hermes_prompt_chars"] = len(prompt)
        attempt_started_at = time.monotonic()
        remaining_seconds = 300 - (time.monotonic() - total_started_at)
        if remaining_seconds <= 0:
            details["hermes_duration_ms"] = int((time.monotonic() - total_started_at) * 1000)
            details["fallback_reason"] = "analysis_timeout"
            details["hermes_failure_code"] = "analysis_timeout"
            details["hermes_failure_message"] = "Hermes 分析调用超时"
            details["hermes_attempt_details"].append({
                "attempt": attempt + 1,
                "prompt_chars": len(prompt),
                "duration_ms": int((time.monotonic() - attempt_started_at) * 1000),
                "returncode": None,
                "stdout_chars": 0,
                "stderr_chars": 0,
                "fallback_reason": "analysis_timeout",
            })
            raise BusinessSnapshotError("Hermes 分析调用超时")
        try:
            result = subprocess.run(
                [
                    str(hermes_cli),
                    "--ignore-rules",
                    "--oneshot",
                    prompt,
                ],
                cwd="/tmp",
                text=True,
                capture_output=True,
                timeout=remaining_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            attempt_duration_ms = int((time.monotonic() - attempt_started_at) * 1000)
            details["hermes_duration_ms"] = int((time.monotonic() - total_started_at) * 1000)
            details["hermes_stderr_summary"] = _summarize_hermes_stderr(exc.stderr)
            details["fallback_reason"] = "analysis_timeout"
            details["hermes_failure_code"] = "analysis_timeout"
            details["hermes_failure_message"] = "Hermes 分析调用超时"
            details["hermes_attempt_details"].append({
                "attempt": attempt + 1,
                "prompt_chars": len(prompt),
                "duration_ms": attempt_duration_ms,
                "returncode": None,
                "stdout_chars": len(exc.stdout or ""),
                "stderr_chars": len(exc.stderr or ""),
                "fallback_reason": "analysis_timeout",
            })
            raise BusinessSnapshotError("Hermes 分析调用超时") from exc
        except OSError as exc:
            attempt_duration_ms = int((time.monotonic() - attempt_started_at) * 1000)
            details["hermes_duration_ms"] = int((time.monotonic() - total_started_at) * 1000)
            details["fallback_reason"] = "hermes_cli_not_found"
            details["hermes_failure_code"] = "os_error"
            details["hermes_failure_message"] = "Hermes 分析调用失败"
            details["hermes_attempt_details"].append({
                "attempt": attempt + 1,
                "prompt_chars": len(prompt),
                "duration_ms": attempt_duration_ms,
                "returncode": None,
                "stdout_chars": 0,
                "stderr_chars": 0,
                "fallback_reason": "hermes_cli_not_found",
            })
            raise BusinessSnapshotError("Hermes 分析调用失败") from exc

        attempt_duration_ms = int((time.monotonic() - attempt_started_at) * 1000)
        details["hermes_duration_ms"] = int((time.monotonic() - total_started_at) * 1000)
        details["hermes_returncode"] = result.returncode
        details["hermes_stderr_summary"] = _summarize_hermes_stderr(result.stderr)
        attempt_detail = {
            "attempt": attempt + 1,
            "prompt_chars": len(prompt),
            "duration_ms": attempt_duration_ms,
            "returncode": result.returncode,
            "stdout_chars": len(result.stdout or ""),
            "stderr_chars": len(result.stderr or ""),
        }
        details["hermes_attempt_details"].append(attempt_detail)
        if result.returncode:
            details["fallback_reason"] = _classify_hermes_failure(result.stderr)
            details["hermes_failure_code"] = "process_exit"
            details["hermes_failure_message"] = "Hermes 分析进程返回非零状态"
            attempt_detail["fallback_reason"] = details["fallback_reason"]
            raise BusinessSnapshotError("Hermes 分析未返回有效结果")
        try:
            return _parse_hermes_analysis_output(result.stdout, details, evidence)
        except BusinessSnapshotError as exc:
            attempt_detail["fallback_reason"] = details.get("fallback_reason")
            attempt_detail["hermes_failure_code"] = details.get("hermes_failure_code")
            attempt_detail["hermes_failure_message"] = details.get("hermes_failure_message")
            if attempt == 0 and details.get("fallback_reason") in retryable_reasons:
                details["analysis_retry_used"] = True
                prompt += f"\n上次输出未通过校验：{exc}。请修正后仅返回规定的 JSON，不复述错误。"
                continue
            raise



def _new_hermes_analysis_diagnostics(hotel_key: str, run_id: str) -> dict[str, Any]:
    return {
        "hotel_key": hotel_key,
        "run_id": run_id,
        "hermes_executable": None,
        "hermes_python": None,
        "hermes_version": None,
        "hermes_returncode": None,
        "hermes_duration_ms": None,
        "hermes_error_type": None,
        "hermes_stderr_summary": "",
        "fallback_reason": None,
        "hermes_failure_code": None,
        "hermes_failure_message": None,
        "hermes_tool_count": 0,
        "hermes_attempts": 0,
        "analysis_retry_used": False,
        "output_recovery": None,
        "hermes_prompt_chars": None,
        "hermes_attempt_details": [],
    }


def _hermes_version(hermes_cli: Path) -> str | None:
    try:
        result = subprocess.run(
            [str(hermes_cli), "--version"],
            cwd="/tmp",
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in result.stdout.splitlines():
        if line.startswith("Hermes Agent v"):
            return line.removeprefix("Hermes Agent v").strip()
    return None


def _summarize_hermes_stderr(stderr: str | bytes | None) -> str:
    if not stderr:
        return ""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    compact = " ".join(stderr.split())[:500]
    return _HERMES_SECRET_VALUE.sub(r"\1=<redacted>", compact)


def _classify_hermes_failure(stderr: str | bytes | None) -> str:
    summary = _summarize_hermes_stderr(stderr).lower()
    if "importerror" in summary or "cannot import name" in summary:
        return "hermes_cli_import_error"
    if "rate limit" in summary or "429" in summary:
        return "provider_rate_limited"
    if "authentication" in summary or "unauthorized" in summary or "api key" in summary:
        return "provider_auth_error"
    if "timeout" in summary or "timed out" in summary:
        return "provider_timeout"
    if "bad request" in summary or "400" in summary:
        return "provider_bad_request"
    if "config" in summary:
        return "hermes_cli_config_error"
    return "unknown_analysis_error"


def _read_database_snapshot(
    stay_dates: list[str],
    own_hotels: list[HotelConfig],
    competitor_hotels: list[HotelConfig],
    alerts: list[str],
    own_hotel_key: str = "",
    run_id: str | None = None,
) -> dict[str, Any]:
    data = {
        "pms_status": None,
        "previous_pms_status": None,
        "recent_pms_statuses": [],
        "historical_pms_statuses": [],
        "own_prices": [],
        "competitor_prices": [],
        "decisions": [],
        "exhibitions": [],
        "exhibition_available": False,
        "exhibition_rule_available": False,
        "future_event_demand_context": {"lookahead_days": 30, "events": [], "demand_signals": [], "price_control_advice": []},
        "room_availability_history": [],
    }
    if psycopg is None:
        alerts.append("数据库驱动不可用，已尝试使用 latest JSON 兜底。")
        return data
    own_keys = [hotel.key for hotel in own_hotels]
    competitor_keys = [hotel.key for hotel in competitor_hotels]
    try:
        with psycopg.connect(_load_database_url(), row_factory=psycopg.rows.dict_row) as connection:
            with connection.cursor() as cursor:
                pms_rows = _fetch_recent_pms(cursor, own_keys, stay_dates[0])
                data["pms_status"] = pms_rows[0] if pms_rows else None
                data["previous_pms_status"] = pms_rows[1] if len(pms_rows) > 1 else None
                data["recent_pms_statuses"] = pms_rows
                if pms_rows:
                    try:
                        with connection.transaction():
                            data["historical_pms_statuses"] = _fetch_same_time_pms(cursor, pms_rows[0])
                    except Exception:
                        logger.warning("经营建议的历史同时段样本暂不可用")
                data["own_prices"] = _fetch_prices(cursor, own_keys, stay_dates, run_id)
                data["competitor_prices"] = _fetch_prices(cursor, competitor_keys, stay_dates, run_id)
                data["room_availability_history"] = _fetch_room_availability_history(
                    cursor,
                    [*own_keys, *competitor_keys],
                    stay_dates[0],
                    run_id,
                )
                data["decisions"] = _fetch_latest_decisions(cursor, own_keys, stay_dates, run_id)
                if own_hotel_key:
                    try:
                        with connection.transaction():
                            from src.event_demand_context import load_future_event_demand_context

                            data["future_event_demand_context"] = load_future_event_demand_context(
                                cursor, own_hotel_key, date.fromisoformat(stay_dates[0])
                            )
                    except Exception:
                        alerts.append("未来事件与需求联动信息暂不可用。")
                try:
                    with connection.transaction():
                        data["exhibitions"] = _fetch_hotel_exhibitions(
                            cursor,
                            own_hotel_key,
                            stay_dates[0],
                        )
                    data["exhibition_available"] = True
                    data["exhibition_rule_available"] = True
                except Exception:
                    logger.warning("酒店展会影响规则不可用，回退为未用于建议的全局展会信息", exc_info=True)
                    try:
                        with connection.transaction():
                            data["exhibitions"] = _fetch_background_event_occurrences(cursor, stay_dates[0])
                        for event in data["exhibitions"]:
                            event["global_event_demand_level"] = _normalize_event_level(event.get("demand_level"))
                            event["hotel_impact_level"] = "unknown"
                            event["effective_event_level"] = "unknown"
                        data["exhibition_available"] = True
                        alerts.append("展会本店影响规则暂不可用，未将展会作为经营建议依据。")
                    except Exception:
                        alerts.append("展会信息暂不可用。")
    except Exception:
        alerts.append("数据库快照读取不可用，已使用 latest JSON 兜底。")
    return data


def _fetch_hotel_exhibitions(cursor: Any, own_hotel_key: str, stay_date: str) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT e.venue, e.event_name, e.event_start_date, e.event_end_date,
               e.stay_start_date, e.stay_end_date, e.category, e.demand_level,
               r.impact_level AS hotel_impact_level,
               c.raw #>> '{entry,impact_level}' AS search_impact_level,
               c.raw -> 'entry' -> 'hotel_relevance' AS hotel_relevance
        FROM event_occurrences e
        LEFT JOIN event_candidates c ON c.id = e.source_row AND c.canonical_event_id = e.id
          AND e.source_kind = 'official_candidate'
        LEFT JOIN hotel_venue_impact_rules r
          ON r.own_hotel_key = %s AND r.venue = e.venue
        WHERE e.is_business_active = TRUE
          AND e.canonical_status <> 'cancelled'
          AND COALESCE(e.stay_start_date, e.event_start_date) <= %s
          AND COALESCE(e.stay_end_date, e.event_end_date) >= %s
        ORDER BY e.event_start_date, e.event_name
        """,
        (own_hotel_key, stay_date, stay_date),
    )
    rows = [dict(row) for row in cursor.fetchall()]
    venue_rules = {row["venue"]: str(row["hotel_impact_level"]) for row in rows if row.get("hotel_impact_level")}
    if any(not row.get("hotel_impact_level") for row in rows):
        venue_rules = _hotel_venue_rules(cursor, own_hotel_key)
    events = []
    for row in rows:
        event = dict(row)
        event["hotel_impact_level"], event["impact_note"], event["display_venue"] = _snapshot_impact(
            event, own_hotel_key, venue_rules
        )
        event = _with_hotel_event_impact(event)
        if _lodging_eligible(event, event["hotel_impact_level"]):
            events.append(event)
    return events


def _fetch_background_event_occurrences(cursor: Any, stay_date: str) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT e.venue, e.event_name, e.event_start_date, e.event_end_date,
               e.stay_start_date, e.stay_end_date, e.category, e.demand_level,
               NULL::text AS hotel_impact_level
        FROM event_occurrences e
        WHERE e.is_business_active = TRUE
          AND e.canonical_status <> 'cancelled'
          AND COALESCE(e.stay_start_date, e.event_start_date) <= %s
          AND COALESCE(e.stay_end_date, e.event_end_date) >= %s
        ORDER BY e.event_start_date, e.event_name
        """,
        (stay_date, stay_date),
    )
    return [_with_hotel_event_impact(dict(row)) for row in cursor.fetchall()]


def _normalize_event_level(value: Any) -> str:
    return {
        "high": "high",
        "very_high": "high",
        "高": "high",
        "medium": "medium",
        "中": "medium",
        "low": "normal",
        "低": "normal",
        "normal": "normal",
    }.get(str(value or "normal").strip().lower(), "normal")


def _with_hotel_event_impact(event: dict[str, Any]) -> dict[str, Any]:
    global_level = _normalize_event_level(event.get("global_event_demand_level") or event.get("demand_level"))
    impact_level = str(event.get("hotel_impact_level") or "").strip().lower()
    if impact_level not in {"high", "medium", "low"}:
        effective_level = "unknown"
        impact_level = "unknown"
    else:
        levels = ("normal", "medium", "high")
        score = {level: index for index, level in enumerate(levels)}[global_level]
        score += {"high": 1, "medium": 0, "low": -1}[impact_level]
        effective_level = levels[max(0, min(len(levels) - 1, score))]
    event["global_event_demand_level"] = global_level
    event["hotel_impact_level"] = impact_level
    event["effective_event_level"] = effective_level
    event["demand_level"] = effective_level
    return _json_safe(event)


def _fetch_latest_pms(cursor: Any, hotel_keys: list[str], stay_date: str) -> dict[str, Any] | None:
    if not hotel_keys:
        return None
    rows = _fetch_recent_pms(cursor, hotel_keys, stay_date)
    return rows[0] if rows else None


def _fetch_recent_pms(cursor: Any, hotel_keys: list[str], stay_date: str) -> list[dict[str, Any]]:
    if not hotel_keys:
        return []
    cursor.execute(
        """
        SELECT hotel_key, stay_date, room_total, occupied_rooms, available_rooms, sold_rooms,
               booking_count, occupancy_rate, collected_at, raw
        FROM own_daily_status
        WHERE hotel_key = %s AND stay_date = %s
        ORDER BY collected_at DESC
        LIMIT 4
        """,
        (hotel_keys[0], stay_date),
    )
    return list(cursor.fetchall())


def _fetch_same_time_pms(cursor: Any, current: dict[str, Any]) -> list[dict[str, Any]]:
    collected = datetime.fromisoformat(str(current["collected_at"]))
    if collected.tzinfo is None:
        return []
    local_time = collected.astimezone(timezone(timedelta(hours=8))).time().isoformat()
    stay_date = str(current["stay_date"])
    cursor.execute(
        """
        SELECT * FROM (
            SELECT DISTINCT ON (stay_date)
                   hotel_key, stay_date, room_total, occupied_rooms, available_rooms,
                   sold_rooms, booking_count, occupancy_rate, collected_at, raw
            FROM own_daily_status
            WHERE hotel_key = %s
              AND stay_date >= %s::date - 42 AND stay_date < %s::date
              AND EXTRACT(ISODOW FROM stay_date) = EXTRACT(ISODOW FROM %s::date)
              AND (collected_at AT TIME ZONE 'Asia/Shanghai')::date = stay_date
              AND ABS(EXTRACT(EPOCH FROM
                  ((collected_at AT TIME ZONE 'Asia/Shanghai')::time - %s::time))) <= 3600
            ORDER BY stay_date,
                     ABS(EXTRACT(EPOCH FROM
                         ((collected_at AT TIME ZONE 'Asia/Shanghai')::time - %s::time))),
                     collected_at DESC
        ) samples ORDER BY stay_date DESC LIMIT 6
        """,
        (current["hotel_key"], stay_date, stay_date, stay_date, local_time, local_time),
    )
    return list(cursor.fetchall())


def _pms_history_context(
    current: dict[str, Any], recent: list[dict[str, Any]], historical: list[dict[str, Any]],
    current_rooms: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    source = current.get("sales_rate_source")
    comparable = []
    comparable_rows = []
    for row in historical:
        sample = _pms_summary(row, [])
        rate = _ratio(sample.get("actual_sales_rate"))
        if (
            source in {"order_api", "inventory_estimate_not_order_api"}
            and sample.get("sales_rate_source") == source
            and sample.get("room_total") == current.get("room_total")
            and rate is not None
        ):
            comparable.append({"stay_date": str(row.get("stay_date")), "collected_at": str(row.get("collected_at")), "sales_rate": rate})
            comparable_rows.append(row)
    room_baselines = {}
    for room in current_rooms or []:
        key = _room_inventory_key(room)
        if not key:
            continue
        values = []
        for row in comparable_rows:
            matches = [item for item in _pms_room_type_details(row) if _room_inventory_key(item) == key]
            if len(matches) == 1:
                value = _number(matches[0].get("available"))
                if value is not None and isfinite(value) and value >= 0:
                    values.append(value)
        if len(values) >= 3:
            room_baselines[key] = {"sample_count": len(values), "median_available": median(values)}
    recent_samples = []
    for row in reversed(recent[:4]):
        sample = _pms_summary(row, [])
        recent_samples.append({
            "collected_at": str(sample.get("collected_at")),
            "available_rooms": sample.get("available_rooms"),
            "sales_rate_source": sample.get("sales_rate_source"),
            "room_type_inventory": _pms_room_type_details(row),
        })
    return {
        "recent_samples": recent_samples,
        "same_weekday_samples": comparable,
        "sample_count": len(comparable),
        "median_sales_rate": median(sample["sales_rate"] for sample in comparable) if len(comparable) >= 3 else None,
        "room_baselines": room_baselines,
        "limitation": "同店过去六周同星期、采集时间前后一小时、同房量同口径；少于三天不判断快慢，未控制历史活动或促销差异，不能归因。",
    }


def _fetch_prices(cursor: Any, hotel_keys: list[str], stay_dates: list[str], run_id: str | None = None) -> list[dict[str, Any]]:
    if not hotel_keys:
        return []
    hotel_slots = ",".join(["%s"] * len(hotel_keys))
    date_slots = ",".join(["%s"] * len(stay_dates))
    if run_id:
        cursor.execute(
            f"""
            SELECT platform, source_hotel_key AS hotel_key, hotel_name, stay_date,
                   room_name, COALESCE(normalized_room_type, room_category) AS room_category,
                   price, available, breakfast, cancel_policy, source_url,
                   collected_at, COALESCE(raw_payload, raw) AS raw
            FROM ota_room_prices
            WHERE source_hotel_key IN ({hotel_slots})
              AND stay_date IN ({date_slots})
              AND run_id = %s AND available = TRUE AND price > 0
            ORDER BY collected_at DESC, price ASC
            """,
            [*hotel_keys, *stay_dates, run_id],
        )
        return [_json_safe(dict(row)) for row in cursor.fetchall()]
    cursor.execute(
        f"""
        WITH latest_runs AS (
            SELECT DISTINCT ON (source_hotel_key, stay_date)
                   source_hotel_key, stay_date, run_id
            FROM ota_room_prices
            WHERE source_hotel_key IN ({hotel_slots})
              AND stay_date IN ({date_slots})
              AND available = TRUE
              AND price > 0
            ORDER BY source_hotel_key, stay_date, (run_id IS NOT NULL) DESC, collected_at DESC
        )
        SELECT p.platform, p.source_hotel_key AS hotel_key, p.hotel_name, p.stay_date,
               p.room_name, COALESCE(p.normalized_room_type, p.room_category) AS room_category,
               p.price, p.available, p.breakfast, p.cancel_policy, p.source_url,
               p.collected_at, COALESCE(p.raw_payload, p.raw) AS raw
        FROM ota_room_prices p
        JOIN latest_runs latest
          ON latest.source_hotel_key = p.source_hotel_key
         AND latest.stay_date = p.stay_date
        WHERE p.available = TRUE
          AND p.price > 0
          AND ((latest.run_id IS NULL AND p.run_id IS NULL) OR p.run_id = latest.run_id)
        ORDER BY p.collected_at DESC, p.price ASC
        """,
        [*hotel_keys, *stay_dates],
    )
    return [_json_safe(dict(row)) for row in cursor.fetchall()]


def _fetch_room_availability_history(
    cursor: Any,
    hotel_keys: list[str],
    stay_date: str,
    run_id: str | None,
) -> list[dict[str, Any]]:
    """Read the shared batch plus each hotel's immediately preceding OTA batch."""
    if not hotel_keys or not run_id:
        return []
    hotel_slots = ",".join(["%s"] * len(hotel_keys))
    cursor.execute(
        f"""
        WITH previous_runs AS (
            SELECT DISTINCT ON (source_hotel_key) source_hotel_key, run_id
            FROM (
                SELECT source_hotel_key, run_id, MAX(collected_at) AS latest_collected_at
                FROM ota_room_prices
                WHERE source_hotel_key IN ({hotel_slots})
                  AND stay_date = %s
                  AND run_id IS NOT NULL
                  AND run_id <> %s
                GROUP BY source_hotel_key, run_id
            ) ranked
            ORDER BY source_hotel_key, latest_collected_at DESC
        )
        SELECT 'current' AS snapshot, source_hotel_key AS hotel_key, hotel_name,
               stay_date, room_name,
               COALESCE(normalized_room_type, room_category) AS room_category,
               available, price, collected_at
        FROM ota_room_prices
        WHERE source_hotel_key IN ({hotel_slots})
          AND stay_date = %s
          AND run_id = %s
        UNION ALL
        SELECT 'previous' AS snapshot, p.source_hotel_key AS hotel_key, p.hotel_name,
               p.stay_date, p.room_name,
               COALESCE(p.normalized_room_type, p.room_category) AS room_category,
               p.available, p.price, p.collected_at
        FROM ota_room_prices p
        JOIN previous_runs previous
          ON previous.source_hotel_key = p.source_hotel_key
         AND previous.run_id = p.run_id
        WHERE p.stay_date = %s
        """,
        [*hotel_keys, stay_date, run_id, *hotel_keys, stay_date, run_id, stay_date],
    )
    return [_json_safe(dict(row)) for row in cursor.fetchall()]


def _room_variant_for_supply_alert(row: dict[str, Any]) -> tuple[str, str] | None:
    room_type = str(row.get("room_category") or row.get("normalized_room_type") or "").strip()
    if not room_type:
        return None
    try:
        from src.multi_hotel import _core_feature_signature, _room_profile

        return room_type, _core_feature_signature(_room_profile(room_type, row))
    except Exception:
        return None


def _competitor_room_supply_alerts(
    rows: list[dict[str, Any]],
    own_hotel_key: str,
    competitor_hotels: list[HotelConfig],
    stay_date: str,
) -> list[dict[str, Any]]:
    """Return only confirmed, strict room matches that are unavailable in two batches."""
    current = [row for row in rows if row.get("snapshot") == "current" and str(row.get("stay_date")) == stay_date]
    previous = [row for row in rows if row.get("snapshot") == "previous" and str(row.get("stay_date")) == stay_date]
    if not current or not previous:
        return []
    competitor_names = {hotel.key: hotel.name for hotel in competitor_hotels}
    competitor_keys = set(competitor_names)
    own_available = [
        row for row in current
        if row.get("hotel_key") == own_hotel_key
        and row.get("available") is True
        and (_number(row.get("price")) or 0) > 0
    ]
    current_competitors = [row for row in current if row.get("hotel_key") in competitor_keys]
    previous_competitors = [row for row in previous if row.get("hotel_key") in competitor_keys]
    alerts: list[dict[str, Any]] = []
    for own in own_available:
        variant = _room_variant_for_supply_alert(own)
        if variant is None:
            continue
        if any(
            _room_variant_for_supply_alert(row) == variant
            and row.get("available") is True
            and (_number(row.get("price")) or 0) > 0
            for row in current_competitors
        ):
            continue
        for competitor_key in sorted(competitor_keys):
            current_hotel = [row for row in current_competitors if row.get("hotel_key") == competitor_key]
            previous_hotel = [row for row in previous_competitors if row.get("hotel_key") == competitor_key]
            # Other sellable rooms in both batches excludes a whole-hotel closure.
            if not any(row.get("available") is True and (_number(row.get("price")) or 0) > 0 for row in current_hotel):
                continue
            if not any(row.get("available") is True and (_number(row.get("price")) or 0) > 0 for row in previous_hotel):
                continue
            current_target = [row for row in current_hotel if _room_variant_for_supply_alert(row) == variant]
            previous_target = [row for row in previous_hotel if _room_variant_for_supply_alert(row) == variant]
            if not current_target or not previous_target:
                continue
            if not all(row.get("available") is False for row in current_target):
                continue
            if not all(row.get("available") is False for row in previous_target):
                continue
            competitor_room = current_target[0]
            alerts.append({
                "stay_date": stay_date,
                "own_room_name": own.get("room_name"),
                "competitor_hotel_key": competitor_key,
                "competitor_hotel_name": competitor_room.get("hotel_name") or competitor_names.get(competitor_key),
                "competitor_room_name": competitor_room.get("room_name"),
                "room_category": variant[0],
                "core_feature_signature": variant[1],
                "unavailable_confirmations": 2,
            })
    return alerts


def _fetch_latest_decisions(cursor: Any, own_keys: list[str], stay_dates: list[str], run_id: str | None = None) -> list[dict[str, Any]]:
    if not own_keys:
        return []
    if run_id:
        cursor.execute(
            """
            SELECT * FROM price_decisions
            WHERE run_id = %s AND COALESCE(own_hotel_key, hotel_key) = %s AND stay_date = %s
            ORDER BY room_category
            """,
            (run_id, own_keys[0], stay_dates[0]),
        )
        return [_json_safe(dict(item)) for item in cursor.fetchall()]
    cursor.execute(
        """
        SELECT run_id
        FROM price_decisions
        WHERE COALESCE(own_hotel_key, hotel_key) = %s AND stay_date = %s
        ORDER BY decision_at DESC, id DESC
        LIMIT 1
        """,
        (own_keys[0], stay_dates[0]),
    )
    row = cursor.fetchone()
    if not row:
        return []
    cursor.execute("SELECT * FROM price_decisions WHERE run_id = %s AND COALESCE(own_hotel_key, hotel_key) = %s ORDER BY room_category", (row["run_id"], own_keys[0]))
    return [_json_safe(dict(item)) for item in cursor.fetchall()]


def _rooms_from_hermes(hermes: dict[str, Any], stay_dates: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    own: list[dict[str, Any]] = []
    competitors: list[dict[str, Any]] = []
    for target, group in ((own, "own_hotels"), (competitors, "competitor_hotels")):
        for hotel in hermes.get(group, []) or []:
            if not isinstance(hotel, dict):
                continue
            for room in hotel.get("available_rooms", []) or []:
                if isinstance(room, dict) and str(room.get("stay_date") or room.get("date") or "") in stay_dates:
                    target.append(_room_from_hermes(hotel, room))
    return own, competitors


def _room_from_hermes(hotel: dict[str, Any], room: dict[str, Any]) -> dict[str, Any]:
    return {
        "hotel_key": hotel.get("key") or hotel.get("hotel_key"),
        "hotel_name": hotel.get("name") or hotel.get("hotel_name"),
        "stay_date": room.get("stay_date") or room.get("date"),
        "room_name": room.get("room_name"),
        "room_category": room.get("room_category") or room.get("normalized_room_type"),
        "price": room.get("price"),
        "available": True,
        "collected_at": hotel.get("metadata", {}).get("captured_at") or "",
    }


def _pms_summary(status: dict[str, Any] | None, alerts: list[str]) -> dict[str, Any]:
    if not status:
        alerts.append("PMS 房态缺失。")
        return {}
    raw = status.get("raw") if isinstance(status.get("raw"), dict) else {}
    summary = raw.get("summary") if isinstance(raw.get("summary"), dict) else {}
    room_total = _number(status.get("room_total"))
    available_rooms = _number(summary.get("today_available"))
    available_rooms = _number(status.get("available_rooms")) if available_rooms is None else available_rooms
    actual_sold_rooms = _number(summary.get("actual_sold_rooms"))
    if actual_sold_rooms is None and room_total is not None and available_rooms is not None:
        actual_sold_rooms = room_total - available_rooms
    actual_sales_rate = _number(summary.get("actual_sales_rate"))
    if actual_sales_rate is None and actual_sold_rooms is not None and room_total:
        actual_sales_rate = actual_sold_rooms / room_total
    pending_arrival_rooms = _number(summary.get("pending_arrival_rooms"))
    if pending_arrival_rooms is None and actual_sold_rooms is not None:
        occupied_rooms = _number(status.get("occupied_rooms"))
        pending_arrival_rooms = actual_sold_rooms - occupied_rooms if occupied_rooms is not None else None
    if pending_arrival_rooms is not None and pending_arrival_rooms < 0:
        alerts.append("PMS库存售卖估算与当前在住口径不一致，未入住预订暂不可用。")
        pending_arrival_rooms = None
    sales_rate_source = str(raw.get("sales_rate_source") or summary.get("sales_rate_source") or "unknown")
    sales_rate_label = {
        "order_api": "真实销售率",
        "inventory_estimate_not_order_api": "库存售卖率估算",
    }.get(sales_rate_source, "销售率")
    return {
        "room_total": status.get("room_total"),
        "occupied_rooms": status.get("occupied_rooms"),
        "available_rooms": available_rooms,
        "today_available": available_rooms,
        "sold_rooms": status.get("sold_rooms"),
        "actual_sold_rooms": actual_sold_rooms,
        "actual_sales_rate": actual_sales_rate,
        "sales_rate_source": sales_rate_source,
        "sales_rate_label": sales_rate_label,
        "pending_arrival_rooms": pending_arrival_rooms,
        "booking_count": status.get("booking_count"),
        "occupancy_rate": status.get("occupancy_rate"),
        "warning": summary.get("warning") or raw.get("warning"),
        "sales_status_note": summary.get("sales_status_note") or "PMS occupancy_rate 仅代表当前在住率，不等于今晚真实销售率。",
        "collected_at": status.get("collected_at"),
    }


def _pms_change_summary(
    current: dict[str, Any],
    previous: dict[str, Any],
    current_rooms: list[dict[str, Any]],
    previous_rooms: list[dict[str, Any]],
) -> dict[str, Any]:
    if not current or not previous:
        return {}

    def delta(key: str) -> float | None:
        current_value = _number(current.get(key))
        previous_value = _number(previous.get(key))
        return round(current_value - previous_value, 4) if current_value is not None and previous_value is not None else None

    current_rate = _ratio(current.get("actual_sales_rate"))
    previous_rate = _ratio(previous.get("actual_sales_rate"))
    room_changes: list[dict[str, Any]] = []
    previous_by_key = {_room_inventory_key(row): row for row in previous_rooms if _room_inventory_key(row)}
    for row in current_rooms:
        key = _room_inventory_key(row)
        previous_row = previous_by_key.get(key)
        current_available = _number(row.get("available"))
        previous_available = _number(previous_row.get("available")) if previous_row else None
        if current_available is None or previous_available is None or current_available == previous_available:
            continue
        room_changes.append({
            "room_type": row.get("roomType") or row.get("room_type") or key,
            "previous_available": previous_available,
            "current_available": current_available,
            "available_delta": current_available - previous_available,
        })
    room_changes.sort(key=lambda item: abs(item["available_delta"]), reverse=True)
    return {
        "previous_collected_at": previous.get("collected_at"),
        "current_collected_at": current.get("collected_at"),
        "actual_sold_rooms_delta": delta("actual_sold_rooms"),
        "pending_arrival_rooms_delta": delta("pending_arrival_rooms"),
        "available_rooms_delta": delta("available_rooms"),
        "actual_sales_rate_delta_pp": round((current_rate - previous_rate) * 100, 2) if current_rate is not None and previous_rate is not None else None,
        "sales_rate_label": current.get("sales_rate_label") or "销售率",
        "room_type_changes": room_changes[:3],
        "all_room_type_changes": room_changes,
    }


def _room_inventory_key(row: dict[str, Any]) -> str:
    return str(
        row.get("roomTypeID")
        or row.get("room_type_id")
        or row.get("roomType")
        or row.get("room_type")
        or ""
    ).strip()


def _pms_change_lines(change: dict[str, Any]) -> list[str]:
    parts: list[str] = []
    if change.get("actual_sold_rooms_delta") is not None:
        parts.append(f"已售 {_signed_number(change.get('actual_sold_rooms_delta'))} 间")
    if change.get("pending_arrival_rooms_delta") is not None:
        parts.append(f"未入住预订 {_signed_number(change.get('pending_arrival_rooms_delta'))} 间")
    if change.get("available_rooms_delta") is not None:
        parts.append(f"可售 {_signed_number(change.get('available_rooms_delta'))} 间")
    if change.get("actual_sales_rate_delta_pp") is not None:
        parts.append(f"{change.get('sales_rate_label') or '销售率'} {_signed_number(change.get('actual_sales_rate_delta_pp'))} 个百分点")
    lines = ["较上次：" + "；".join(parts)] if parts else []
    for item in change.get("room_type_changes") or []:
        lines.append(
            f"{_show(item.get('room_type'))}：可售 {_show(item.get('previous_available'))}→{_show(item.get('current_available'))} 间"
            f"（{_signed_number(item.get('available_delta'))}）"
        )
    return lines


def _signed_number(value: Any) -> str:
    number = _number(value)
    if number is None:
        return "暂无"
    text = f"{number:.2f}".rstrip("0").rstrip(".")
    return f"+{text}" if number > 0 else text


def _pms_room_type_details(status: dict[str, Any] | None) -> list[dict[str, Any]]:
    raw = status.get("raw") if status else None
    if not isinstance(raw, dict):
        return []
    rows = raw.get("room_type_inventory") or raw.get("summary", {}).get("room_type_inventory") or []
    if not isinstance(rows, list):
        return []
    return [_json_safe(row) for row in rows if isinstance(row, dict)]


def _price_compare_details(stay_dates: list[str], own_rows: list[dict[str, Any]], competitor_rows: list[dict[str, Any]], alerts: list[str]) -> list[dict[str, Any]]:
    own_by_category = _group_valid_prices(own_rows, stay_dates)
    competitor_by_category = _group_valid_prices(competitor_rows, stay_dates)
    if not own_by_category:
        alerts.append("自有 OTA 价格数据不足。")
    if not competitor_by_category:
        alerts.append("竞品 OTA 价格数据不足。")

    details: list[dict[str, Any]] = []
    for category in sorted(set(own_by_category) & set(competitor_by_category)):
        own = min(own_by_category[category], key=lambda row: float(row["price"]))
        competitors = sorted(competitor_by_category[category], key=lambda row: float(row["price"]))
        best = competitors[0]
        own_price = float(own["price"])
        best_price = float(best["price"])
        details.append(
            {
                "stay_date": own.get("stay_date"),
                "room_category": category,
                "own_room_name": own.get("room_name"),
                "own_price": own_price,
                "competitor_min_price": best_price,
                "competitor_hotel_key": best.get("hotel_key"),
                "competitor_hotel_name": best.get("hotel_name"),
                "competitor_room_name": best.get("room_name"),
                "price_diff": round(own_price - best_price, 2),
                "price_diff_pct": round((own_price - best_price) / best_price * 100, 2) if best_price else None,
                "competitor_candidates": competitors,
            }
        )
    return details


def _business_compare_details(payload: dict[str, Any], stay_dates: list[str]) -> list[dict[str, Any]]:
    rows = payload.get("summary_by_room_type") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    details: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or str(row.get("stay_date")) not in stay_dates:
            continue
        own_price = _number(row.get("own_min_price"))
        competitor_price = _number(row.get("competitor_min_price"))
        if own_price is None:
            continue
        details.append({
            "stay_date": row.get("stay_date"),
            "room_category": row.get("normalized_room_type"),
            "variant_key": row.get("variant_key"),
            "own_room_name": row.get("own_room_name") or row.get("selected_raw_room_name") or row.get("normalized_room_type"),
            "own_price": own_price,
            "competitor_min_price": competitor_price,
            "price_diff": row.get("own_vs_competitor_min_diff") if row.get("can_drive_pricing") else None,
            "price_diff_pct": None,
            "match_level": row.get("match_level"),
            "comparison_role": row.get("comparison_role"),
            "can_drive_pricing": bool(row.get("can_drive_pricing")),
            "room_difference_note": row.get("room_difference_note"),
        })
    return details


def _group_valid_prices(rows: list[dict[str, Any]], stay_dates: list[str]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        price = _number(row.get("price"))
        category = str(row.get("room_category") or row.get("normalized_room_type") or "").strip()
        if price is None or price <= 0 or not category or str(row.get("stay_date")) not in stay_dates:
            continue
        item = dict(row)
        item["price"] = price
        grouped[category].append(_json_safe(item))
    return grouped


def _recommendations(
    compares: list[dict[str, Any]],
    pms: dict[str, Any],
    decisions: list[dict[str, Any]],
    alerts: list[str],
    pms_change: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    pms_change = pms_change or {}
    if decisions:
        return [_recommendation_from_decision(item, pms, pms_change) for item in decisions]
    if not compares:
        alerts.append("比价数据不足。")
        return []

    rate = _number(pms.get("actual_sales_rate"))
    rate = _number(pms.get("occupancy_rate")) if rate is None else rate
    rate_label = pms.get("sales_rate_label") or "销售率"
    available = _number(pms.get("available_rooms"))
    pending = _number(pms.get("pending_arrival_rooms"))
    rate = rate / 100 if rate is not None and rate > 1 else rate
    rows: list[dict[str, Any]] = []
    for item in compares:
        action = "维持"
        reason = "当前价差和房态未触发明显调价条件。"
        competitor_price = _number(item.get("competitor_min_price"))
        if competitor_price is not None and rate is not None and rate >= 0.90 and available is not None and available <= 5 and pending is not None and pending > 0:
            action = "谨慎上调" if item["own_price"] <= competitor_price * 1.05 else "维持"
            reason = f"已预订未入住较多，{rate_label}高且只剩少量可售房，按接近满房处理，不降价。"
        elif competitor_price is not None and rate is not None and rate >= 0.95 and (available is None or available <= 5):
            action = "谨慎上调" if item["own_price"] <= competitor_price * 1.05 else "维持"
            reason = f"今日{rate_label}高且库存低，不建议下调。"
        elif competitor_price is not None and item["own_price"] > competitor_price * 1.10:
            action = "谨慎下调"
            reason = "自有价格明显高于最低竞品价，需关注转化。"
        rows.append(
            {
                "stay_date": item.get("stay_date"),
                "room_category": item.get("room_category"),
                "room_name": item.get("own_room_name"),
                "action": action,
                "suggested_adjustment": None,
                "reason": reason,
                "risk": "继续上调可能影响临近预订转化" if action == "维持" else "调价后需观察转化",
                "confidence": "low",
                "manual_review": action != "维持",
                "inventory_change": _room_type_change_for_recommendation(item.get("own_room_name"), pms_change),
                "own_price": item.get("own_price"),
                "competitor_price": item.get("competitor_min_price"),
                "has_direct_comparison": bool(item.get("can_drive_pricing")),
                "competitor_reference": {
                    "hotel_key": item.get("competitor_hotel_key"),
                    "hotel_name": item.get("competitor_hotel_name"),
                    "price": item.get("competitor_min_price"),
                },
            }
        )
    return rows


def _recommendation_from_decision(item: dict[str, Any], pms: dict[str, Any], pms_change: dict[str, Any]) -> dict[str, Any]:
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
    room_name = _decision_room_name(item)
    competitor_price = _number(item.get("competitor_min_price"))
    can_drive_pricing = item.get("can_drive_pricing")
    if can_drive_pricing is None:
        can_drive_pricing = raw.get("can_drive_pricing")
    has_direct_comparison = can_drive_pricing is not False and competitor_price is not None
    manual_reasons: list[str] = []
    if not has_direct_comparison:
        manual_reasons.append("缺少直接可比房型，不输出具体调价")
    if pms.get("warning"):
        manual_reasons.append(str(pms["warning"]))

    action = str(item.get("recommended_action") or "人工确认")
    suggested_adjustment = item.get("recommended_price")
    reason = str(item.get("reason") or "当前数据不足以形成明确价格动作。")
    own_price = _number(item.get("own_current_price") or item.get("current_price"))
    if manual_reasons:
        action = "人工确认"
        suggested_adjustment = None
        reason = "；".join(manual_reasons)
    elif own_price is not None and _number(suggested_adjustment) is not None and _number(suggested_adjustment) < own_price * 0.95:
        action = "小降"
        suggested_adjustment = round(own_price * 0.95, 2)
        reason = "原建议降幅超过单次5%上限，已收敛为5%试探性降幅，请人工确认"

    manual_review = bool(manual_reasons) or action not in {"维持", "保持", "维持价格", "保持价格", "暂不调整"}
    risk = str(item.get("risk_note") or "库存变化仅作估算，不代表真实订单销量")
    return {
        "stay_date": item.get("stay_date"),
        "room_category": item.get("room_category"),
        "room_name": room_name,
        "action": action,
        "suggested_adjustment": suggested_adjustment,
        "reason": reason,
        "risk": risk,
        "confidence": item.get("confidence") or "low",
        "manual_review": manual_review,
        "inventory_change": _room_type_change_for_recommendation(room_name, pms_change),
        "own_price": own_price,
        "competitor_price": competitor_price,
        "has_direct_comparison": has_direct_comparison,
        "competitor_reference": raw.get("competitor_min_source"),
    }


def _room_type_change_for_recommendation(room_name: Any, pms_change: dict[str, Any]) -> dict[str, Any] | None:
    target = _room_name_key(room_name)
    if not target or not isinstance(pms_change, dict):
        return None
    rows = pms_change.get("all_room_type_changes") or pms_change.get("room_type_changes") or []
    for row in rows:
        if isinstance(row, dict) and _room_name_key(row.get("room_type")) == target:
            return row
    return None


def _room_name_key(value: Any) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", str(value or "")).lower()


def _decision_room_name(item: dict[str, Any]) -> str:
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
    comparison = raw.get("business_comparison") if isinstance(raw.get("business_comparison"), dict) else {}
    return str(item.get("own_room_name") or comparison.get("own_room_name") or comparison.get("selected_raw_room_name") or item.get("room_category") or "未命名房型")


def _hotel_json(hotel: HotelConfig) -> dict[str, str]:
    return {"hotel_key": hotel.key, "hotel_name": hotel.name}


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _build_exhibition_context(
    stay_date: str,
    events: list[dict[str, Any]],
    available: bool,
    hotel_rule_available: bool = True,
) -> dict[str, Any]:
    safe_events = [_json_safe(event) for event in annotate_events_for_day(stay_date, events)]
    if hotel_rule_available:
        safe_events = [
            _with_hotel_event_impact(dict(event)) if not event.get("effective_event_level") else event
            for event in safe_events
        ]
    active_events = [event for event in safe_events if event.get("event_phase") == "active"]
    if not available:
        message = "展会信息暂不可用。"
    elif active_events:
        message = "今日进行中展会：有"
        if not hotel_rule_available:
            message += "（本店影响规则暂不可用）"
    elif safe_events:
        message = "今日无进行中展会；以下仅为住宿覆盖信息。"
    else:
        message = "今日未匹配到展会信息。"
    return {
        "stay_date": stay_date,
        "available": available,
        "hotel_rule_available": hotel_rule_available,
        "matched": bool(active_events),
        "message": message,
        "events": safe_events,
        "event_signal": _build_event_signal(active_events, hotel_rule_available),
    }


def _build_event_signal(events: list[dict[str, Any]], hotel_rule_available: bool) -> dict[str, Any]:
    signal_events: list[dict[str, Any]] = []
    for raw_event in events:
        event = dict(raw_event)
        if not event.get("effective_event_level"):
            event = _with_hotel_event_impact(event)
        signal_events.append(event)
    return {
        "active": bool(events),
        "rules_available": hotel_rule_available,
        "events": [
            {
                "event_name": _show(event.get("event_name")),
                "venue": _show(event.get("venue")),
                "global_demand_level": _show(event.get("global_event_demand_level") or event.get("demand_level")),
                "hotel_impact_level": _show(event.get("hotel_impact_level")),
                "effective_event_level": _show(event.get("effective_event_level")),
            }
            for event in signal_events
        ],
    }


def _exhibition_rule_advice(context: dict[str, Any]) -> list[str]:
    signal = context.get("event_signal") if isinstance(context, dict) else {}
    if not isinstance(signal, dict) or not signal.get("active"):
        return []
    events = signal.get("events") if isinstance(signal.get("events"), list) else []
    if not signal.get("rules_available"):
        return ["已匹配进行中展会，但本店场馆影响规则不可用，未将展会作为经营或调价依据。"]
    advice: list[str] = []
    for event in events[:3]:
        if not isinstance(event, dict):
            continue
        name = _show(event.get("event_name"))
        global_level = _show(event.get("global_demand_level"))
        hotel_level = _show(event.get("hotel_impact_level"))
        effective_level = _show(event.get("effective_event_level"))
        if effective_level == "high":
            action = "作为需求偏强背景，结合销售率和库存保护现价，不单独触发涨价。"
        elif effective_level == "medium":
            action = "作为需求背景继续观察，必须结合销售率和库存判断，不单独调价。"
        else:
            action = "仅作背景信息，不用于调价判断。"
        advice.append(
            f"展会参考：{name}；全局需求{global_level}、本店影响{hotel_level}、最终影响{effective_level}。{action}"
        )
    return advice


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ratio(value: Any) -> float | None:
    number = _number(value)
    return number / 100 if number is not None and number > 1 else number


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, (datetime,)):
        return value.isoformat()
    return value


def _names(rows: list[dict[str, Any]]) -> str:
    return "、".join(str(row.get("hotel_name") or row.get("name") or "暂无") for row in rows) or "暂无"


def _show(value: Any) -> str:
    return "暂无" if value in (None, "") else str(value)


def _evidence_time(value: Any) -> str:
    try:
        stamp = datetime.fromisoformat(str(value))
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone(timezone(timedelta(hours=8)))
        return stamp.strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return "上一批" if value in (None, "") else str(value)


def _inventory_attention_lines(report: dict[str, Any]) -> list[str]:
    """Summarize current room-type pressure without inferring orders from deltas."""
    rows = report.get("pms_room_type_details") or []
    sold_out_count = 0
    tight_count = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        available = _number(row.get("available"))
        if available is None:
            continue
        if available <= 0:
            sold_out_count += 1
        elif available <= 2:
            tight_count += 1

    if not sold_out_count and not tight_count:
        return []
    parts: list[str] = []
    if sold_out_count:
        parts.append(f"{sold_out_count}个房型售罄")
    if tight_count:
        parts.append(f"{tight_count}个房型仅余1–2间")
    return [f"库存结构：{'，'.join(parts)}"]


def _rate_text(value: Any) -> str:
    ratio = _ratio(value)
    return "暂无" if ratio is None else f"{ratio * 100:.2f}%"


def _room_count_text(value: Any) -> str:
    number = _number(value)
    return "—" if number is None else f"{number:g}间"


def _inventory_status(value: Any) -> str:
    available = _number(value)
    if available is None:
        return "暂无"
    if available <= 5:
        return "极紧张"
    if available <= 10:
        return "偏紧张"
    return "相对充足"


def _price_summary_lines(summary: dict[str, Any]) -> list[str]:
    summary = summary if isinstance(summary, dict) else {}
    comparisons = summary.get("comparisons", [])
    references = summary.get("reference_comparisons", [])
    lines: list[str] = [f"【直接可比｜共{len(comparisons)}项】"]
    if comparisons:
        for index, item in enumerate(comparisons, start=1):
            lines.extend([f"{index}. {_show(item.get('own_room_name'))}："])
            competitor_price = _number(item.get("competitor_price"))
            if competitor_price is None:
                lines.append(f"我方 {_money(item.get('own_price'))}，竞品暂无同类有效价格")
            else:
                lines.append(f"我方 {_money(item.get('own_price'))}，竞品 {_money(competitor_price)}")
                diff = _number(item.get("price_diff")) or 0
                if diff < 0:
                    lines.append(f"价差：我方低 {_money(abs(diff))}")
                elif diff > 0:
                    lines.append(f"价差：我方高 {_money(diff)}")
                else:
                    lines.append("价差：与竞品相同")
            lines.append(f"判断：{_show(item.get('judgement'))}，{_show(item.get('advice'))}")
            if index < len(comparisons):
                lines.append("")
    else:
        lines.append("暂无直接可比数据")
    if references:
        lines.extend(["", f"【参考对比｜共{len(references)}项，不驱动调价】"])
        for index, item in enumerate(references, start=1):
            lines.extend(
                [
                    f"{index}. 我方：{_show(item.get('own_room_name'))}｜{_money(item.get('own_price'))}",
                    f"   竞品：{_show(item.get('competitor_room_name'))}｜{_money(item.get('competitor_price'))}｜差异：{_show(item.get('room_difference_note'))}",
                ]
            )
            if index < len(references):
                lines.append("")
    return lines


def _exhibition_lines(context: dict[str, Any]) -> list[str]:
    context = context if isinstance(context, dict) else {}
    events = context.get("events", [])
    if not events:
        return [str(context.get("message") or "展会信息暂不可用。")]
    hotel_rule_available = bool(context.get("hotel_rule_available", False))
    groups: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        phase = str(event.get("event_phase") or "").strip()
        business_note = _event_business_note(phase, hotel_rule_available, event.get("effective_event_level"))
        key = (
            _show(event.get("venue")),
            _show(event.get("event_start_date")),
            _show(event.get("event_end_date")),
            phase,
            business_note,
        )
        groups[key].append(event)

    venue_count = len({venue for venue, _, _, _, _ in groups})
    lines = [
        str(context.get("message") or "展会信息暂不可用。"),
        f"共{len(events)}场｜涉及{venue_count}个展馆",
    ]
    for (venue, start_date, end_date, phase, business_note), group_events in groups.items():
        lines.extend(
            [
                "",
                f"【{venue}｜{start_date} 至 {end_date}｜{_event_phase_label(phase)}｜{len(group_events)}场】",
                f"影响汇总：{_event_impact_summary(group_events)}",
            ]
        )
        for index, event in enumerate(group_events, start=1):
            global_level = _event_level_text(event.get("global_event_demand_level") or event.get("demand_level"))
            hotel_level = _event_level_text(event.get("hotel_impact_level"))
            effective_level = _event_level_text(event.get("effective_event_level"))
            lines.extend(
                [
                    f"{index}. {_show(event.get('event_name'))}",
                    f"   本店{hotel_level}｜全局{global_level}｜最终{effective_level}",
                ]
            )
        lines.append(f"经营口径：{business_note}")
    return lines


def _event_impact_summary(events: list[dict[str, Any]]) -> str:
    hotel_levels = [_event_level_text(event.get("hotel_impact_level")) for event in events]
    effective_levels = [_event_level_text(event.get("effective_event_level")) for event in events]
    return f"本店{_event_level_counts(hotel_levels)}｜最终{_event_level_counts(effective_levels)}"


def _event_level_counts(levels: list[str]) -> str:
    order = ("高", "中", "常规", "低", "未配置")
    counts = {level: levels.count(level) for level in order}
    return "、".join(f"{level}{count}场" for level, count in counts.items() if count) or "未配置"


def _event_phase_label(phase: str) -> str:
    return {
        "active": "进行中",
        "pre_event_stay": "展前入住覆盖",
        "post_event_checkout": "展后离店覆盖",
    }.get(phase, "状态未知")


def _event_phase_text(phase: str, fallback: Any) -> str:
    labels = {
        "active": "展会进行中。",
        "pre_event_stay": "展会尚未开始；仅为展前入住覆盖。",
        "post_event_checkout": "展会已结束；仅为展后离店覆盖。",
    }
    return labels.get(phase, _show(fallback))


def _event_business_note(phase: str, hotel_rule_available: Any, effective_level: Any) -> str:
    if phase != "active":
        return "不作为当日需求或调价依据。"
    if not hotel_rule_available or str(effective_level or "").strip().lower() == "unknown":
        return "本店影响规则不可用，仅作背景信息。"
    return "可作为今日需求背景，不单独触发调价。"


def _event_level_text(value: Any) -> str:
    return {
        "high": "高",
        "medium": "中",
        "normal": "常规",
        "low": "低",
        "unknown": "未配置",
    }.get(str(value or "").strip().lower(), "未配置")




def _numbered_lines(rows: list[Any]) -> list[str]:
    return [f"{index}. {_show(row)}" for index, row in enumerate(rows, start=1)] or ["暂无明确建议"]


def _money(value: Any) -> str:
    number = _number(value)
    if number is None:
        return "价格暂无"
    return f"¥{number:.0f}" if number.is_integer() else f"¥{number:.2f}"


def _ota_lines(rows: list[dict[str, Any]], *, include_hotel: bool) -> list[str]:
    if not rows:
        return ["- 暂无"]
    lines = []
    for row in rows:
        hotel = f"{_show(row.get('hotel_name'))}｜" if include_hotel else ""
        lines.append(f"- {hotel}{_show(row.get('room_name'))}｜{_show(row.get('room_category'))}｜¥{_show(row.get('price'))}｜可售 {_show(row.get('available'))}｜{_show(row.get('collected_at'))}")
    return lines


def _compare_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- 暂无"]
    return [f"- {_show(row.get('room_category'))}｜自有 {_show(row.get('own_room_name'))} ¥{_show(row.get('own_price'))}｜最低竞品 {_show(row.get('competitor_hotel_name'))} {_show(row.get('competitor_room_name'))} ¥{_show(row.get('competitor_min_price'))}｜差价 {_show(row.get('price_diff'))}｜差价比例 {_show(row.get('price_diff_pct'))}%" for row in rows]


def _competitor_supply_alert_lines(rows: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for row in rows:
        lines.append(
            "⚠️ 竞品房型供给收紧："
            f"{_show(row.get('competitor_hotel_name'))} {_show(row.get('competitor_room_name'))}"
            f"与我方 {_show(row.get('own_room_name'))} 核心属性一致，连续两批明确不可售；"
            "我方仍可售，存在提价观察空间。请结合本店库存、销售率和预订变化人工判断。"
        )
    return lines


def _recommendation_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- 本次无需要关注的价格动作。"]
    lines: list[str] = []
    hold_actions = {"维持", "保持", "维持价格", "保持价格", "暂不调整"}
    for row in rows[:3]:
        base_action = _show(row.get("action"))
        action = base_action
        if row.get("manual_review") and action not in {"人工确认", "人工复核"}:
            action += "（人工确认）"
        headline = f"- {_show(row.get('room_name') or row.get('room_category'))}｜{action}"
        if row.get("suggested_adjustment") not in (None, ""):
            confidence = str(row.get("confidence") or "").lower()
            if base_action in hold_actions:
                price_label = "维持价"
            else:
                price_label = "试探价" if row.get("manual_review") or confidence == "low" else "建议价"
            headline += f"｜{price_label} {_money(row.get('suggested_adjustment'))}"
        lines.append(headline)

        evidence: list[str] = []
        inventory_change = row.get("inventory_change")
        if isinstance(inventory_change, dict):
            previous = _show(inventory_change.get("previous_available"))
            current = _show(inventory_change.get("current_available"))
            delta = _number(inventory_change.get("available_delta"))
            if delta is not None:
                direction = "净减少" if delta < 0 else "净回补"
                evidence.append(f"库存：{previous}→{current}间（{direction}{abs(delta):g}间，库存变化估算）")
        own_price = _number(row.get("own_price"))
        competitor_price = _number(row.get("competitor_price"))
        if own_price is not None and competitor_price is not None and row.get("has_direct_comparison"):
            evidence.append(f"我方/直接竞品：{_money(own_price)}/{_money(competitor_price)}")
        if evidence:
            lines.append("  " + "｜".join(evidence))
        lines.append(f"  原因：{_show(row.get('reason'))}")
    return lines


def _future_action_fallback_lines(report: dict[str, Any]) -> list[str]:
    """Render one grounded future-date preparation when Hermes is empty or unavailable."""
    raw_context = report.get("future_event_demand_context") or {}
    if not isinstance(raw_context, dict):
        return []
    compact_context = _compact_future_event_context(raw_context)
    pms = report.get("pms_summary") or {}
    snapshot = {
        "business_snapshot": {
            "today_available": pms.get("today_available", pms.get("available_rooms")),
            "actual_sales_rate": pms.get("actual_sales_rate"),
            "sales_rate_source": pms.get("sales_rate_source"),
            "room_type_inventory": report.get("pms_room_type_details") or [],
        },
        "analysis_context": {"pms_change": report.get("pms_change") or {}},
        "future_event_demand_context": compact_context,
    }
    evidence = _business_evidence(snapshot, {})
    candidates = [
        (key, fact) for key, fact in evidence.items()
        if fact.get("kind") == "future_event"
        and fact.get("event_actionable")
        and fact.get("has_confirmed_signal")
    ]
    candidates.sort(key=lambda item: str(item[1].get("stay_date") or ""))
    if candidates:
        _, fact = candidates[0]
        target = fact.get("target_label") or "活动入住日"
        reason = "活动日期与对应入住日库存代理信号达到持续起量条件，今天先核对该入住日的房型与价格安排"
        recheck = "下一批核对该入住日库存代理信号与在售房态"
        task_text = "今天核对活动入住日的房型、在售价格与低价库存安排"
    else:
        demand_candidates = [
            (key, fact) for key, fact in evidence.items()
            if fact.get("kind") == "demand_signal" and fact.get("confirmed")
        ]
        demand_candidates.sort(key=lambda item: str(item[1].get("stay_date") or ""))
        if not demand_candidates:
            return []
        _, fact = demand_candidates[0]
        target = fact.get("target_label") or "未来入住日"
        reason = "未来入住日库存代理信号达到持续起量条件，今天先复核该日期的房型与价格安排"
        recheck = "下一批核对该入住日库存代理信号与在售房态"
        task_text = "今天复核该未来入住日的房型、在售价格与低价库存安排"
    return [
        "【今日动作｜数据规则兜底】",
        f"1. {target}：{task_text}。",
        f"   判断：{reason}。依据：{fact['text']}。复查：{recheck}。",
    ]


def _business_advice_lines(report: dict[str, Any]) -> list[str]:
    if report.get("analysis_source") == "hermes" and isinstance(report.get("hermes_decision"), dict):
        actions = _render_hermes_decision(report["hermes_decision"])
        if actions:
            return ["【今日动作】", *actions]
        future_fallback = _future_action_fallback_lines(report)
        if future_fallback:
            return future_fallback
        if not report.get("pms_room_type_details"):
            return ["【今日动作】", "房型库存缺失，本批无法判断房型经营动作。"]
        comparisons = (report.get("price_summary") or {}).get("comparisons") or []
        has_direct = any(isinstance(row, dict) and _number(row.get("competitor_price")) is not None for row in comparisons)
        future = report.get("future_event_demand_context") or {}
        future_reviewed = bool(
            (future.get("events") or [])
            or (future.get("demand_signals") or [])
            or (future.get("price_control_advice") or [])
        )
        notes = []
        if future_reviewed:
            notes.append("活动与未来入住日信号已纳入判断")
        if not has_direct:
            notes.append("缺少有效直接比价仅限制竞品驱动的价格动作")
        suffix = "；" + "；".join(notes) if notes else ""
        return ["【今日动作】", f"本批综合经营数据后未形成需立即执行的新动作{suffix}。"]
    if "hermes_decision" in report:
        future_fallback = _future_action_fallback_lines(report)
        if future_fallback:
            return future_fallback
        comparisons = (report.get("price_summary") or {}).get("comparisons") or []
        inventory = report.get("pms_room_type_details") or []
        candidates = []
        for row in comparisons:
            if not isinstance(row, dict):
                continue
            name = str(row.get("own_room_name") or "").strip()
            own, rival = _number(row.get("own_price")), _number(row.get("competitor_price"))
            if not name or own is None or rival is None or own <= 0 or rival <= 0:
                continue
            if abs(own - rival) < max(40, own * 0.20):
                continue
            verified_name = _verified_pms_room_name(name, inventory)
            if not verified_name:
                continue
            matching = [
                _number(item.get("available")) for item in inventory
                if isinstance(item, dict)
                and _room_name_key(item.get("roomType") or item.get("room_type")) == _room_name_key(verified_name)
            ]
            if len(matching) != 1 or matching[0] is None or matching[0] <= 0:
                continue
            candidates.append((abs(own - rival), verified_name, own, rival, matching))
        if candidates:
            _, name, own, rival, matching = max(candidates)
            stock = f"；该房型可售{matching[0]:g}间" if matching and matching[0] is not None else ""
            return [
                "【今日动作｜数据规则】",
                f"1. {name}：核对同日同权益挂牌价，再决定是否调整。",
                f"   依据：我方{_money(own)}，直接可比竞品{_money(rival)}，{'我方高' if own > rival else '我方低'}{_money(abs(own - rival))}{stock}。",
            ]
        return ["【今日动作】", "本次经营分析未完成，暂未生成可核实的新动作。"]
    recommendations = report.get("recommendations") or []
    hold_actions = {"维持", "保持", "维持价格", "保持价格", "暂不调整"}
    decision_rows = sorted(
        (
            row for row in recommendations
            if str(row.get("action") or "").strip()
            and str(row.get("action") or "").strip() not in {"人工确认", "人工复核"}
            and _number(row.get("suggested_adjustment")) is not None
            and bool(row.get("has_direct_comparison"))
        ),
        key=_price_action_sort_key,
    )
    action_rows = [
        row for row in decision_rows
        if str(row.get("action") or "").strip() not in hold_actions
    ]
    rule_advice = [
        str(item).strip()
        for item in (report.get("business_advice") or [])
        if str(item).strip()
        and not str(item).strip().startswith("展会参考：")
        and "进行中展会" not in str(item)
    ][:3]
    lines: list[str] = []
    raw_hermes = str(report.get("hermes_analysis") or "").strip()
    hermes_available = (
        str(report.get("analysis_source") or "") == "hermes"
        and _hermes_analysis_is_safe(raw_hermes)
    )
    hermes = _displayable_hermes_analysis(raw_hermes) if hermes_available else ""
    if decision_rows:
        lines.extend(["【今日动作】", *_price_action_notice_lines(action_rows)])
        lines.extend(_recommendation_lines(decision_rows))
    elif rule_advice:
        lines.extend(["【今日动作】", *_numbered_lines(rule_advice)])
    if hermes:
        if lines:
            lines.append("")
        lines.extend(["【判断依据】", hermes])
    return lines


_REDUNDANT_HERMES_JUDGEMENT_MARKERS = (
    "待入住数据缺失",
    "待入住数据不可用",
    "无法推断待入住",
    "无法推断后续进店",
    "参考数据不具备同类可比性",
    "不形成竞品价差判断",
    "没有同类可比价格",
    "竞品对照存在配置差异",
    "竞品价差不作强弱结论",
)


def _displayable_hermes_analysis(raw_analysis: str) -> str:
    items: list[str] = []
    for line in str(raw_analysis or "").splitlines():
        item = re.sub(r"^\s*\d+[.)]\s*", "", line).strip()
        if not item or any(marker in item for marker in _REDUNDANT_HERMES_JUDGEMENT_MARKERS):
            continue
        items.append(item)
        if len(items) == _HERMES_ANALYSIS_MAX_ITEMS:
            break
    return "\n".join(f"{index}. {item}" for index, item in enumerate(items, start=1))


def _price_action_sort_key(row: dict[str, Any]) -> tuple[int, int]:
    confidence = str(row.get("confidence") or "").lower()
    confidence_rank = {"high": 0, "medium": 1, "low": 2}.get(confidence, 3)
    return (confidence_rank, 1 if row.get("manual_review") else 0)


def _price_action_notice_lines(rows: list[dict[str, Any]]) -> list[str]:
    low_confidence_rows = [row for row in rows if str(row.get("confidence") or "").lower() == "low"]
    if not low_confidence_rows:
        return []
    risks = list(dict.fromkeys(
        str(row.get("risk") or "").strip() for row in low_confidence_rows if str(row.get("risk") or "").strip()
    ))
    suffix = "；".join(risks) if risks else "当前数据不足以形成高置信度价格结论"
    return [f"⚠️ 以下为低置信度试探价，需人工确认：{suffix}"]


def _alert_lines(rows: list[Any]) -> list[str]:
    if not rows:
        return ["- 无"]
    return [f"- {_show(row)}" for row in rows]
