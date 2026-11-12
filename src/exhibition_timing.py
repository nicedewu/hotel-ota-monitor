from __future__ import annotations

from datetime import date, datetime
from typing import Any


def as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def annotate_events_for_day(stay_date: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    annotated: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        item = dict(event)
        phase, note = event_phase_for_day(stay_date, item)
        item["event_phase"] = phase
        item["event_phase_note"] = note
        annotated.append(item)
    return annotated


def event_phase_for_day(stay_date: str, event: dict[str, Any]) -> tuple[str, str]:
    target = as_date(stay_date)
    start = as_date(event.get("event_start_date"))
    end = as_date(event.get("event_end_date"))
    if target is not None and start is not None and end is not None and start <= target <= end:
        return "active", "展会进行中；可作为当日需求参考。"
    if target is not None and end is not None and target > end:
        return "post_event_checkout", "展会已结束；今天仅为展后离店日，不得称为展会日、最后一天或闭幕日，也不得作为当日展会需求支撑。"
    if target is not None and start is not None and target < start:
        return "pre_event_stay", "展会尚未开始；今天仅为展前入住覆盖期，不得称为首日、开幕日或举行中，也不得作为当日展会需求支撑。"
    return "coverage_only", "仅为住宿覆盖信息，不代表今日处于展会实际展期。"


def partition_events_for_day(stay_date: str, events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    annotated = annotate_events_for_day(stay_date, events)
    active = [event for event in annotated if event.get("event_phase") == "active"]
    coverage = [event for event in annotated if event.get("event_phase") != "active"]
    return active, coverage


def annotate_events_for_period(
    period_start: str,
    period_end: str,
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    annotated: list[dict[str, Any]] = []
    start = as_date(period_start)
    end = as_date(period_end)
    for event in events:
        if not isinstance(event, dict):
            continue
        item = dict(event)
        event_start = as_date(item.get("event_start_date"))
        event_end = as_date(item.get("event_end_date"))
        if start is not None and end is not None and event_start is not None and event_end is not None and event_start <= end and event_end >= start:
            item["period_phase"] = "active_in_period"
            item["period_phase_note"] = "本周期包含实际展期；可按实际展期日期复盘。"
        else:
            item["period_phase"] = "coverage_only"
            item["period_phase_note"] = "本周期仅为住宿覆盖信息，不得称为本周期展会日或据此推断展会需求。"
        annotated.append(item)
    return annotated
