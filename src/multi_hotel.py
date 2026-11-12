from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any
from uuid import uuid4

from src.compare import normalize_room_type as normalize_room_type_builtin
from src.config import HotelConfig, find_hotel, load_hotels
from src.db_writer import DBWriterError, Jsonb, _load_database_url, psycopg, write_ota_room_prices_from_payload
from src.login_session import LoginReminder, login_reminder_due, mark_login_reminder_sent
from src.utils import ensure_dir, resolve_stay_dates

logger = logging.getLogger(__name__)
LOGIN_REQUIRED_PREFIX = "登录态异常："
LOGIN_FAILURE_STATUSES = {"login_required", "session_expired", "captcha_required"}


class SoldOutError(DBWriterError):
    """当前批次有抓价数据，但全部房型明确不可订（满房）。"""



def load_active_own_hotels(conn: Any) -> list[dict[str, Any]]:
    return _fetch_all(
        conn,
        "SELECT * FROM hotels WHERE hotel_role = 'own' AND is_active = TRUE ORDER BY hotel_key",
    )


def load_hotel(conn: Any, hotel_key: str) -> dict[str, Any] | None:
    rows = _fetch_all(conn, "SELECT * FROM hotels WHERE hotel_key = %s", (hotel_key,))
    return rows[0] if rows else None


def load_competitors_for_hotel(conn: Any, own_hotel_key: str) -> list[dict[str, Any]]:
    return _fetch_all(
        conn,
        """
        SELECT h.*, hc.priority
        FROM hotel_competitors hc
        JOIN hotels h ON h.hotel_key = hc.competitor_hotel_key
        WHERE hc.own_hotel_key = %s AND hc.is_active = TRUE AND h.is_active = TRUE
        ORDER BY hc.priority, h.hotel_key
        """,
        (own_hotel_key,),
    )


def load_push_routes(conn: Any, own_hotel_key: str, push_type: str = "business_snapshot") -> list[dict[str, Any]]:
    return _fetch_all(
        conn,
        """
        SELECT * FROM hotel_push_routes
        WHERE own_hotel_key = %s AND push_type = %s AND is_active = TRUE
        ORDER BY id
        """,
        (own_hotel_key, push_type),
    )


def load_room_type_mappings(conn: Any, hotel_key: str | None = None) -> list[dict[str, Any]]:
    return _fetch_all(
        conn,
        """
        SELECT * FROM room_type_mapping
        WHERE is_active = TRUE AND (hotel_key = %s OR hotel_key IS NULL)
        ORDER BY CASE WHEN hotel_key = %s THEN 0 ELSE 1 END, priority, id
        """,
        (hotel_key, hotel_key),
    )


def normalize_room_type(room_name: str, mappings: list[dict[str, Any]]) -> str | None:
    mapping = resolve_room_type_mapping(room_name, mappings)
    if mapping is not None:
        return str(mapping["normalized_room_type"])
    current = normalize_room_type_builtin(room_name)
    return current or None


def resolve_room_type_mapping(room_name: str, mappings: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the winning mapping so its attribute overrides reach the room profile."""
    folded = room_name.casefold()
    ordered = sorted(
        mappings,
        key=lambda item: (0 if item.get("hotel_key") else 1, int(item.get("priority") or 100000)),
    )
    for mapping in ordered:
        keyword = str(mapping.get("match_keyword") or "").casefold()
        if keyword and keyword in folded:
            return mapping
    return None


def build_variant_key(normalized_room_type: str, core_feature_signature: str) -> str:
    return f"{normalized_room_type}|{core_feature_signature}"


def create_job_run(conn: Any, job_type: str, own_hotel_key: str | None, metadata: dict[str, Any]) -> str:
    run_id = uuid4().hex
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO agent_runs (run_id, run_type, own_hotel_key, status, metadata, raw)
            VALUES (%s, %s, %s, 'running', %s, %s)
            """,
            (run_id, job_type, own_hotel_key, Jsonb(metadata), Jsonb(metadata)),
        )
    conn.commit()
    return run_id


