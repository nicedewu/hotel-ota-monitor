from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import ceil
from pathlib import Path
from statistics import mean, median
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from src.config import load_hotels
from src.weekly_review_analyzer import WeeklyAnalysisError, analyze_weekly_snapshot
from src.weekly_review_rules import build_rules_analysis
from src.weekly_review_repository import data_health as db_data_health
from src.weekly_review_repository import (
    claim_admin_review_for_push,
    fetch_db_hotel_config,
    fetch_weekly_facts,
    fetch_weekly_review_states,
    finish_admin_review_push,
    get_admin_review,
    update_weekly_review_notify_status,
    upsert_admin_review,
    write_weekly_review,
)


EXPECTED_HOTEL_COUNT = 5
ADMIN_WEBHOOK_ENV_KEY = "ADMIN_FEISHU_WEBHOOK_URL"
REPORT_VERSION = "v1"


class WeeklyReviewError(RuntimeError):
    pass


@dataclass
class WeeklyHotel:
    key: str
    name: str
    role: str
    competitors: list[str]
    weekly_review_enabled: bool
    webhook_env_key: str
    admin_overview_enabled: bool
    venue: str
    is_active: bool = True


def run_weekly_review(
    *,
    week_start: str,
    week_end: str,
    auto_last_week: bool,
    hotel_key: str,
    dry_run: bool,
    notify: bool,
    write_db: bool,
    admin_overview: bool,
    output_dir: str,
    config_path: str = "hotels.json",
    retry_failed_only: bool = False,
    no_llm: bool = False,
    force_regenerate: bool = False,
    require_complete_final: bool = False,
) -> dict[str, Any]:
    start, end = resolve_week(week_start, week_end, auto_last_week)
    run_id = uuid4().hex
    hotels = load_weekly_hotels(config_path)
    targets = _select_targets(hotels, hotel_key)
    if not targets:
        raise WeeklyReviewError("没有找到可复盘的自有酒店")
    configured_targets = list(targets)
    if require_complete_final:
        preflight = _weekly_preflight_targets(configured_targets, start, end)
        if not preflight["ready"]:
            failures = [
                f"{item['hotel_key']}：{'、'.join(item['missing_final_data'])}"
                for item in preflight["hotel_results"]
                if not item["ready"]
            ]
            raise WeeklyReviewError("周复盘预检未通过：" + "；".join(failures))

    existing = fetch_weekly_review_states(start, [hotel.key for hotel in targets]) if retry_failed_only else {}
    if retry_failed_only:
        existing_run_ids = {str(row.get("run_id")) for row in existing.values() if row.get("run_id")}
        if len(existing_run_ids) == 1:
            run_id = existing_run_ids.pop()
        targets = [
            hotel for hotel in targets
            if (existing.get(hotel.key, {}).get("report_json") or {}).get("data_status") != "complete"
        ]

    root = Path(output_dir) / f"{start.isoformat()}_{end.isoformat()}"
    processed = []
    warnings: list[str] = []
    notify_stats = Counter()
    db_statuses = Counter()

    for hotel in targets:
        try:
            item = build_single_hotel_review(hotel, hotels, start, end, run_id, root, use_hermes=not no_llm)
        except Exception as exc:
            item = build_failed_hotel_review(hotel, start, end, run_id, root, exc)
        processed.append(item)
        warnings.extend(item.get("warnings") or [])
        db_status = "dry_run" if dry_run else "skipped"
        if write_db and not dry_run:
            db_result = write_weekly_review({
                "week_start": start,
                "week_end": end,
                "hotel_key": hotel.key,
                "hotel_name": hotel.name,
                "review_type": "single_hotel",
                "report_markdown": item["markdown"],
                "report_json": item["json"],
                "notify_status": "pending",
                "run_id": run_id,
                "analysis_mode": item["json"]["analysis_mode"],
                "analysis_status": item["json"]["analysis_status"],
                "analysis_degraded": item["json"]["analysis_degraded"],
                "analysis_model": item["json"].get("analysis_model"),
                "analysis_model_version": item["json"].get("analysis_model_version"),
                "analysis_schema_version": item["json"].get("analysis_schema_version"),
                "analysis_duration_ms": item["json"].get("analysis_duration_ms"),
                "analysis_quality_status": item["json"].get("analysis_quality_status"),
                "analysis_error_code": item["json"].get("analysis_error_code"),
                "analysis_error_message": item["json"].get("analysis_error_message"),
                "fact_snapshot_json": item["json"]["fact_snapshot"],
                "fact_quality_passed": item["json"]["fact_quality_passed"],
            })
            db_status = db_result["status"]
        db_statuses[db_status] += 1

    if retry_failed_only:
        merged = {item["hotel"].key: item for item in _processed_from_states(existing, hotels)}
        merged.update({item["hotel"].key: item for item in processed})
        processed_for_admin = [merged[key] for key in sorted(merged)]
    else:
        processed_for_admin = processed
    admin_paths: dict[str, Any] = {}
    if hotel_key == "all" and admin_overview:
        admin_paths = build_admin_review(processed_for_admin, start, end, root, expected_hotel_count=len(configured_targets))
        if write_db and not dry_run:
            admin_paths["db_status"] = upsert_admin_review(admin_paths["db_row"])
    if notify:
        # Existing single-hotel weekly push is intentionally unchanged and independent.
        for item in processed:
            hotel = item["hotel"]
            notify_stats[notify_weekly_report(item["markdown_path"], hotel.webhook_env_key, dry_run, hotel_key=hotel.key, source_record_id=f"{run_id}:{hotel.key}")] += 1

    summary = {
        "run_id": run_id,
        "period": f"{start.isoformat()}..{end.isoformat()}",
        "hotels_processed": len(processed),
        "reports_written": len(processed),
        "admin_overview_written": bool(admin_paths),
        "notify_status": _notify_status(notify_stats, notify, dry_run),
        "db_write_status": _db_status(db_statuses, write_db, dry_run),
        "warnings": len(warnings),
        "warning_messages": warnings,
        "single_reports": [item["markdown_path"] for item in processed],
        "admin_overview": admin_paths,
        "expected_hotel_count": len(configured_targets),
        "actual_hotel_count": len(configured_targets),
        "generation_status": admin_paths.get("generation_status", "single_hotel_only"),
        "hotel_results": [
            {
                "hotel_key": item["hotel"].key,
                "data_status": item["json"].get("data_status"),
                "valid_days": (item["json"].get("metrics") or {}).get("valid_days"),
                "analysis_mode": item["json"].get("analysis_mode"),
            }
            for item in processed
        ],
    }
    return summary


def preflight_weekly_review(
    *, week_start: str, week_end: str, auto_last_week: bool,
    hotel_key: str, config_path: str = "hotels.json",
) -> dict[str, Any]:
    start, end = resolve_week(week_start, week_end, auto_last_week)
    hotels = load_weekly_hotels(config_path)
    targets = _select_targets(hotels, hotel_key)
    if not targets:
        raise WeeklyReviewError("没有找到可复盘的自有酒店")
    return _weekly_preflight_targets(targets, start, end)