def finish_job_run(conn: Any, run_id: str, status: str, error_message: str | None = None) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE agent_runs
            SET status = %s, finished_at = now(), error_message = %s, message = %s
            WHERE run_id = %s
            """,
            (status, error_message, error_message, run_id),
        )
    conn.commit()


def scrape_hotel_set(
    own_hotel_key: str,
    *,
    channel: str,
    profile_dir: str,
    headless: bool,
    wait_ms: int,
    stay_date: str | None,
    days: int,
    config_path: str,
    run_id: str | None = None,
    publish_latest: bool = True,
) -> Path:
    from src.capture import run_scrape_many
    from src.hermes import build_hermes

    stay_dates = resolve_stay_dates(stay_date, days)
    hotels = _hotel_set(own_hotel_key, config_path)
    output_dir = Path("output/hotel_sets") / own_hotel_key / stay_dates[0]
    scoped = [replace(hotel, output=str(output_dir / hotel.key)) for hotel in hotels]
    results = asyncio.run(
        run_scrape_many(
            hotels=scoped,
            channel=channel,
            profile_dir=profile_dir,
            headless=headless,
            wait_ms=wait_ms,
            stay_dates=stay_dates,
        )
    )
    failed = [item for item in results if item.get("page_status") != "success"]
    if failed:
        names = ", ".join(str(item.get("hotel_name") or item.get("url") or "unknown") for item in failed)
        if any(item.get("page_status") in {"login_required", "session_expired", "captcha_required"} for item in failed):
            raise DBWriterError(f"{LOGIN_REQUIRED_PREFIX}{names}")
        raise DBWriterError(f"本次抓取未完成：{names}")
    summary = build_hermes(config_path, str(output_dir), hotels=scoped, run_id=run_id)
    if summary["status"] not in {"success", "warning"}:
        raise DBWriterError(f"Hermes 结果异常：{summary['status']}")
    if publish_latest:
        _copy_latest(output_dir, ("hermes_payload.json", "rooms_all.jsonl", "price_compare.json", "run_summary.json"))
    return output_dir / "hermes_payload.json"


def build_price_comparisons(
    own_hotel_key: str,
    *,
    stay_date: str | None,
    days: int,
    write_db: bool,
    run_id: str | None = None,
    publish_latest: bool = True,
) -> list[dict[str, Any]]:
    stay_dates = resolve_stay_dates(stay_date, days)
    source_run_id = run_id
    run_id = run_id or uuid4().hex
    comparison_batch_id = f"{run_id}-{uuid4().hex[:8]}"
    warnings: list[str] = []
    with _connect() as conn:
        competitors = load_competitors_for_hotel(conn, own_hotel_key)
        competitor_count = len(competitors)
        mappings_by_hotel = {
            key: load_room_type_mappings(conn, key)
            for key in [own_hotel_key, *(row["hotel_key"] for row in competitors)]
        }
        comparisons: list[dict[str, Any]] = []
        summary_by_room_type: list[dict[str, Any]] = []
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cursor:
            for current_date in stay_dates:
                own = _latest_lowest_prices(cursor, own_hotel_key, current_date, mappings_by_hotel[own_hotel_key], source_run_id)
                competitor_prices: list[dict[str, Any]] = []
                for competitor in competitors:
                    competitor_key = competitor["hotel_key"]
                    try:
                        other = _latest_lowest_prices(cursor, competitor_key, current_date, mappings_by_hotel[competitor_key], source_run_id)
                    except SoldOutError:
                        warnings.append(f"竞品当日满房：{competitor_key}（{current_date} 批次房型全部不可订）")
                        continue
                    for room_type, competitor_row in other.items():
                        competitor_prices.append({"hotel_key": competitor_key, "normalized_room_type": room_type, **competitor_row})
                matches = match_competitor_candidates(own, competitor_prices)
                for room_type, own_row in sorted(own.items()):
                    match = matches[room_type]
                    normalized_room_type = _room_type(own_row)
                    competitor_row = match.get("competitor_min_source")
                    competitor_price = match.get("competitor_min_price")
                    own_price = float(own_row["price"])
                    item = {
                        "own_hotel_key": own_hotel_key,
                        "stay_date": current_date,
                        "normalized_room_type": normalized_room_type,
                        "core_feature_signature": own_row["core_feature_signature"],
                        "variant_key": build_variant_key(normalized_room_type, own_row["core_feature_signature"]),
                        "own_room_name": own_row["room_name"],
                        "own_min_price": own_price,
                        **{key: match[key] for key in (
                            "match_level", "match_reason", "room_difference_note",
                            "comparison_role", "can_drive_pricing", "selected_match", "market_floor_sample",
                            "market_candidate_count", "market_competitor_hotel_count", "market_price_band_width",
                            "market_min_price", "market_median_price", "market_max_price",
                            "pricing_candidate_count", "pricing_competitor_hotel_count", "pricing_price_band_width",
                            "pricing_min_price", "pricing_median_price", "pricing_max_price",
                            "own_vs_market_min_delta", "own_vs_market_median_delta",
                            "own_vs_pricing_min_delta", "own_vs_pricing_median_delta",
                            "mismatch_attributes", "unknown_attributes",
                            "competitor_min_price", "competitor_median_price", "competitor_max_price",
                            "competitor_available_count", "competitor_hotel_count", "competitor_price_spread",
                        )},
                        "own_vs_competitor_min_diff": round(own_price - competitor_price, 2) if competitor_price is not None else None,
                        "own_vs_competitor_median_diff": round(own_price - match["competitor_median_price"], 2) if match["competitor_median_price"] is not None else None,
                    }
                    summary_by_room_type.append(item)
                    if competitor_row is not None:
                        comparisons.append(
                            {
                                "run_id": run_id,
                                "own_hotel_key": own_hotel_key,
                                "competitor_hotel_key": competitor_row.get("hotel_key", ""),
                                "stay_date": current_date,
                                "normalized_room_type": normalized_room_type,
                                "core_feature_signature": own_row["core_feature_signature"],
                                "variant_key": item["variant_key"],
                                "own_room_name": own_row["room_name"],
                                "own_price": own_price,
                                "competitor_room_name": competitor_row["room_name"],
                                "competitor_price": competitor_price,
                                "price_diff": item["own_vs_competitor_min_diff"],
                                "price_diff_pct": round(item["own_vs_competitor_min_diff"] / competitor_price, 4) if competitor_price else None,
                                "collected_at": max(own_row["collected_at"], competitor_row["collected_at"]),
                                "match_level": match["match_level"], "comparison_role": match["comparison_role"], "can_drive_pricing": match["can_drive_pricing"],
                                "match_reason": match["match_reason"],
                                "room_difference_note": match["room_difference_note"],
                            }
                        )
            summary_map = {_comparison_variant_key(row): row for row in summary_by_room_type}
            for row in comparisons:
                summary = summary_map.get(_comparison_variant_key(row), {})
                row.update({key: summary.get(key) for key in (
                    "competitor_min_price", "competitor_median_price", "competitor_max_price",
                    "competitor_available_count", "competitor_hotel_count", "competitor_price_spread",
                )})
                row["raw"] = {"summary_by_room_type": summary, "match_level": row["match_level"], "match_reason": row["match_reason"], "room_difference_note": row["room_difference_note"]}
            if write_db:
                if not _table_exists(cursor, "price_comparisons"):
                    warnings.append("price_comparisons table missing; skipped db write")
                elif comparisons:
                    _insert_price_comparisons(cursor, comparisons)
        if write_db:
            conn.commit()
    output_dir = ensure_dir(Path("output/hotel_sets") / own_hotel_key / stay_dates[0])
    payload = {
        "run_id": run_id,
        "source_run_id": source_run_id or run_id,
        "comparison_batch_id": comparison_batch_id,
        "comparison_version": "multi-hotel-room-variant-v2",
        "generated_at": datetime.now().astimezone().isoformat(),
        "own_hotel_key": own_hotel_key,
        "comparisons": comparisons,
        "summary_by_room_type": summary_by_room_type,
        "warnings": warnings,
    }
    _write_json(output_dir / "price_compare.json", payload)
    if publish_latest:
        _copy_latest(output_dir, ("price_compare.json",))
    return comparisons


def run_scheduled_snapshot(
    *,
    own_hotel_key: str | None,
    all_active: bool,
    stay_date: str | None,
    days: int,
    push: bool,
    channel: str,
    profile_dir: str,
    wait_ms: int,
    config_path: str,
    dry_run: bool = False,
    stagger_seconds: int = 0,
    slot: str = "morning",
    skip_pms_snapshot: bool = False,
) -> list[dict[str, Any]]:
    with _connect() as conn:
        targets = load_active_own_hotels(conn) if all_active else [load_hotel(conn, own_hotel_key or "")]
    targets = [target for target in targets if target]
    if not targets:
        raise DBWriterError("没有找到启用的自有酒店")
    if dry_run:
        return [_dry_run_plan(hotel, stay_date, days) for hotel in targets]
    try:
        _preflight_login_session(
            targets[0]["hotel_key"],
            channel=channel,
            profile_dir=profile_dir,
            wait_ms=wait_ms,
            stay_date=stay_date,
            days=days,
            config_path=config_path,
        )
    except DBWriterError as exc:
        if str(exc).startswith(LOGIN_REQUIRED_PREFIX):
            try:
                _notify_admin_login_alert(
                    [{"own_hotel_key": targets[0]["hotel_key"], "hotel_name": targets[0].get("hotel_name")}]
                )
            except Exception:
                logger.exception("登录态预检告警发送失败")
        raise
    _send_login_maintenance_reminder(profile_dir)
    results = []
    login_failures = []
    for index, hotel in enumerate(targets):
        if index and stagger_seconds > 0:
            time.sleep(stagger_seconds)
        key = hotel["hotel_key"]
        with _connect() as conn:
            run_id = create_job_run(conn, "scheduled_snapshot", key, {"days": days, "push": push})
        _finish_stage(run_id, key, "load_hotel_set", "success")
        try:
            pms_warning = "PMS snapshot 被调用方跳过" if skip_pms_snapshot else _run_pms_snapshot(key, stay_date, days, refresh_monitor_baseline=(slot == "afternoon"))
            _finish_stage(run_id, key, "pms_snapshot", "failed" if pms_warning else "success")
            if pms_warning:
                _update_job_metadata(run_id, {"pms_skipped_reason": pms_warning})
            payload_path = _run_stage(run_id, key, "ota_scrape", lambda: scrape_hotel_set(
                key,
                channel=channel,
                profile_dir=profile_dir,
                headless=True,
                wait_ms=wait_ms,
                stay_date=stay_date,
                days=days,
                config_path=config_path,
                run_id=run_id,
                publish_latest=False,
            ))
            inserted = _run_stage(run_id, key, "db_write_prices", lambda: write_ota_room_prices_from_payload(str(payload_path), run_id=run_id))
            comparisons = _run_stage(run_id, key, "price_comparison", lambda: build_price_comparisons(key, stay_date=stay_date, days=days, write_db=True, run_id=run_id, publish_latest=False))
            # The first Hermes payload is only the capture-to-DB carrier.  Publish
            # the formal payload after comparison, from this exact result object.
            from src.hermes import build_hermes
            current_date = resolve_stay_dates(stay_date, days)[0]
            formal_output = Path("output/hotel_sets") / key / current_date
            formal_hotels = [replace(hotel, output=str(formal_output / hotel.key)) for hotel in _hotel_set(key, config_path)]
            business_payload = json.loads((formal_output / "price_compare.json").read_text(encoding="utf-8"))
            _run_stage(run_id, key, "business_artifacts", lambda: build_hermes(
                config_path, str(formal_output), hotels=formal_hotels,
                run_id=run_id, price_comparisons=business_payload,
            ))
            if pms_warning:
                with _connect() as conn:
                    finish_job_run(conn, run_id, "partial", pms_warning)
                results.append({"run_id": run_id, "own_hotel_key": key, "status": "partial", "ota_rows": inserted, "comparisons": len(comparisons), "decisions": 0, "warnings": [pms_warning]})
                continue
            decisions, decision_path = _run_stage(run_id, key, "price_decision", lambda: _run_price_decision(key, stay_date, days, config_path, run_id, business_payload))
            _publish_business_batch(formal_output, business_payload)
            report_path = _run_stage(run_id, key, "business_snapshot_report", lambda: _build_business_snapshot_report(key, stay_date, days, config_path, slot, publish_latest=False, run_id=run_id))
            warnings = _run_stage(run_id, key, "feishu_notify", lambda: _push_report(key, report_path, run_id)) if push else []
            status_name = "partial" if pms_warning or warnings else "success"
            with _connect() as conn:
                finish_job_run(conn, run_id, status_name)
            results.append(
                {
                    "run_id": run_id,
                    "own_hotel_key": key,
                    "status": status_name,
                    "ota_rows": inserted,
                    "comparisons": len(comparisons),
                    "decisions": len(decisions),
                    "decision_path": str(decision_path),
                    "warnings": [warning for warning in [pms_warning, *warnings] if warning],
                }
            )
        except SoldOutError as exc:
            reason = f"自有酒店当日满房，跳过本次快报推送：{exc}"
            with _connect() as conn:
                finish_job_run(conn, run_id, "skipped", reason)
            results.append({"run_id": run_id, "own_hotel_key": key, "hotel_name": hotel.get("hotel_name"), "status": "skipped", "reason": reason})
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            with _connect() as conn:
                finish_job_run(conn, run_id, "failed", error)
            result = {"run_id": run_id, "own_hotel_key": key, "hotel_name": hotel.get("hotel_name"), "status": "failed", "error": error}
            results.append(result)
            if error.startswith(f"DBWriterError: {LOGIN_REQUIRED_PREFIX}"):
                login_failures.append(result)
                # 同一 profile 已失效，继续请求其余酒店只会制造重复失败和告警。
                break
            if not all_active:
                raise
    if login_failures:
        _notify_admin_login_alert(login_failures)
    return results


def _preflight_login_session(
    own_hotel_key: str,
    *,
    channel: str,
    profile_dir: str,
    wait_ms: int,
    stay_date: str | None,
    days: int,
    config_path: str,
) -> None:
    """Probe one own-hotel page before the scheduled batch writes any data."""
    from src.capture import run_scrape

    hotels = _hotel_set(own_hotel_key, config_path)
    if not hotels:
        raise DBWriterError(f"登录态预检缺少酒店配置：{own_hotel_key}")
    hotel = hotels[0]
    current_date = resolve_stay_dates(stay_date, days)[0]
    with tempfile.TemporaryDirectory(prefix="ctrip-login-preflight-") as temporary_dir:
        result = asyncio.run(
            run_scrape(
                url=hotel.url,
                hotel_name=hotel.name,
                channel=channel,
                profile_dir=profile_dir,
                headless=True,
                wait_ms=wait_ms,
                output_dir=temporary_dir,
                stay_date=current_date,
            )
        )
    status = str(result.get("page_status") or "")
    if status in LOGIN_FAILURE_STATUSES:
        raise DBWriterError(f"{LOGIN_REQUIRED_PREFIX}{hotel.name}")


def _send_login_maintenance_reminder(profile_dir: str) -> None:
    reminder = login_reminder_due(profile_dir)
    if reminder is None:
        return
    try:
        _notify_admin_login_reminder(reminder)
    except Exception:
        logger.exception("携程登录态维护提醒发送失败")
        return
    mark_login_reminder_sent(profile_dir, reminder)


def _connect() -> Any:
    if psycopg is None or Jsonb is None:
        raise DBWriterError("缺少 psycopg 依赖，请执行 pip install -r requirements.txt")
    return psycopg.connect(_load_database_url(), row_factory=psycopg.rows.dict_row)


def _fetch_all(conn: Any, query: str, values: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cursor:
        cursor.execute(query, values)
        return list(cursor.fetchall())


def _hotel_set(own_hotel_key: str, config_path: str) -> list[HotelConfig]:
    try:
        with _connect() as conn:
            own = load_hotel(conn, own_hotel_key)
            if own:
                rows = [own, *load_competitors_for_hotel(conn, own_hotel_key)]
                return [_hotel_config(row) for row in rows]
    except Exception:
        logger.warning("酒店配置数据库不可用，已回退 hotels.json")
    own = find_hotel(config_path, own_hotel_key)
    hotels = load_hotels(config_path)
    competitor_keys = set(own.competitors) or {hotel.key for hotel in hotels if hotel.role == "competitor"}
    return [own, *(hotel for hotel in hotels if hotel.key in competitor_keys)]


def _hotel_config(row: dict[str, Any]) -> HotelConfig:
    key = str(row["hotel_key"])
    return HotelConfig(
        key=key,
        role=str(row["hotel_role"]),
        name=str(row["hotel_name"]),
        url=str(row.get("ota_url") or ""),
        output=f"output/{key}",
    )


def _comparison_variant_key(row: dict[str, Any]) -> tuple[str, str, str]:
    """Keep same-name room variants isolated when enriching comparison rows."""
    return (
        str(row.get("stay_date") or ""),
        str(row.get("normalized_room_type") or ""),
        str(row.get("core_feature_signature") or ""),
    )


def _latest_lowest_prices(cursor: Any, hotel_key: str, stay_date: str, mappings: list[dict[str, Any]], run_id: str | None = None) -> dict[str, dict[str, Any]]:
    if run_id:
        cursor.execute(
            """
            SELECT room_name, normalized_room_type, price, collected_at
            FROM ota_room_prices
            WHERE source_hotel_key = %s AND stay_date = %s AND run_id = %s
              AND available = TRUE AND price > 0
            """,
            (hotel_key, stay_date, run_id),
        )
        rows = cursor.fetchall()
        if not rows:
            cursor.execute(
                """
                SELECT count(*) FILTER (WHERE available IS FALSE) AS unavailable,
                       count(*) FILTER (WHERE available IS NULL) AS unknown
                FROM ota_room_prices
                WHERE source_hotel_key = %s AND stay_date = %s AND run_id = %s
                """,
                (hotel_key, stay_date, run_id),
            )
            counts = cursor.fetchone()
            if counts["unavailable"] and not counts["unknown"]:
                raise SoldOutError(f"{hotel_key} 当日房型全部明确不可订（满房）")
            raise DBWriterError(f"{hotel_key} 缺少当前批次 OTA 价格")
    else:
        cursor.execute(
            """
            SELECT run_id FROM ota_room_prices
            WHERE source_hotel_key = %s AND stay_date = %s AND run_id IS NOT NULL
              AND available = TRUE AND price > 0
            ORDER BY collected_at DESC LIMIT 1
            """,
            (hotel_key, stay_date),
        )
        latest = cursor.fetchone()
        if latest:
            cursor.execute(
                """
                SELECT room_name, normalized_room_type, price, collected_at
                FROM ota_room_prices
                WHERE source_hotel_key = %s AND stay_date = %s AND run_id = %s
                  AND available = TRUE AND price > 0
                """,
                (hotel_key, stay_date, latest["run_id"]),
            )
        else:
            cursor.execute(
                """
                SELECT DISTINCT ON (COALESCE(normalized_room_type, room_category))
                       room_name, COALESCE(normalized_room_type, room_category) AS normalized_room_type, price, collected_at
                FROM ota_room_prices
                WHERE source_hotel_key = %s AND stay_date = %s AND available = TRUE AND price > 0
                ORDER BY COALESCE(normalized_room_type, room_category), collected_at DESC, price ASC
                """,
                (hotel_key, stay_date),
            )
        rows = cursor.fetchall()
    # Keep premium variants separate before selecting their lowest price.
    lowest: dict[str, dict[str, Any]] = {}
    for row in rows:
        mapping = resolve_room_type_mapping(str(row["room_name"]), mappings)
        room_type = str((mapping or {}).get("normalized_room_type") or row.get("normalized_room_type") or normalize_room_type_builtin(str(row["room_name"])) or "other")
        enriched = {
            **dict(row),
            "normalized_room_type": room_type,
            "attribute_overrides": {
                **dict(row.get("attribute_overrides") or {}),
                **dict((mapping or {}).get("attribute_overrides") or {}),
            },
        }
        profile = _room_profile(room_type, enriched)
        enriched["room_attributes"] = profile
        enriched["core_feature_signature"] = _core_feature_signature(profile)
        variant_key = build_variant_key(room_type, enriched["core_feature_signature"])
        current = lowest.get(variant_key)
        if current is None or row["price"] < current["price"]:
            lowest[variant_key] = enriched
    return lowest


def match_competitor_candidates(own_by_room_type: dict[str, dict[str, Any]], competitor_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Match each own-room category against the best usable competitor candidate."""
    return {
        room_type: _match_candidate(room_type, own_row, competitor_rows)
        for room_type, own_row in own_by_room_type.items()
    }