def _weekly_preflight_targets(targets: list[WeeklyHotel], week_start: date, week_end: date) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for hotel in targets:
        try:
            facts = fetch_weekly_facts(hotel.key, hotel.competitors, week_start, week_end, hotel.venue)
            metrics = summarize_metrics(hotel, facts)
            ready = metrics.get("valid_days") == 7 and metrics.get("previous_valid_days") == 7
            missing = [
                item for item in metrics.get("missing_data") or []
                if "final" in item or "有效天数" in item
            ]
            if not ready and not missing:
                missing = [
                    f"final 正式样本不足：本周 {metrics.get('valid_days', 0)}/7，"
                    f"环比基准 {metrics.get('previous_valid_days', 0)}/7"
                ]
            results.append({
                "hotel_key": hotel.key,
                "hotel_name": hotel.name,
                "ready": ready,
                "valid_final_days": metrics.get("valid_days", 0),
                "previous_valid_final_days": metrics.get("previous_valid_days", 0),
                "missing_final_data": missing,
            })
        except Exception as exc:
            results.append({
                "hotel_key": hotel.key,
                "hotel_name": hotel.name,
                "ready": False,
                "valid_final_days": 0,
                "previous_valid_final_days": 0,
                "missing_final_data": [f"事实读取失败：{type(exc).__name__}: {str(exc)[:160]}"],
            })
    return {
        "period": f"{week_start.isoformat()}..{week_end.isoformat()}",
        "ready": all(item["ready"] for item in results),
        "hotels_checked": len(results),
        "hotel_results": results,
    }


def notify_existing_weekly_reviews(
    *,
    week_start: str,
    week_end: str,
    auto_last_week: bool,
    hotel_key: str,
    dry_run: bool,
    output_dir: str,
    config_path: str = "hotels.json",
) -> dict[str, Any]:
    start, end = resolve_week(week_start, week_end, auto_last_week)
    targets = _select_targets(load_weekly_hotels(config_path), hotel_key)
    if not targets:
        raise WeeklyReviewError("没有找到可推送周报的自有酒店")
    root = Path(output_dir) / f"{start.isoformat()}_{end.isoformat()}"
    states = fetch_weekly_review_states(start, [hotel.key for hotel in targets])
    prepared: list[tuple[WeeklyHotel, dict[str, Any], Path]] = []
    errors: list[str] = []

    for hotel in targets:
        state = states.get(hotel.key)
        if not state:
            errors.append(f"{hotel.key}: 数据库中不存在已保存周报")
            continue
        payload = state.get("report_json") or {}
        markdown = str(state.get("report_markdown") or "")
        markdown_path = root / hotel.key / "weekly_review_push.md"
        checks = {
            "hotel_key": payload.get("hotel_key") == hotel.key,
            "week_start": payload.get("week_start") == start.isoformat(),
            "week_end": payload.get("week_end") == end.isoformat(),
            "analysis": payload.get("analysis_status") == "completed" and payload.get("analysis_mode") in {"hermes", "rules_fallback"},
            "fact_quality": payload.get("fact_quality_passed") is True,
            "run_id": bool(payload.get("run_id")),
            "report_body": bool(markdown.strip()),
        }
        failed = [name for name, passed in checks.items() if not passed]
        if failed:
            errors.append(f"{hotel.key}: 校验失败 ({', '.join(failed)})")
            continue
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(markdown, encoding="utf-8")
        prepared.append((hotel, payload, markdown_path))

    run_ids = {str(payload["run_id"]) for _, payload, _ in prepared}
    if len(run_ids) > 1:
        errors.append("各店周报 run_id 不一致")
    if errors:
        raise WeeklyReviewError("周复盘统一推送已阻断：" + "; ".join(errors))

    notify_stats = Counter()
    db_statuses = Counter()
    for hotel, payload, markdown_path in prepared:
        status = notify_weekly_report(markdown_path, hotel.webhook_env_key, dry_run, hotel_key=hotel.key, source_record_id=f"{payload.get('run_id') or 'weekly'}:{hotel.key}")
        notify_stats[status] += 1
        if not dry_run:
            db_statuses[update_weekly_review_notify_status(str(payload["run_id"]), hotel.key, status)] += 1

    notify_status = _notify_status(notify_stats, True, dry_run)
    if notify_status not in {"sent", "dry_run"}:
        raise WeeklyReviewError(f"周复盘统一推送失败：notify_status={notify_status}")
    if not dry_run and set(db_statuses) != {"updated"}:
        raise WeeklyReviewError(f"周复盘推送状态写库失败：{dict(db_statuses)}")

    return {
        "run_id": run_ids.pop(),
        "period": f"{start.isoformat()}..{end.isoformat()}",
        "hotels_processed": len(prepared),
        "reports_written": 0,
        "admin_overview_written": False,
        "notify_status": notify_status,
        "db_write_status": "dry_run" if dry_run else "updated",
        "warnings": 0,
        "warning_messages": [],
        "single_reports": [str(path) for _, _, path in prepared],
        "admin_overview": {},
    }


def build_single_hotel_review(
    hotel: WeeklyHotel,
    hotels: dict[str, WeeklyHotel],
    week_start: date,
    week_end: date,
    run_id: str,
    output_root: Path,
    use_hermes: bool = True,
) -> dict[str, Any]:
    facts = fetch_weekly_facts(hotel.key, hotel.competitors, week_start, week_end, hotel.venue)
    metrics = summarize_metrics(hotel, facts)
    metrics["inventory_change_event_count"] = len(facts.get("inventory_change_events") or [])
    missing_data = list(metrics.pop("missing_data"))
    if facts["price_comparisons_missing"]:
        missing_data.append("price_comparisons table missing")
    snapshot = _build_fact_snapshot(hotel, week_start, week_end, run_id, metrics, facts, missing_data)
    fact_quality_passed = bool(snapshot["fact_quality"]["passed"])
    diagnostics: dict[str, Any] = {"analysis_duration_ms": None, "analysis_model": None, "analysis_schema_version": "1.0"}
    error_code = ""
    if not fact_quality_passed:
        analysis = build_rules_analysis(snapshot, "FACT_QUALITY_FAILED")
        mode, status, degraded, error_code = "rules_fallback", "blocked", True, "FACT_QUALITY_FAILED"
    elif not use_hermes:
        analysis = build_rules_analysis(snapshot, "HERMES_DISABLED")
        mode, status, degraded, error_code = "rules_fallback", "completed", True, "HERMES_DISABLED"
    else:
        try:
            analysis, diagnostics = analyze_weekly_snapshot(snapshot)
            mode, status, degraded = "hermes", "completed", False
        except WeeklyAnalysisError as exc:
            analysis = build_rules_analysis(snapshot, exc.code)
            mode, status, degraded, error_code = "rules_fallback", "completed", True, exc.code
    hotel_dir = output_root / hotel.key
    json_path = hotel_dir / "weekly_review.json"
    markdown_path = hotel_dir / "weekly_review.md"
    payload = {
        "run_id": run_id,
        "hotel_key": hotel.key,
        "hotel_name": hotel.name,
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "metrics": metrics,
        "data_status": metrics["data_status"],
        "core_finding": analysis["summary"],
        "risk": (analysis.get("risks") or [{}])[0].get("risk", ""),
        "opportunity": "按已保存的事实继续观察。",
        "action": [item.get("action") for item in analysis.get("actions") or [] if item.get("action")],
        "evidence": _metric_evidence(metrics),
        "missing": sorted(set(missing_data)),
        "missing_data": sorted(set(missing_data)),
        "analysis": analysis,
        "analysis_mode": mode,
        "analysis_status": status,
        "analysis_degraded": degraded,
        "analysis_model": diagnostics.get("analysis_model"),
        "analysis_model_version": diagnostics.get("analysis_model"),
        "analysis_schema_version": diagnostics.get("analysis_schema_version", "1.0"),
        "analysis_duration_ms": diagnostics.get("analysis_duration_ms"),
        "analysis_quality_status": "passed" if fact_quality_passed else "failed",
        "analysis_error_code": error_code or None,
        "analysis_error_message": None,
        "fact_snapshot": snapshot,
        "fact_quality_passed": fact_quality_passed,
    }
    markdown = render_single_markdown(payload)
    _write_json(json_path, payload)
    markdown_path.write_text(markdown, encoding="utf-8")
    return {
        "hotel": hotel,
        "json": payload,
        "markdown": markdown,
        "json_path": str(json_path),
        "markdown_path": str(markdown_path),
        "warnings": sorted(set(missing_data)),
    }


def build_failed_hotel_review(
    hotel: WeeklyHotel, week_start: date, week_end: date, run_id: str, output_root: Path, exc: Exception
) -> dict[str, Any]:
    error = f"{type(exc).__name__}: {str(exc)[:180]}"
    snapshot = {
        "schema_version": "1.0", "run_id": run_id, "hotel_key": hotel.key,
        "period_start": week_start.isoformat(), "period_end": week_end.isoformat(),
        "fact_quality": {"passed": False, "missing_sources": [error], "warnings": [error]},
        "facts": {"metrics": {}}, "fact_references": [],
    }
    payload = {
        "run_id": run_id,
        "hotel_key": hotel.key,
        "hotel_name": hotel.name,
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "data_status": "blocked",
        "metrics": {"data_status": "blocked", "valid_days": 0},
        "core_finding": "本店周复盘生成失败",
        "risk": error,
        "opportunity": "03:00 任务将只重试失败门店",
        "action": ["检查数据源后由 03:00 重试任务重新生成。"],
        "evidence": [],
        "missing": [error],
        "missing_data": [error],
        "analysis": build_rules_analysis(snapshot, "FACT_COLLECTION_ERROR"),
        "analysis_mode": "rules_fallback",
        "analysis_status": "blocked",
        "analysis_degraded": True,
        "analysis_model": None,
        "analysis_model_version": None,
        "analysis_schema_version": "1.0",
        "analysis_duration_ms": None,
        "analysis_quality_status": "failed",
        "analysis_error_code": "FACT_COLLECTION_ERROR",
        "analysis_error_message": error,
        "fact_snapshot": snapshot,
        "fact_quality_passed": False,
    }
    markdown = render_single_markdown(payload)
    hotel_dir = output_root / hotel.key
    json_path = hotel_dir / "weekly_review.json"
    markdown_path = hotel_dir / "weekly_review.md"
    _write_json(json_path, payload)
    markdown_path.write_text(markdown, encoding="utf-8")
    return {
        "hotel": hotel,
        "json": payload,
        "markdown": markdown,
        "json_path": str(json_path),
        "markdown_path": str(markdown_path),
        "warnings": [error],
    }


def summarize_metrics(hotel: WeeklyHotel, facts: dict[str, Any]) -> dict[str, Any]:
    daily = _daily_samples(facts.get("pms_rows") or [])
    previous = _daily_samples(facts.get("previous_pms_rows") or [])
    valid_days = len(daily)
    previous_valid_days = len(previous)
    current_final_missing_dates = list(facts.get("pms_current_week_final_missing_dates") or [])
    previous_final_missing_dates = list(facts.get("pms_previous_week_final_missing_dates") or [])
    comparison_ready = valid_days == 7 and previous_valid_days == 7
    data_status = "complete" if comparison_ready else "degraded" if valid_days >= 4 else "blocked"
    source_name = str((facts.get("source_status") or {}).get("pms") or "hotel_daily_operating_metrics.metrics.snapshots.final")
    missing: list[str] = []
    if valid_days != 7:
        missing.append(f"{source_name} 本周有效天数={valid_days}/7")
    if previous_valid_days != 7:
        missing.append(f"{source_name} 环比基准有效天数={previous_valid_days}/7")
    if current_final_missing_dates:
        missing.append(f"本周 final 入住率缺失日期：{'、'.join(current_final_missing_dates)}")
    if previous_final_missing_dates:
        missing.append(f"环比基准 final 入住率缺失日期：{'、'.join(previous_final_missing_dates)}")
    sales_rate = _weighted_rate(daily)
    previous_rate = _weighted_rate(previous)
    weekend = _weighted_rate([row for row in daily if date.fromisoformat(row["date"]).weekday() in {4, 5}])
    weekday = _weighted_rate([row for row in daily if date.fromisoformat(row["date"]).weekday() not in {4, 5}])
    tight_days = sum(1 for row in daily if row["sales_rate"] >= 85 or row["available_rooms"] <= max(3, ceil(row["room_total"] * 0.1)))
    critical_days = sum(1 for row in daily if row["sales_rate"] >= 95 or row["available_rooms"] <= 2)
    min_available = min(daily, key=lambda row: row["available_rooms"], default={})

    price_comparisons = _weekly_price_comparisons(hotel, facts.get("price_rows") or [])
    price_deltas = [row["price_delta_rate"] for row in price_comparisons]
    price_delta = round(median(price_deltas), 2) if price_deltas else None
    if price_delta is None:
        price_position = "样本不足"
    elif price_delta <= -5:
        price_position = "低于竞品"
    elif price_delta <= 5:
        price_position = "接近竞品"
    elif price_delta <= 15:
        price_position = "略高于竞品"
    else:
        price_position = "明显高于竞品"
    price_sample_count = len(price_comparisons)
    price_confidence = "normal" if price_sample_count >= 5 else "low" if price_sample_count >= 2 else "insufficient"
    if price_sample_count < 2:
        missing.append("normalized competitor price samples")

    alerts = [row for row in facts.get("occupancy_alerts") or [] if _formal_alert(row)]
    alert_categories = Counter(str(row.get("alert_type") or "unknown") for row in alerts)
    delta = round(sales_rate - previous_rate, 2) if comparison_ready and sales_rate is not None and previous_rate is not None else None
    next_actions = [
        "下周每日正式快照后核对销售率与可售房；进入紧张区间时减少低价房量。",
        "同类房型连续两次高于竞品 15% 以上且销售无增长时，再提出小幅调价建议。",
    ]
    if alerts:
        next_actions.append("对本周正式预警逐条复核处置结果，避免同类异常跨日重复出现。")
    return {
        "data_status": data_status,
        "valid_days": valid_days,
        "previous_valid_days": previous_valid_days,
        "week_over_week_available": comparison_ready,
        "current_final_missing_dates": current_final_missing_dates,
        "previous_final_missing_dates": previous_final_missing_dates,
        "daily_pms": daily,
        "weekly_sales_rate": sales_rate,
        "previous_week_sales_rate": previous_rate if comparison_ready else None,
        "week_over_week_pp": delta,
        "weekend_sales_rate": weekend,
        "weekday_sales_rate": weekday,
        "total_sold_room_nights": sum(row["sold_rooms"] for row in daily),
        "total_room_nights": sum(row["room_total"] for row in daily),
        "previous_total_sold_room_nights": sum(row["sold_rooms"] for row in previous) if comparison_ready else None,
        "previous_total_room_nights": sum(row["room_total"] for row in previous) if comparison_ready else None,
        "min_available_rooms": min_available.get("available_rooms"),
        "min_available_date": min_available.get("date"),
        "tight_days": tight_days,
        "critical_days": critical_days,
        "formal_alert_count": len(alerts),
        "formal_alert_categories": dict(alert_categories),
        "price_position": price_position,
        "median_price_delta_rate": price_delta,
        "price_sample_count": price_sample_count,
        "price_sample_confidence": price_confidence,
        "price_comparisons": price_comparisons,
        "next_week_actions": next_actions[:3],
        "missing_data": missing,
    }