def _match_candidate(room_type: str, own_row: dict[str, Any], competitor_rows: list[dict[str, Any]]) -> dict[str, Any]:
    own = _room_profile(room_type, own_row)
    own["price"] = _float_or_none(own_row.get("price"))
    comparable_rows = [
        row for row in competitor_rows
        if _two_person_bed_compatible(own, _room_profile(_room_type(row), row))
    ]
    exact = [row for row in comparable_rows if _room_type(row) == _room_type(own_row) and _core_feature_signature(_room_profile(_room_type(row), row)) == _core_feature_signature(own)]
    same_occupancy = [row for row in comparable_rows if own["occupancy"] is not None and _room_profile(_room_type(row), row)["occupancy"] == own["occupancy"]]
    if exact:
        return _with_variant_key(_match_result("exact", own, exact, "归一化房型与核心产品属性一致"), room_type)
    if same_occupancy:
        return _with_variant_key(_match_result("comparable", own, same_occupancy, "入住人数一致，按核心产品属性、电竞属性、床型和档次排序"), room_type)
    if comparable_rows:
        return _with_variant_key(_match_result("benchmark", own, comparable_rows, "无同入住人数竞品，作为市场价格带参考"), room_type)
    return {
        "variant_key": build_variant_key(_room_type(own_row), _core_feature_signature(own)),
        "match_level": "none",
        "match_reason": "当天绑定竞品没有有效可售数据",
        "room_difference_note": "无可用竞品价格",
        "comparison_role": "unavailable",
        "can_drive_pricing": False,
        "selected_match": None,
        "market_floor_sample": None,
        "market_candidate_count": 0,
        "market_competitor_hotel_count": 0,
        "market_min_price": None,
        "market_median_price": None,
        "market_max_price": None,
        "market_price_band_width": None,
        "pricing_candidate_count": 0,
        "pricing_competitor_hotel_count": 0,
        "pricing_min_price": None,
        "pricing_median_price": None,
        "pricing_max_price": None,
        "pricing_price_band_width": None,
        "own_vs_market_min_delta": None,
        "own_vs_market_median_delta": None,
        "own_vs_pricing_min_delta": None,
        "own_vs_pricing_median_delta": None,
        "mismatch_attributes": [],
        "unknown_attributes": [],
        "competitor_min_source": None,
        "competitor_min_price": None,
        "competitor_median_price": None,
        "competitor_max_price": None,
        "competitor_available_count": 0,
        "competitor_hotel_count": 0,
        "competitor_price_spread": None,
        "competitor_price_rows": [],
    }