def _daily_samples(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        total = _number(row.get("room_total"))
        sold = _number(row.get("sold_rooms"))
        available = _number(row.get("available_rooms"))
        if total is None or sold is None or available is None or total <= 0 or min(sold, available) < 0 or max(sold, available) > total:
            continue
        grouped[str(row["stay_date"])].append(row)
    result = []
    for day, valid in sorted(grouped.items()):
        row = _latest(valid)
        total = float(row["room_total"])
        sold = float(row["sold_rooms"])
        result.append({
            "date": day,
            "room_total": total,
            "sold_rooms": sold,
            "available_rooms": float(row["available_rooms"]),
            "sales_rate": round(sold / total * 100, 2),
            "max_occupancy_rate": round(sold / total * 100, 2),
        })
    return result


def _weighted_rate(rows: list[dict[str, Any]]) -> float | None:
    total = sum(float(row["room_total"]) for row in rows)
    return round(sum(float(row["sold_rooms"]) for row in rows) / total * 100, 2) if total else None


def _weekly_price_comparisons(hotel: WeeklyHotel, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, list[float]]] = defaultdict(lambda: {"own": [], "competitor": []})
    for row in rows:
        price = _number(row.get("price"))
        category = str(row.get("room_category") or "").strip()
        if price is None or price <= 0 or not category or category == "unknown":
            continue
        side = "own" if row.get("hotel_key") == hotel.key else "competitor" if row.get("hotel_key") in hotel.competitors else ""
        if side:
            grouped[(str(row.get("stay_date")), category)][side].append(price)
    result = []
    for (stay_date, category), prices in sorted(grouped.items()):
        if not prices["own"] or not prices["competitor"]:
            continue
        own_price, competitor_price = min(prices["own"]), min(prices["competitor"])
        result.append({
            "date": stay_date,
            "room_category": category,
            "own_min_price": own_price,
            "competitor_min_price": competitor_price,
            "price_delta_rate": round((own_price - competitor_price) / competitor_price * 100, 2),
        })
    return result


def _formal_alert(row: dict[str, Any]) -> bool:
    text = json.dumps(row.get("raw_context") or {}, ensure_ascii=False, default=str).lower()
    alert_type = str(row.get("alert_type") or "").lower()
    return not any(marker in text or marker in alert_type for marker in ('"dry_run": true', '"test": true', '"suppressed": true', "dry_run", "test"))


def _metric_evidence(metrics: dict[str, Any]) -> list[str]:
    evidence = [f"正式日样本 {metrics.get('valid_days', 0)}/7"]
    if metrics.get("price_sample_count"):
        evidence.append(f"同类房型价格样本 {metrics['price_sample_count']} 个")
    if metrics.get("formal_alert_count"):
        evidence.append(f"正式库存预警 {metrics['formal_alert_count']} 次")
    return evidence


def load_weekly_hotels(config_path: str) -> dict[str, WeeklyHotel]:
    config = _config_hotels(config_path)
    try:
        db_hotels = fetch_db_hotel_config()
    except Exception:
        db_hotels = {}
    keys = set(config) | set(db_hotels)
    result: dict[str, WeeklyHotel] = {}
    for key in sorted(keys):
        cfg = config.get(key, {})
        db = db_hotels.get(key, {})
        role = str(db.get("hotel_role") or cfg.get("hotel_type") or cfg.get("role") or "unknown")
        name = str(db.get("hotel_name") or cfg.get("name") or cfg.get("hotel_name") or key)
        competitors = _competitor_keys(cfg.get("competitors")) or [str(v) for v in db.get("competitors", [])]
        enabled = role == "own" and cfg.get("weekly_review_enabled") is not False
        is_active = bool(db.get("is_active", cfg.get("active", True)))
        result[key] = WeeklyHotel(
            key=key,
            name=name,
            role=role,
            competitors=competitors,
            weekly_review_enabled=enabled,
            webhook_env_key=str(cfg.get("weekly_review_webhook_env_key") or cfg.get("feishu_webhook_env_key") or db.get("webhook_env_key") or ""),
            admin_overview_enabled=bool(cfg.get("admin_overview_enabled", role == "own")),
            venue=str(cfg.get("venue") or "上海新国际博览中心"),
            is_active=is_active,
        )
    return result