def _match_result(level: str, own: dict[str, Any], candidates: list[dict[str, Any]], reason: str) -> dict[str, Any]:
    direct_candidates = [
        row for row in candidates
        if level == "exact"
        and not _direct_comparison_gaps(own, _room_profile(_room_type(row), row))
    ]
    selected = min(direct_candidates or candidates, key=lambda row: _candidate_sort_key(level, own, row))
    prices = sorted(float(row["price"]) for row in candidates)
    selected_profile = _room_profile(_room_type(selected), selected)
    mismatches = _mismatch_attributes(own, selected_profile)
    direct_gaps = _direct_comparison_gaps(own, selected_profile)
    can_drive = level == "exact" and bool(direct_candidates)
    if can_drive:
        role = "direct_peer"
    elif level == "benchmark":
        role = "market_benchmark"
    elif "gpu" in mismatches:
        role = "hardware_mismatch_reference"
    elif _premium_mismatch(own, selected_profile):
        role = "premium_mismatch_reference"
    elif direct_gaps:
        role = "unverified_feature_reference"
    else:
        role = "feature_mismatch_reference"
    pricing_rows = direct_candidates if can_drive else []
    pricing_prices = sorted(float(row["price"]) for row in pricing_rows)
    market_floor = min(candidates, key=lambda row: float(row["price"]))
    own_price = _float_or_none(own.get("price"))
    pricing_min = pricing_prices[0] if pricing_prices else None
    pricing_median = median(pricing_prices) if pricing_prices else None
    market_min = prices[0]
    market_median = median(prices)
    return {
        "core_feature_signature": _core_feature_signature(own),
        "match_level": level,
        "match_reason": reason,
        "room_difference_note": _room_difference_note(own, selected_profile, level),
        "competitor_min_source": selected if can_drive else None,
        "competitor_min_price": pricing_min,
        "competitor_median_price": pricing_median,
        "competitor_max_price": pricing_prices[-1] if pricing_prices else None,
        "competitor_available_count": len(pricing_rows),
        "competitor_hotel_count": len({str(row.get("hotel_key") or "") for row in pricing_rows if row.get("hotel_key")}),
        "competitor_price_spread": round(pricing_prices[-1] - pricing_prices[0], 2) if pricing_prices else None,
        "competitor_price_rows": candidates,
        "comparison_role": role,
        "can_drive_pricing": can_drive,
        "selected_match": selected,
        "market_floor_sample": market_floor,
        "market_candidate_count": len(candidates),
        "market_competitor_hotel_count": len({str(row.get("hotel_key") or "") for row in candidates if row.get("hotel_key")}),
        "market_min_price": market_min,
        "market_median_price": market_median,
        "market_max_price": prices[-1],
        "market_price_band_width": round(prices[-1] - market_min, 2),
        "pricing_candidate_count": len(pricing_rows),
        "pricing_competitor_hotel_count": len({str(row.get("hotel_key") or "") for row in pricing_rows if row.get("hotel_key")}),
        "pricing_min_price": pricing_min,
        "pricing_median_price": pricing_median,
        "pricing_max_price": pricing_prices[-1] if pricing_prices else None,
        "pricing_price_band_width": round(pricing_prices[-1] - pricing_prices[0], 2) if pricing_prices else None,
        "own_vs_market_min_delta": round(own_price - market_min, 2) if own_price is not None else None,
        "own_vs_market_median_delta": round(own_price - market_median, 2) if own_price is not None else None,
        "own_vs_pricing_min_delta": round(own_price - pricing_min, 2) if own_price is not None and pricing_min is not None else None,
        "own_vs_pricing_median_delta": round(own_price - pricing_median, 2) if own_price is not None and pricing_median is not None else None,
        "mismatch_attributes": _mismatch_attributes(own, selected_profile),
        "unknown_attributes": [],
    }