def run_data_health(*, stay_date: str, hotel_key: str, config_path: str = "hotels.json") -> dict[str, Any]:
    try:
        current = date.fromisoformat(stay_date)
    except ValueError as exc:
        raise WeeklyReviewError("date 必须是 YYYY-MM-DD") from exc
    hotels = load_weekly_hotels(config_path)
    if hotel_key == "all":
        keys = [hotel.key for hotel in hotels.values() if hotel.role == "own" and hotel.is_active and hotel.weekly_review_enabled]
    else:
        keys = [hotel_key]
    result = db_data_health(current, keys)
    latest = Path("output/latest")
    archive = Path("output/archive") / current.isoformat()
    result["output_latest_exists"] = latest.exists()
    result["output_archive_exists"] = archive.exists()
    if not latest.exists():
        result["warnings"].append("output/latest missing")
    if not archive.exists():
        result["warnings"].append("output/archive date missing")
    result["warnings"] = sorted(set(result["warnings"]))
    return result


def load_business_snapshot_analyses(hotel: WeeklyHotel, week_start: date, week_end: date) -> list[dict[str, Any]]:
    candidates = [
        *Path("output/hotel_sets").glob(f"{hotel.key}/*/business_snapshot_report.json"),
        *Path("output/archive").glob("**/business_snapshot_report.json"),
        Path("output/latest/business_snapshot_report.json"),
    ]
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in candidates:
        if not path.is_file():
            continue
        report = _read_json_object(path)
        analysis = str(report.get("hermes_analysis") or "").strip()
        if not analysis:
            continue
        snapshot_date = _snapshot_date(report, path)
        if not snapshot_date or not (week_start <= snapshot_date <= week_end):
            continue
        if not _report_matches_hotel(report, hotel):
            continue
        key = snapshot_date.isoformat()
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "date": snapshot_date.isoformat(),
            "analysis": analysis,
            "analysis_source": report.get("analysis_source") or "unknown",
            "report_path": str(path),
        })
    return sorted(rows, key=lambda row: row["date"])


def resolve_week(week_start: str, week_end: str, auto_last_week: bool) -> tuple[date, date]:
    if auto_last_week:
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        this_monday = today - timedelta(days=today.weekday())
        start = this_monday - timedelta(days=7)
        return start, start + timedelta(days=6)
    if not week_start or not week_end:
        raise WeeklyReviewError("必须提供 --week-start/--week-end，或使用 --auto-last-week")
    try:
        start, end = date.fromisoformat(week_start), date.fromisoformat(week_end)
    except ValueError as exc:
        raise WeeklyReviewError("week-start/week-end 必须是 YYYY-MM-DD") from exc
    if end < start:
        raise WeeklyReviewError("week-end 不能早于 week-start")
    return start, end


def render_single_markdown(payload: dict[str, Any]) -> str:
    metrics = payload["metrics"]
    analysis = payload.get("analysis") or {}
    lines = [
        f"# {payload['hotel_name']} 飞书经营周报",
        f"周期：{payload['week_start']} 至 {payload['week_end']}",
        "",
        "## 1. 本周经营结论",
        _brief_paragraph([analysis.get("summary") or payload.get("core_finding"), payload.get("risk"), payload.get("opportunity")]),
        "",
        "## 2. 核心数据",
        f"- 周综合真实销售率：{_show(metrics.get('weekly_sales_rate', metrics.get('avg_occupancy_rate')))}%",
        f"- 周五周六销售率：{_show(metrics.get('weekend_sales_rate', metrics.get('weekend_occupancy_rate')))}%",
        f"- 总售房间夜：{_show(metrics.get('total_sold_room_nights'))}",
        f"- 最低可售库存：{_show(metrics.get('min_available_rooms', metrics.get('avg_available_rooms')))}",
        f"- 较前周变化：{_show(metrics.get('week_over_week_pp'))} 个百分点",
        f"- 同类房型价格位置：{_show(metrics.get('price_position'))}",
        f"- 正式库存预警：{_show(metrics.get('formal_alert_count', metrics.get('inventory_change_event_count')))} 次",
        "",
        "## 3. 关键变化",
        *_brief_list([item.get("finding", "") for item in analysis.get("key_findings") or []], 3, "- 本周未识别到显著经营变化。"),
        "",
        "## 4. 下周动作",
        *_brief_list([item.get("action", "") for item in analysis.get("actions") or []] or metrics.get("next_week_actions") or [], 3, "- 继续观察经营数据。"),
    ]
    return _sanitize_user_text("\n".join(lines))


def _build_fact_snapshot(
    hotel: WeeklyHotel, week_start: date, week_end: date, run_id: str,
    metrics: dict[str, Any], facts: dict[str, Any], missing_data: list[str],
) -> dict[str, Any]:
    references = [
        f"fact:metrics:{week_start.isoformat()}:{hotel.key}",
        f"fact:pricing:{week_start.isoformat()}:{hotel.key}",
        f"fact:inventory:{week_start.isoformat()}:{hotel.key}",
    ]
    if facts.get("exhibition_events"):
        references.append(f"fact:exhibition:{week_start.isoformat()}:{hotel.key}")
    passed = metrics.get("data_status") in {"complete", "degraded"}
    return {
        "schema_version": "1.0",
        "run_id": run_id,
        "hotel_key": hotel.key,
        "period_start": week_start.isoformat(),
        "period_end": week_end.isoformat(),
        "fact_quality": {"passed": passed, "missing_sources": sorted(set(missing_data)), "warnings": sorted(set(missing_data))},
        "facts": {
            "metrics": metrics,
            "price_comparisons": _short_rows(facts.get("price_comparisons") or [], 40),
            "price_decisions": _short_rows(facts.get("price_decisions") or [], 40),
            "exhibition_events": _short_rows(facts.get("exhibition_events") or [], 20),
            "inventory_change_events": _short_rows(facts.get("inventory_change_events") or [], 40),
        },
        "fact_references": references,
    }



def _sanitize_user_text(text: str) -> str:
    text = re.sub(r"mnemosyne", "经验候选", text, flags=re.I)
    text = re.sub(r"actual_sales_rate", "销售率", text, flags=re.I)
    text = re.sub(r"(?<![A-Za-z0-9_])esports_[A-Za-z0-9_]+(?![A-Za-z0-9_])", "相关电竞房型", text, flags=re.I)
    return re.sub(r"walk[-‐‑‒–—]?in", "散客到店", text, flags=re.I)


def build_admin_review(
    processed: list[dict[str, Any]], week_start: date, week_end: date, output_root: Path,
    expected_hotel_count: int = EXPECTED_HOTEL_COUNT,
) -> dict[str, Any]:
    rows = []
    for item in processed:
        payload = item["json"]
        metrics = payload.get("metrics") or {}
        rows.append({
            "hotel_key": item["hotel"].key,
            "hotel_name": item["hotel"].name,
            "short_name": _short_hotel_name(item["hotel"].name),
            "data_status": payload.get("data_status") or metrics.get("data_status") or "blocked",
            "metrics": metrics,
            "core_finding": str(payload.get("core_finding") or "数据不足，暂不下结论"),
            "action": list(payload.get("action") or metrics.get("next_week_actions") or [])[:1],
            "missing": list(payload.get("missing") or payload.get("missing_data") or []),
        })
    valid = [row for row in rows if row["data_status"] in {"complete", "degraded"}]
    complete = sum(row["data_status"] == "complete" for row in rows)
    degraded = sum(row["data_status"] == "degraded" for row in rows)
    blocked = len(rows) - complete - degraded
    actual = len(rows)
    if len(valid) < 3:
        generation_status = "blocked"
    elif actual != expected_hotel_count or degraded or blocked:
        generation_status = "degraded_ready"
    else:
        generation_status = "ready_to_push"
    sold = sum(float(row["metrics"].get("total_sold_room_nights") or 0) for row in valid)
    room_nights = sum(float(row["metrics"].get("total_room_nights") or 0) for row in valid)
    overall_rate = round(sold / room_nights * 100, 2) if room_nights else None
    comparable_previous = bool(valid) and all(float(row["metrics"].get("previous_total_room_nights") or 0) > 0 for row in valid)
    previous_sold = sum(float(row["metrics"].get("previous_total_sold_room_nights") or 0) for row in valid) if comparable_previous else 0
    previous_rooms = sum(float(row["metrics"].get("previous_total_room_nights") or 0) for row in valid) if comparable_previous else 0
    previous_rate = round(previous_sold / previous_rooms * 100, 2) if previous_rooms else None
    overall_delta = round(overall_rate - previous_rate, 2) if overall_rate is not None and previous_rate is not None else None
    sales_rank = sorted(valid, key=lambda row: row["metrics"].get("weekly_sales_rate") or -1, reverse=True)
    improve_rank = sorted(valid, key=lambda row: row["metrics"].get("week_over_week_pp") if row["metrics"].get("week_over_week_pp") is not None else -999, reverse=True)
    best = sales_rank[0] if sales_rank else None
    watch = sales_rank[-1] if sales_rank else None
    text = _render_admin_report(rows, week_start, week_end, generation_status, overall_rate, overall_delta, best, watch, sales_rank, improve_rank)
    quality_checks = {
        "five_sections": all(title in text for title in ("一、本周管理结论", "二、门店经营对比", "三、各店核心结论与动作", "四、异常与数据质量", "五、下周跨店管理重点")),
        "all_hotels_present": all(row["short_name"] in text for row in rows),
        "length": len(text),
        "within_hard_limit": len(text) <= 1600,
        "no_unsupported_metrics": not any(word in text for word in ("收入", "ADR", "RevPAR", "订单数", "客源结构", "渠道占比")),
    }
    if not all(value for key, value in quality_checks.items() if key != "length"):
        generation_status = "generation_failed"
    metrics = {
        "overall_sales_rate": overall_rate,
        "previous_overall_sales_rate": previous_rate,
        "week_over_week_pp": overall_delta,
        "sales_ranking": [row["hotel_key"] for row in sales_rank],
        "improvement_ranking": [row["hotel_key"] for row in improve_rank],
        "tight_days_ranking": [row["hotel_key"] for row in sorted(valid, key=lambda row: row["metrics"].get("tight_days") or 0, reverse=True)],
        "critical_days_ranking": [row["hotel_key"] for row in sorted(valid, key=lambda row: row["metrics"].get("critical_days") or 0, reverse=True)],
        "formal_alert_count": sum(int(row["metrics"].get("formal_alert_count") or 0) for row in valid),
        "hotels": rows,
    }
    db_row = {
        "week_start": week_start,
        "week_end": week_end,
        "timezone": "Asia/Shanghai",
        "expected_hotel_count": expected_hotel_count,
        "actual_hotel_count": actual,
        "complete_hotel_count": complete,
        "degraded_hotel_count": degraded,
        "blocked_hotel_count": blocked,
        "generation_status": generation_status,
        "report_version": REPORT_VERSION,
        "hotel_keys": [row["hotel_key"] for row in rows],
        "metrics": metrics,
        "report_text": text,
        "quality_checks": quality_checks,
        "generation_error": None if generation_status not in {"blocked", "generation_failed"} else "有效门店不足或质量检查未通过",
    }
    payload = {**db_row, "week_start": week_start.isoformat(), "week_end": week_end.isoformat()}
    json_path = output_root / "admin_review.json"
    md_path = output_root / "admin_review.md"
    _write_json(json_path, payload)
    md_path.write_text(text, encoding="utf-8")
    return {"json_path": str(json_path), "markdown_path": str(md_path), "generation_status": generation_status, "quality_checks": quality_checks, "db_row": db_row}


def _render_admin_report(
    rows: list[dict[str, Any]], week_start: date, week_end: date, generation_status: str,
    overall_rate: float | None, overall_delta: float | None, best: dict[str, Any] | None,
    watch: dict[str, Any] | None, sales_rank: list[dict[str, Any]], improve_rank: list[dict[str, Any]],
) -> str:
    lines = [
        "【多门店周经营复盘】",
        f"周期：{week_start.isoformat()} 至 {week_end.isoformat()}｜实际纳入 {len(rows)} 家启用自有门店",
    ]
    if generation_status != "ready_to_push":
        lines.append("数据状态：部分门店数据不完整，以下结论按现有正式样本生成。")
    lines.extend([
        "",
        "一、本周管理结论",
        f"- 综合真实销售率 {_show(overall_rate)}%，较前周 {_signed(overall_delta)} 个百分点（正式日样本加权）。",
        f"- 表现最好：{best['short_name'] if best else '暂无'}，销售率 {_show((best or {}).get('metrics', {}).get('weekly_sales_rate'))}%，紧张 {(best or {}).get('metrics', {}).get('tight_days', 0)} 天。",
        f"- 重点关注：{watch['short_name'] if watch else '暂无'}，销售率 {_show((watch or {}).get('metrics', {}).get('weekly_sales_rate'))}%，正式预警 {(watch or {}).get('metrics', {}).get('formal_alert_count', 0)} 次。",
        "",
        "二、门店经营对比",
    ])
    for index, row in enumerate(sales_rank, 1):
        metrics = row["metrics"]
        lines.append(f"- {index}. {row['short_name']}：销售 {_show(metrics.get('weekly_sales_rate'))}%，环比 {_signed(metrics.get('week_over_week_pp'))}pp，周五/六 {_show(metrics.get('weekend_sales_rate'))}%，紧张/极紧张 {metrics.get('tight_days', 0)}/{metrics.get('critical_days', 0)}天，价格{metrics.get('price_position', '样本不足')}。")
    lines.extend(["", "三、各店核心结论与动作"])
    for row in rows:
        action = row["action"][0] if row["action"] else "补齐正式样本后再确定动作。"
        lines.append(f"- {row['short_name']}（{_data_status_label(row['data_status'])}）：{_brief_text(row['core_finding'], 64)}；下周：{_brief_text(action, 64)}")
    lines.extend(["", "四、异常与数据质量"])
    for row in rows:
        metrics = row["metrics"]
        missing = _missing_label(row["missing"][:1])
        lines.append(f"- {row['short_name']}：有效{metrics.get('valid_days', 0)}/7天，预警{metrics.get('formal_alert_count', 0)}次，价格样本{metrics.get('price_sample_count', 0)}；{missing}。")
    lines.extend([
        "",
        "五、下周跨店管理重点",
        "- 周五、周六进入紧张区间的门店优先控制低价房量；竞品短时低价不直接触发全房型跟降。",
        "- 连续两次正式快照销售无增长且同类房型高于竞品 15% 以上时，才提出单房型小幅调整并复核结果。",
        f"- 环比改善优先顺序：{'、'.join(row['short_name'] for row in improve_rank) or '暂无'}；正式预警按门店闭环，不改变分店原推送。",
    ])
    return _sanitize_user_text("\n".join(lines))