def _with_variant_key(result: dict[str, Any], room_type: str) -> dict[str, Any]:
    result["variant_key"] = build_variant_key(room_type, str(result["core_feature_signature"]))
    return result


def _mismatch_attributes(own: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    feature_keys = ("bed", "has_bathtub", "has_mahjong", "room_form", "window_type", "gpu")
    return [key for key in feature_keys if own.get(key) != candidate.get(key)]


def _two_person_bed_compatible(own: dict[str, Any], candidate: dict[str, Any]) -> bool:
    # 双床/双人已归一为 double；双人产品没有一致且已注明的床型时不进入任何比价层级。
    if own.get("occupancy") != 2 or candidate.get("occupancy") != 2:
        return True
    return own.get("bed") != "unknown" and own.get("bed") == candidate.get("bed")


def _direct_comparison_gaps(own: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    gaps: list[str] = []
    # 床型只区分双人产品；多人房没有明确床型时不降低可比性。
    if own.get("occupancy") == 2 and (own.get("bed") == "unknown" or candidate.get("bed") == "unknown"):
        gaps.append("床型未注明")
    if own.get("window_type") == "unknown" or candidate.get("window_type") == "unknown":
        gaps.append("窗型未注明")
    if own.get("esports") or candidate.get("esports"):
        if not own.get("gpu") or not candidate.get("gpu"):
            gaps.append("显卡未注明")
    return gaps


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _candidate_sort_key(level: str, own: dict[str, Any], row: dict[str, Any]) -> tuple[Any, ...]:
    if level == "benchmark":
        return (float(row["price"]),)
    candidate = _room_profile(_room_type(row), row)
    tier_gap = abs(own["tier"] - candidate["tier"]) if own["tier"] is not None and candidate["tier"] is not None else 99
    return (
        candidate["esports"] != own["esports"],
        candidate["bed"] != own["bed"],
        candidate["window_type"] != own["window_type"],
        candidate["gpu"] != own["gpu"],
        tier_gap,
        candidate["name"] != own["name"],
        float(row["price"]),
    )


def _room_type(row: dict[str, Any]) -> str:
    return str(row.get("normalized_room_type") or row.get("room_category") or "other")


def _hardware_profile(text: str) -> dict[str, str | None]:
    gpu_match = re.search(r"(?<!\d)(?:rtx|gtx)?\s*((?:10|16|20|30|40|50)\d{2})(?:\s*(ti|super))?(?!\d)", text, re.IGNORECASE)
    return {"gpu": f"{gpu_match.group(1)}{(gpu_match.group(2) or '').upper()}" if gpu_match else None}


def _room_profile(room_type: str, row: dict[str, Any]) -> dict[str, Any]:
    name = str(row.get("room_name") or "")
    text = f"{room_type} {name}".casefold()
    bed_text = f"{name} {row.get('bed_info') or ''}".casefold()
    window_text = f"{name} {row.get('window_info') or ''} {row.get('window_type') or ''}".casefold()
    capacity = next((value for token, value in (("single", 1), ("单人", 1), ("单机", 1), ("double", 2), ("twin", 2), ("双人", 2), ("大床", 2), ("双床", 2), ("triple", 3), ("三人", 3), ("quad", 4), ("四人", 4), ("penta", 5), ("five", 5), ("五人", 5), ("hexa", 6), ("six", 6), ("六人", 6)) if token in text), None)
    # 双床与双人房属于同一双人产品；仅与一张大床产品区分。
    bed = "double" if "双床" in bed_text or "两张" in bed_text or "2张" in bed_text or "双人" in bed_text else "big_bed" if "大床" in bed_text or "特大床" in bed_text else "unknown"
    tier = next((index for index, token in enumerate(("特惠", "舒适", "精选", "雅致", "豪华", "甄选", "奢华")) if token in name), None)
    attrs = dict(row.get("attribute_overrides") or {})
    def flag(key: str, *tokens: str) -> bool | None:
        if key in attrs:
            return attrs[key]
        return True if any(token in text for token in tokens) else None
    form = attrs.get("room_form") or ("loft" if "loft" in text else "duplex" if "复式" in text or "複式" in text else "suite" if "套房" in text or "套间" in text or "套間" in text else "standard")
    # OTA listing convention: rooms are assumed to have an exterior window unless the source explicitly says otherwise.
    window = attrs.get("window_type") or ("no_window" if "无窗" in window_text or "無窗" in window_text or "暗窗" in window_text else "inner_window" if "内窗" in window_text or "內窗" in window_text else "window")
    hardware = _hardware_profile(f"{name} {row.get('hardware_info') or ''} {row.get('bed_info') or ''}")
    return {"name": re.sub(r"[\s\W_]+", "", name).casefold(), "occupancy": capacity, "esports": "esports" in text or "电竞" in text or "游戏" in text, "bed": bed, "tier": tier, "has_bathtub": flag("has_bathtub", "浴缸", "泡池", "浴池"), "has_mahjong": flag("has_mahjong", "棋牌", "麻将", "麻將"), "room_form": form, "window_type": window, **hardware}


def _core_feature_signature(profile: dict[str, Any]) -> str:
    def value(key: str) -> str:
        item = profile.get(key)
        return "unknown" if item is None else str(item).lower()
    # 电竞房显卡是直接比价硬条件；其他硬件仅在双方均已注明且不同才降为参考价。
    return "|".join(f"{key}={value(key)}" for key in ("occupancy", "bed", "has_bathtub", "has_mahjong", "room_form", "window_type", "gpu"))


def _premium_mismatch(own: dict[str, Any], candidate: dict[str, Any]) -> bool:
    return any(own.get(key) is True and candidate.get(key) is not True for key in ("has_bathtub", "has_mahjong")) or (own.get("room_form") in {"loft", "duplex", "suite", "party_suite"} and own.get("room_form") != candidate.get("room_form")) or (own.get("window_type") != "unknown" and candidate.get("window_type") != "unknown" and own.get("window_type") != candidate.get("window_type"))


def _display_attribute(key: str, value: Any) -> str:
    labels = {
        "bed": {"big_bed": "大床", "twin": "双床", "double": "双人", "unknown": "未注明"},
        "window_type": {"window": "有窗", "no_window": "无窗", "inner_window": "内窗", "unknown": "未注明"},
        "has_bathtub": {True: "有", False: "无", None: "未注明"},
        "has_mahjong": {True: "有", False: "无", None: "未注明"},
        "room_form": {"standard": "标准", "loft": "Loft", "duplex": "复式", "suite": "套房", "party_suite": "派对套房"},
    }
    return str(labels.get(key, {}).get(value, value or "未注明"))


def _room_difference_note(own: dict[str, Any], candidate: dict[str, Any], level: str) -> str:
    if level == "benchmark":
        return "入住人数不同，仅作市场价格带参考"
    differences: list[str] = []
    labels = {"bed": "床型", "window_type": "窗型", "has_bathtub": "浴缸", "has_mahjong": "棋牌", "room_form": "房型结构", "gpu": "显卡"}
    for key, label in labels.items():
        different = own.get(key) != candidate.get(key)
        if different:
            differences.append(f"{label}：我方{_display_attribute(key, own.get(key))}/竞品{_display_attribute(key, candidate.get(key))}")
    if own["esports"] != candidate["esports"]:
        differences.append("电竞属性不同")
    if own["tier"] is not None and candidate["tier"] is not None and own["tier"] != candidate["tier"]:
        differences.append("房型档次不同")
    gaps = _direct_comparison_gaps(own, candidate)
    differences.extend(gap for gap in gaps if gap not in differences)
    if differences:
        return "；".join(differences)
    return "同类房型" if level == "exact" else "同入住人数，相近房型"


def _table_exists(cursor: Any, table_name: str) -> bool:
    cursor.execute("SELECT to_regclass(%s) IS NOT NULL AS exists", (f"public.{table_name}",))
    row = cursor.fetchone()
    return bool(row and row["exists"])


def _table_columns(cursor: Any, table_name: str) -> set[str]:
    cursor.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        """,
        (table_name,),
    )
    return {str(row["column_name"]) for row in cursor.fetchall()}


def _insert_price_comparisons(cursor: Any, comparisons: list[dict[str, Any]]) -> None:
    columns = [
        "run_id", "own_hotel_key", "competitor_hotel_key", "stay_date",
        "normalized_room_type", "own_room_name", "own_price",
        "competitor_room_name", "competitor_price", "price_diff",
        "price_diff_pct", "collected_at", "competitor_min_price",
        "competitor_median_price", "competitor_max_price",
        "competitor_available_count", "competitor_hotel_count",
        "competitor_price_spread", "raw",
    ]
    available = _table_columns(cursor, "price_comparisons")
    insert_columns = [column for column in columns if column in available]
    values_sql = ", ".join(f"%({column})s" for column in insert_columns)
    column_sql = ", ".join(insert_columns)
    rows = []
    for row in comparisons:
        item = dict(row)
        if "raw" in insert_columns:
            item["raw"] = Jsonb(_json_safe(item.get("raw") or {}))
        rows.append(item)
    cursor.executemany(f"INSERT INTO price_comparisons ({column_sql}) VALUES ({values_sql})", rows)


def _run_pms_snapshot(own_hotel_key: str, stay_date: str | None, days: int, *, refresh_monitor_baseline: bool = False) -> str | None:
    from src.pms_snapshot import write_pms_snapshot

    try:
        write_pms_snapshot(stay_date=stay_date, days=days, own_hotel_key=own_hotel_key, refresh_monitor_baseline=refresh_monitor_baseline)
        return None
    except Exception as exc:
        return f"PMS 已跳过：{type(exc).__name__}: {exc}"


def _run_price_decision(own_hotel_key: str, stay_date: str | None, days: int, config_path: str, run_id: str, business_comparison: dict[str, Any] | None = None, persist: bool = True) -> tuple[list[dict[str, Any]], Path]:
    from src.price_decision import generate_price_decision_brief

    current_date = resolve_stay_dates(stay_date, days)[0]
    output = Path("output/hotel_sets") / own_hotel_key / current_date / "price_decision_brief.md"
    decisions, _, _, _ = generate_price_decision_brief(
        stay_date=stay_date,
        days=days,
        own_hotel_key=own_hotel_key,
        output_path=str(output),
        config_path=config_path,
        run_id=run_id,
        business_comparison=business_comparison,
        persist=persist,
    )
    return decisions, output


def _build_business_snapshot_report(own_hotel_key: str, stay_date: str | None, days: int, config_path: str, slot: str = "morning", publish_latest: bool = True, run_id: str | None = None) -> Path:
    from src.business_snapshot import build_business_snapshot_report

    current_date = resolve_stay_dates(stay_date, days)[0]
    output = Path("output/hotel_sets") / own_hotel_key / current_date / "business_snapshot_report.json"
    build_business_snapshot_report(
        stay_date=stay_date,
        days=days,
        detail_level="full",
        config_path=config_path,
        output_path=str(output),
        own_hotel_key=own_hotel_key,
        slot=slot,
        run_id=run_id,
    )
    if publish_latest:
        _copy_latest(output.parent, (output.name,))
    return output


def _run_stage(run_id: str, own_hotel_key: str, stage_name: str, func: Any) -> Any:
    from src.run_stage_logger import fail_stage, finish_stage, start_stage

    stage_id = start_stage(run_id, own_hotel_key, stage_name)
    try:
        result = func()
    except Exception as exc:
        fail_stage(stage_id, f"{type(exc).__name__}: {exc}")
        raise
    finish_stage(stage_id, "success")
    return result


def _finish_stage(run_id: str, own_hotel_key: str, stage_name: str, status: str) -> None:
    from src.run_stage_logger import finish_stage, start_stage

    stage_id = start_stage(run_id, own_hotel_key, stage_name)
    finish_stage(stage_id, status)


def _dry_run_plan(hotel: dict[str, Any], stay_date: str | None, days: int) -> dict[str, Any]:
    key = str(hotel["hotel_key"])
    with _connect() as conn:
        competitors = load_competitors_for_hotel(conn, key)
        routes = load_push_routes(conn, key)
    return {
        "own_hotel_key": key,
        "hotel_name": hotel.get("hotel_name"),
        "status": "skipped",
        "dry_run": True,
        "stay_dates": resolve_stay_dates(stay_date, days),
        "competitor_count": len(competitors),
        "push_route_count": len(routes),
        "push_route_env_keys": [str(route.get("webhook_env_key") or "") for route in routes],
        "pms_config": _pms_config_status(key),
        "stages": [
            "load_hotel_set",
            "pms_snapshot",
            "ota_scrape",
            "db_write_prices",
            "price_comparison",
            "price_decision",
            "business_snapshot_report",
            "feishu_notify",
        ],
    }


def _pms_config_status(own_hotel_key: str) -> dict[str, Any]:
    values = _env_file_values((Path.home() / ".hotel_agent/pms_multi.env"))
    old_values = _env_file_values((Path.home() / "hotel_pms_agent/.env"))
    prefix = own_hotel_key.upper()
    legacy = own_hotel_key == "own_demo_a"
    old_fallback = {
        f"PMS_{prefix}_HOTEL_CODE": (os.getenv("XZ_HOTEL_CODE", "").strip() or old_values.get("XZ_HOTEL_CODE", "")) if legacy else "",
        f"PMS_{prefix}_PARTNER_CODE": (os.getenv("XZ_PARTNER_ID", "").strip() or old_values.get("XZ_PARTNER_ID", "")) if legacy else "",
        f"PMS_{prefix}_CLIENT_SECRET": (os.getenv("XZ_CLIENT_SECRET", "").strip() or old_values.get("XZ_CLIENT_SECRET", "")) if legacy else "",
    }
    required = list(old_fallback)
    base_url_keys = [f"PMS_{prefix}_BASE_URL", "PMS_BASE_URL"]
    missing = [key for key in required if not values.get(key) and not os.getenv(key, "").strip() and not old_fallback[key]]
    has_base_url = (
        any(values.get(key) or os.getenv(key, "").strip() for key in base_url_keys)
        or bool((os.getenv("XZ_BASE_URL", "").strip() or old_values.get("XZ_BASE_URL", "")) if legacy else "")
    )
    if not has_base_url:
        missing.append("PMS_BASE_URL")
    return {"complete": not missing, "missing_keys": missing}


def _env_file_values(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip().removeprefix("export ").strip()] = value.strip().strip("'\"")
    return values


def _write_hotel_report(
    hotel: dict[str, Any],
    stay_date: str | None,
    days: int,
    comparisons: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    status: dict[str, Any] | None,
    pms_warning: str | None,
) -> Path:
    current_date = resolve_stay_dates(stay_date, days)[0]
    lines = [
        f"【{hotel['hotel_name']}｜经营快照】",
        "",
        f"日期：{current_date}",
        f"入住率：{status.get('occupancy_rate') if status else '缺失'}%" if status else "入住率：缺失",
        f"今日可售：{status.get('available_rooms') if status else '缺失'} 间",
        f"今日已售：{status.get('sold_rooms') if status else '缺失'} 间",
        f"今日预订：{status.get('booking_count') if status else '缺失'} 间",
        "",
        "一、今日价格概况",
    ]
    if comparisons:
        for row in comparisons[:5]:
            lines.append(
                f"- {row['normalized_room_type']}：自有 ¥{row['own_price']:.2f}，"
                f"竞品 ¥{row['competitor_price']:.2f}，价差 ¥{row['price_diff']:.2f}"
            )
    else:
        lines.append("- 暂无同类房型价格对比")
    lines.extend(["", "二、经营建议"])
    if decisions:
        for row in decisions[:5]:
            lines.append(f"- {row['room_category']}：{row['recommended_action']}；{row['reason']}")
    else:
        lines.append("- 暂无可生成的调价建议")
    if pms_warning:
        lines.extend(["", f"提示：{pms_warning}"])
    output = ensure_dir(Path("output/hotel_sets") / hotel["hotel_key"] / current_date) / "business_snapshot_report.json"
    _write_json(
        output,
        {"own_hotel_key": hotel["hotel_key"], "hotel_name": hotel["hotel_name"], "feishu_message": "\n".join(lines)},
    )
    _copy_latest(output.parent, (output.name,))
    return output


def _latest_status(own_hotel_key: str, stay_date: str) -> dict[str, Any] | None:
    with _connect() as conn:
        rows = _fetch_all(
            conn,
            """
            SELECT hotel_key, stay_date, room_total, occupied_rooms, available_rooms,
                   sold_rooms, booking_count, occupancy_rate, collected_at
            FROM own_daily_status
            WHERE hotel_key = %s AND stay_date = %s
            ORDER BY collected_at DESC LIMIT 1
            """,
            (own_hotel_key, stay_date),
        )
    return rows[0] if rows else None


def _update_job_metadata(run_id: str, metadata: dict[str, Any]) -> None:
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE agent_runs SET metadata = COALESCE(metadata, '{}'::jsonb) || %s WHERE run_id = %s",
                (Jsonb(metadata), run_id),
            )
        conn.commit()


def _push_report(own_hotel_key: str, report_path: Path, source_run_id: str) -> list[str]:
    with _connect() as conn:
        routes = load_push_routes(conn, own_hotel_key)
    warnings = []
    if not routes:
        return [f"{own_hotel_key} 未配置 business_snapshot 推送路由"]
    webhook_env = _env_file_values((Path.home() / ".hotel_agent/webhook.env"))
    for route in routes:
        env_key = str(route["webhook_env_key"])
        if not (os.getenv(env_key, "").strip() or webhook_env.get(env_key)):
            warnings.append(f"未配置 webhook 环境变量：{env_key}")
            continue
        subprocess.run(
            [
                sys.executable,
                "scripts/notify_feishu.py",
                "--mode",
                "business-snapshot",
                "--report",
                str(report_path),
                "--webhook-env-key",
                env_key,
                "--branch-push-hotel-key",
                own_hotel_key,
                "--branch-push-type",
                "business_snapshot",
                "--branch-push-source-type",
                "scheduled_snapshot",
                "--branch-push-source-id",
                source_run_id,
            ],
            check=True,
            timeout=30,
        )
    return warnings


def _notify_admin_login_alert(results: list[dict[str, Any]]) -> None:
    hotel_keys = "、".join(str(result.get("hotel_name") or result["own_hotel_key"]) for result in results)
    subprocess.run(
        [
            sys.executable,
            "scripts/notify_feishu.py",
            "--mode",
            "business-snapshot",
            "--status",
            "failed",
            "--reason",
            f"{LOGIN_REQUIRED_PREFIX}{hotel_keys}",
            "--webhook-env-key",
            "ADMIN_FEISHU_WEBHOOK_URL",
        ],
        check=True,
        timeout=30,
    )


def _notify_admin_login_reminder(reminder: LoginReminder) -> None:
    subprocess.run(
        [
            sys.executable,
            "scripts/notify_feishu.py",
            "--mode",
            "login-reminder",
            "--reason",
            f"当前登录态已连续使用 {reminder.age_days} 天；请在未来 5 天内择时完成一次人工重登。",
            "--webhook-env-key",
            "ADMIN_FEISHU_WEBHOOK_URL",
        ],
        check=True,
        timeout=30,
    )


def _copy_latest(source_dir: Path, names: tuple[str, ...]) -> None:
    latest = ensure_dir(Path("output/latest"))
    for name in names:
        source = source_dir / name
        if source.is_file():
            shutil.copy2(source, latest / name)


def _publish_business_batch(source_dir: Path, business_payload: dict[str, Any]) -> Path:
    """Publish comparison artifacts only after their shared metadata validates."""
    batch_id = str(business_payload.get("comparison_batch_id") or "")
    if not batch_id:
        raise DBWriterError("业务比较缺少 comparison_batch_id，拒绝发布")
    root = ensure_dir(Path("output/business_runs"))
    temp_dir = root / f".tmp-{batch_id}"
    final_dir = root / batch_id
    if temp_dir.exists() or final_dir.exists():
        raise DBWriterError(f"比较批次目录已存在：{batch_id}")
    temp_dir.mkdir(parents=True)
    names = ("price_compare.json", "hermes_payload.json", "run_summary.json")
    try:
        for name in names:
            shutil.copy2(source_dir / name, temp_dir / name)
        payloads = {name: json.loads((temp_dir / name).read_text(encoding="utf-8")) for name in names}
        meta_keys = ("comparison_batch_id", "source_run_id", "comparison_version")
        if any(any(payload.get(key) != business_payload.get(key) for key in meta_keys) for payload in payloads.values()):
            raise DBWriterError("业务产物批次元数据不一致，拒绝发布")
        expected = {str(row.get("variant_key")) for row in business_payload.get("summary_by_room_type", []) if row.get("variant_key")}
        actual = set(payloads["run_summary.json"].get("comparison_variant_keys") or [])
        if expected != actual:
            raise DBWriterError("业务产物 variant_key 集合不一致，拒绝发布")
        os.replace(temp_dir, final_dir)
        manifest = {key: business_payload.get(key) for key in (*meta_keys, "generated_at")}
        manifest["directory"] = final_dir.as_posix()
        manifest_temp = Path("output/.latest_business_manifest.json.tmp")
        _write_json(manifest_temp, manifest)
        os.replace(manifest_temp, Path("output/latest_business_manifest.json"))
        _copy_latest(final_dir, names)
        return final_dir
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        raise


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _json_safe(value: Any) -> Any:
    """Convert DB-native values before handing comparison audit context to JSONB."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))