def _short_hotel_name(name: str) -> str:
    short = name.split("(", 1)[-1].rstrip(")") if "(" in name else name
    return short.replace("上海", "", 1).replace("地铁站店", "店")


def _data_status_label(status: str) -> str:
    return {"complete": "数据完整", "degraded": "数据待补齐", "blocked": "数据不足"}.get(status, "数据待核对")


def _missing_label(missing: list[str]) -> str:
    if not missing:
        return "正式日样本完整"
    text = missing[0]
    if "valid days=" in text:
        return "正式日样本不足"
    if text == "normalized competitor price samples":
        return "同类房型竞品价格样本不足"
    return "部分数据待补齐"


def _processed_from_states(states: dict[str, dict[str, Any]], hotels: dict[str, WeeklyHotel]) -> list[dict[str, Any]]:
    result = []
    for key, state in states.items():
        hotel = hotels.get(key)
        payload = state.get("report_json") or {}
        if hotel and payload:
            result.append({"hotel": hotel, "json": payload, "markdown": state.get("report_markdown") or "", "warnings": payload.get("missing") or []})
    return result


def notify_existing_admin_review(
    *, week_start: str, week_end: str, auto_last_week: bool, dry_run: bool,
    output_dir: str, confirm_send: bool = False, report_version: str = REPORT_VERSION,
) -> dict[str, Any]:
    start, end = resolve_week(week_start, week_end, auto_last_week)
    if not dry_run and not confirm_send:
        raise WeeklyReviewError("管理员周报真实推送必须显式使用 --confirm-send")
    row = get_admin_review(start, report_version) if dry_run else claim_admin_review_for_push(start, report_version)
    if not row:
        existing = get_admin_review(start, report_version)
        if existing and existing.get("push_status") in {"pushed", "pushing"}:
            return _admin_notify_result(start, end, "skipped_already_pushed", existing)
        raise WeeklyReviewError("没有可推送的管理员周报，或报告状态不允许推送")
    if row.get("generation_status") not in {"ready_to_push", "degraded_ready"}:
        raise WeeklyReviewError(f"管理员周报状态禁止推送：{row.get('generation_status')}")
    report_path = Path(output_dir) / f"{start.isoformat()}_{end.isoformat()}" / "admin_review_push.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(str(row["report_text"]), encoding="utf-8")
    notify_errors: list[str] = []
    status = notify_weekly_report(report_path, ADMIN_WEBHOOK_ENV_KEY, dry_run, error_out=notify_errors)
    if not dry_run:
        error = "" if status == "sent" else (notify_errors[0] if notify_errors else "飞书通知命令失败")
        finish_admin_review_push(start, "pushed" if status == "sent" else "push_failed", error[:500], report_version)
    if status == "failed":
        raise WeeklyReviewError("管理员周报推送失败，已标记 push_failed")
    return _admin_notify_result(start, end, status, row)


def preview_admin_review(*, week_start: str, week_end: str, auto_last_week: bool) -> dict[str, Any]:
    start, end = resolve_week(week_start, week_end, auto_last_week)
    row = get_admin_review(start, REPORT_VERSION)
    if not row:
        raise WeeklyReviewError("管理员周报不存在")
    return _admin_notify_result(start, end, "preview", row)


def _admin_notify_result(start: date, end: date, status: str, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": f"admin-{start.isoformat()}-{REPORT_VERSION}",
        "period": f"{start.isoformat()}..{end.isoformat()}",
        "hotels_processed": int(row.get("actual_hotel_count") or 0),
        "reports_written": 0,
        "admin_overview_written": True,
        "memory_candidates": 0,
        "memory_write_status": "skipped",
        "notify_status": status,
        "db_write_status": "read_only" if status in {"preview", "dry_run"} else "updated",
        "warnings": 0,
        "warning_messages": [],
        "single_reports": [],
        "admin_overview": {"generation_status": row.get("generation_status"), "push_status": row.get("push_status"), "report_text": row.get("report_text") if status == "preview" else ""},
        "mnemosyne_staging": [],
    }


def write_admin_overview(processed: list[dict[str, Any]], week_start: date, week_end: date, output_root: Path) -> dict[str, Any]:
    return build_admin_review(processed, week_start, week_end, output_root)


def notify_weekly_report(markdown_path: str | Path, webhook_env_key: str, dry_run: bool, *, hotel_key: str = "", source_record_id: str = "", error_out: list[str] | None = None) -> str:
    if not webhook_env_key:
        return "skipped"
    args = [
        sys.executable,
        "scripts/notify_feishu.py",
        "--mode",
        "weekly-review",
        "--file",
        str(markdown_path),
        "--webhook-env-key",
        webhook_env_key,
    ]
    if hotel_key and source_record_id:
        args.extend(["--branch-push-hotel-key", hotel_key, "--branch-push-type", "weekly_review", "--branch-push-source-type", "weekly_review", "--branch-push-source-id", source_record_id])
    if dry_run:
        args.append("--dry-run")
    try:
        subprocess.run(args, check=True, timeout=300, text=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        if error_out is not None:
            error_out.append((exc.stderr or exc.stdout or str(exc)).strip())
        return "failed"
    except subprocess.TimeoutExpired as exc:
        if error_out is not None:
            error_out.append(f"飞书通知命令超时: {exc}")
        return "failed"
    return "dry_run" if dry_run else "sent"


def _config_hotels(config_path: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for hotel in load_hotels(config_path):
        result[hotel.key] = {
            "role": hotel.role,
            "name": hotel.name,
            "competitors": list(hotel.competitors),
            "feishu_webhook_env_key": hotel.feishu_webhook_env_key,
        }
    path = Path(config_path)
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        groups = []
        if isinstance(raw, dict):
            groups.extend(raw.get("own_hotels", []) or [])
            groups.extend(raw.get("competitor_hotels", []) or [])
            groups.extend(raw.get("hotels", []) or [])
        elif isinstance(raw, list):
            groups = raw
        for item in groups:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or item.get("hotel_key") or "").strip()
            if key:
                result.setdefault(key, {}).update(item)
    return result


def _select_targets(hotels: dict[str, WeeklyHotel], hotel_key: str) -> list[WeeklyHotel]:
    if hotel_key == "all":
        return [hotel for hotel in hotels.values() if hotel.role == "own" and hotel.is_active and hotel.weekly_review_enabled]
    hotel = hotels.get(hotel_key)
    if not hotel or hotel.role != "own" or not hotel.is_active or not hotel.weekly_review_enabled:
        raise WeeklyReviewError(f"hotel-key={hotel_key} 不是启用的自有酒店")
    return [hotel]


def _price_stats(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        price = _number(row.get("price"))
        category = str(row.get("room_category") or "unknown")
        if price is not None:
            grouped[category].append(price)
    return {
        key: {"min": min(values), "avg": round(mean(values), 2), "max": max(values), "count": len(values)}
        for key, values in sorted(grouped.items())
    }


def _dynamic_comparisons(own: dict[str, dict[str, Any]], competitor: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for category in sorted(set(own) & set(competitor)):
        own_price = own[category]["min"]
        competitor_price = competitor[category]["min"]
        diff = round(own_price - competitor_price, 2)
        rows.append({"normalized_room_type": category, "own_price": own_price, "competitor_price": competitor_price, "price_diff": diff, "price_diff_pct": round(diff / competitor_price, 4) if competitor_price else None})
    return rows


def _comparison_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- 暂无同类房型价格对比。"]
    return [
        f"- {row.get('normalized_room_type') or row.get('room_category')}：自有 ¥{_show(row.get('own_price'))}，竞品 ¥{_show(row.get('competitor_price'))}，价差 {_show(row.get('price_diff'))}"
        for row in rows[:12]
    ]


def _room_stat_lines(stats: dict[str, dict[str, Any]]) -> list[str]:
    if not stats:
        return ["- 暂无自有酒店房型价格表现数据。"]
    return [
        f"- {room_type}：最低 ¥{_show(row.get('min'))}，平均 ¥{_show(row.get('avg'))}，最高 ¥{_show(row.get('max'))}，样本 {row.get('count', 0)}"
        for room_type, row in stats.items()
    ]


def _memory_candidate_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- 本周不写入新的经验规则。"]
    return [f"- {row.get('content')}（confidence={_show(row.get('confidence'))}）" for row in rows[:5]]


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _snapshot_date(report: dict[str, Any], path: Path) -> date | None:
    metadata = report.get("metadata") if isinstance(report.get("metadata"), dict) else {}
    raw = metadata.get("snapshot_date") or report.get("snapshot_date") or report.get("date")
    if not raw and len(path.parts) >= 2:
        raw = next((part for part in reversed(path.parts) if _looks_like_date(part)), "")
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _report_matches_hotel(report: dict[str, Any], hotel: WeeklyHotel) -> bool:
    own_key = str(report.get("own_hotel_key") or "").strip()
    if own_key:
        return own_key == hotel.key
    own_hotels = ((report.get("hotels") or {}).get("own") or []) if isinstance(report.get("hotels"), dict) else []
    keys = {str(item.get("key") or item.get("hotel_key") or "").strip() for item in own_hotels if isinstance(item, dict)}
    if keys:
        return hotel.key in keys
    return True


def _looks_like_date(value: str) -> bool:
    try:
        date.fromisoformat(value[:10])
        return True
    except ValueError:
        return False


def _event_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- 本周期未匹配到展会。"]
    lines = []
    for row in rows[:8]:
        event_dates = f"实际展期 {row.get('event_start_date')} 至 {row.get('event_end_date')}"
        if row.get("period_phase") == "coverage_only":
            lines.append(f"- {row.get('event_name')}：{event_dates}；本周期仅为住宿覆盖信息，不作为本周期展会日或展会需求依据。")
        else:
            lines.append(f"- {row.get('event_name')}：{event_dates}；{row.get('period_phase_note') or '本周期包含实际展期。'}")
    return lines


def _competitor_keys(value: Any) -> list[str]:
    result = []
    for item in value or []:
        if isinstance(item, dict):
            key = str(item.get("hotel_key") or item.get("key") or "").strip()
        else:
            key = str(item).strip()
        if key:
            result.append(key)
    return result


def _max_number(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [_number(row.get(key)) for row in rows]
    values = [value for value in values if value is not None]
    return max(values) if values else None


def _min_number(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [_number(row.get(key)) for row in rows]
    values = [value for value in values if value is not None]
    return min(values) if values else None


def _latest(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return max(rows, key=lambda row: str(row.get("collected_at") or ""), default={})


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _short_rows(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    omitted = {"raw", "raw_payload", "raw_context", "input_snapshot", "metrics"}
    return [{key: value for key, value in row.items() if key not in omitted} for row in rows[:limit]]


def _brief_text(value: Any, limit: int) -> str:
    text = _sanitize_user_text(re.sub(r"\s+", " ", str(value)).strip())
    if len(text) <= limit:
        return text.rstrip("。")
    ending = max(text.rfind(marker, 0, limit + 1) for marker in "。；，")
    if ending >= limit // 2:
        return text[:ending + 1].rstrip("。")
    return text[:limit].rstrip("，；、 ") + "…"


def _brief_list(items: list[Any], limit: int, empty: str) -> list[str]:
    rows = [re.sub(r"\s+", " ", str(item)).strip() for item in items if str(item).strip()][:limit]
    return [f"- {_brief_text(row, 180)}" for row in rows] or [empty]


def _brief_paragraph(items: list[Any]) -> str:
    rows = [_brief_text(item, 180) for item in items if str(item).strip()][:3]
    return "。".join(row.rstrip("。") for row in rows) + "。" if rows else "本周经营证据不足，暂不下结论。"


def _show(value: Any) -> str:
    if value in (None, "", "unknown"):
        return "缺失"
    return str(value)


def _signed(value: Any) -> str:
    number = _number(value)
    return "缺失" if number is None else f"{number:+g}"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _status_from_counter(values: list[str]) -> str:
    unique = set(values)
    if not unique:
        return "skipped"
    unique.discard("skipped_no_candidates")
    if not unique:
        return "skipped_no_candidates"
    return unique.pop() if len(unique) == 1 else "partial_failed"


def _notify_status(counter: Counter[str], notify: bool, dry_run: bool) -> str:
    if not notify:
        return "skipped"
    if dry_run:
        return "dry_run"
    if counter.get("failed"):
        return "partial_failed" if counter.get("sent") else "failed"
    return "sent" if counter.get("sent") else "skipped"


def _db_status(counter: Counter[str], write_db: bool, dry_run: bool) -> str:
    if dry_run:
        return "dry_run"
    if not write_db:
        return "skipped"
    if counter.get("table_missing"):
        return "failed"
    return "written" if counter.get("written") else "skipped"
