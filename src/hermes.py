from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .compare import available_room_records, lowest_available_rooms_by_type, normalize_room_name, normalize_room_type
from .config import HotelConfig, load_hotels
from .utils import ensure_dir, now_iso, today_str


def build_hermes(
    config_path: str,
    output_dir: str = "output/latest",
    hotels: list[HotelConfig] | None = None,
    run_id: str | None = None,
    price_comparisons: list[dict[str, Any]] | dict[str, Any] | None = None,
) -> dict[str, Any]:
    captured_at = now_iso()
    latest_dir = ensure_dir(Path(output_dir))
    hotels = hotels if hotels is not None else load_hotels(config_path)

    own_hotels: list[dict[str, Any]] = []
    competitor_hotels: list[dict[str, Any]] = []
    unknown_hotels: list[dict[str, Any]] = []
    alerts: list[dict[str, Any]] = []
    source_files: list[str] = []
    debug_files: dict[str, Any] = {}
    missing_outputs: list[dict[str, str]] = []
    skipped_hotels: list[dict[str, str]] = []
    need_manual_login = False
    need_parser_update = False

    for hotel in hotels:
        hotel_obj, hotel_alerts, hotel_flags = _load_hotel_result(hotel, captured_at)
        alerts.extend(hotel_alerts)
        if hotel_obj["status"] != "success":
            alerts.append(
                {
                    "level": "warning",
                    "message": "酒店本次价格抓取未成功，已停止使用该酒店输出",
                    "hotel_key": hotel.key,
                    "hotel_name": hotel.name,
                    "capture_status": hotel_obj["status"],
                }
            )
        need_manual_login = need_manual_login or hotel_flags["need_manual_login"]
        need_parser_update = need_parser_update or hotel_flags["need_parser_update"]
        if hotel_flags.get("missing_output"):
            missing_outputs.append({"key": hotel.key, "name": hotel.name, "path": str(Path(hotel.output) / "ctrip_rooms.json")})
        if hotel_flags.get("skipped_missing_url"):
            skipped_hotels.append({"key": hotel.key, "name": hotel.name, "reason": "skipped_missing_url"})

        source_path = Path(hotel.output) / "ctrip_rooms.json"
        if source_path.exists():
            source_files.append(str(source_path))
        debug_files[hotel.key] = {
            "run_summary": str(Path(hotel.output) / "debug" / "run_summary.txt"),
            "page_text": str(Path(hotel.output) / "debug" / "page_text.txt"),
            "network_candidates": str(Path(hotel.output) / "debug" / "network_candidates.csv"),
        }

        if hotel_obj["role"] == "own":
            own_hotels.append(hotel_obj)
        elif hotel_obj["role"] == "competitor":
            competitor_hotels.append(hotel_obj)
        else:
            unknown_hotels.append(hotel_obj)

    if not own_hotels:
        alerts.append({"level": "warning", "message": "missing_own_hotel"})
    if not competitor_hotels:
        alerts.append({"level": "warning", "message": "missing_competitor_hotel"})

    # Formal callers provide the single business comparison result from
    # multi_hotel.  This module must not independently re-match room types.
    business_compare = price_comparisons if isinstance(price_comparisons, dict) else None
    if business_compare is not None:
        price_compare = list(business_compare.get("comparisons") or [])
    else:
        price_compare = list(price_comparisons or [])
    batch_meta = {key: (business_compare or {}).get(key) for key in ("comparison_batch_id", "source_run_id", "comparison_version", "generated_at", "data_cutoff_at")}
    variant_keys = [str(row.get("variant_key")) for row in (business_compare or {}).get("summary_by_room_type", []) if row.get("variant_key")]
    alerts.extend(_price_alerts(price_compare))

    own_room_count = sum(len(hotel["rooms"]) for hotel in own_hotels)
    competitor_room_count = sum(len(hotel["rooms"]) for hotel in competitor_hotels)
    status = _overall_status(alerts, missing_outputs, skipped_hotels)
    summary = {
        "own_hotel_count": len(own_hotels),
        "competitor_hotel_count": len(competitor_hotels),
        "unknown_hotel_count": len(unknown_hotels),
        "own_room_count": own_room_count,
        "competitor_room_count": competitor_room_count,
        "compare_count": len(price_compare),
        "need_manual_login": need_manual_login,
        "need_parser_update": need_parser_update,
        "message": _summary_message(status, need_manual_login, need_parser_update, missing_outputs),
    }
    payload = {
        "task": "hotel_price_monitor",
        "platform": "ctrip",
        "run_id": run_id,
        "captured_at": captured_at,
        "status": status,
        "own_hotels": own_hotels,
        "competitor_hotels": competitor_hotels,
        "unknown_hotels": unknown_hotels,
        "price_compare": price_compare,
        "business_price_compare": business_compare,
        "alerts": alerts,
        "summary": summary,
        "source_files": source_files,
        "debug_files": debug_files,
        **batch_meta,
    }

    rooms_all = _rooms_all(captured_at, own_hotels, competitor_hotels, unknown_hotels)
    run_summary = {
        "run_id": run_id,
        "captured_at": captured_at,
        "config": config_path,
        "output_dir": str(latest_dir),
        "status": status,
        "hotel_count": len(hotels),
        "own_hotel_count": len(own_hotels),
        "competitor_hotel_count": len(competitor_hotels),
        "unknown_hotel_count": len(unknown_hotels),
        "missing_own_hotel": not bool(own_hotels),
        "missing_competitor_hotel": not bool(competitor_hotels),
        "own_room_count": own_room_count,
        "competitor_room_count": competitor_room_count,
        "price_compare_count": len(price_compare),
        "need_manual_login": need_manual_login,
        "need_parser_update": need_parser_update,
        "missing_outputs": missing_outputs,
        "skipped_hotels": skipped_hotels,
        "output_files": [],
        "comparison_variant_keys": variant_keys,
        **batch_meta,
    }

    output_files = _write_outputs(latest_dir, payload, rooms_all, business_compare or _price_compare_payload(price_compare), run_summary)
    run_summary["output_files"] = output_files
    _write_json(latest_dir / "run_summary.json", run_summary)

    print("Hermes 主输入：")
    print(latest_dir / "hermes_payload.json")
    print("运行摘要：")
    print(latest_dir / "run_summary.json")
    print()
    print("价格对比：")
    print(latest_dir / "price_compare.json")
    print()
    print("房型明细：")
    print(latest_dir / "rooms_all.jsonl")
    if need_manual_login:
        print()
        print("需要重新执行人工登录：")
        print(".venv\\Scripts\\python.exe main.py login --channel chrome")
    if need_parser_update:
        print()
        print("抓到 JSON 但解析失败，需要优化 src/extract.py。")

    return run_summary


def _load_hotel_result(hotel: HotelConfig, captured_at: str) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, bool]]:
    alerts: list[dict[str, Any]] = []
    flags = {"need_manual_login": False, "need_parser_update": False, "missing_output": False, "skipped_missing_url": False}
    if not hotel.url:
        flags["skipped_missing_url"] = True
    result_path = Path(hotel.output) / "ctrip_rooms.json"
    metadata: dict[str, Any] = {}
    records: list[dict[str, Any]] = []
    status = "missing_output"
    error_reason = ""

    if result_path.exists():
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            metadata = payload.get("metadata", {}) if isinstance(payload, dict) else {}
            records = payload.get("records", []) if isinstance(payload, dict) else []
            status = str(metadata.get("page_status") or _records_status(records) or "unknown")
        except json.JSONDecodeError as exc:
            status = "page_error"
            error_reason = f"结果 JSON 解析失败: {exc}"
    else:
        flags["missing_output"] = True
        error_reason = "missing_output"

    raw_json_count = _int_or_zero(metadata.get("saved_json_count"))
    if raw_json_count > 0 and not records:
        flags["need_parser_update"] = True
        alerts.append(
            {
                "level": "warning",
                "message": "抓到 JSON 但解析失败，需要更新解析规则",
                "hotel_key": hotel.key,
                "hotel_name": hotel.name,
            }
        )
    if status in {"login_required", "session_expired"}:
        flags["need_manual_login"] = True
        alerts.append(
            {
                "level": "warning",
                "message": "酒店登录态失效，需要重新人工登录",
                "hotel_key": hotel.key,
                "hotel_name": hotel.name,
            }
        )
    if status == "captcha_required":
        alerts.append(
            {
                "level": "critical",
                "message": "酒店出现验证码或安全验证，程序已停止，不会绕过",
                "hotel_key": hotel.key,
                "hotel_name": hotel.name,
            }
        )

    rooms = [_normalize_room(record) for record in records]
    hotel_obj = {
        "key": hotel.key,
        "role": hotel.role,
        "name": hotel.name,
        "url": hotel.url,
        "output": hotel.output,
        "status": status,
        "error_reason": error_reason,
        "rooms": rooms,
        "available_rooms": available_room_records(rooms),
        "lowest_available_rooms": _lowest_available_rooms(rooms),
        "metadata": metadata,
    }
    return hotel_obj, alerts, flags


def _normalize_room(record: dict[str, Any]) -> dict[str, Any]:
    room_name = str(record.get("room_name", "") or "")
    price = _valid_price_or_none(record.get("price"))
    available = _available_text(record.get("available"))
    source_type = str(record.get("source_type", "") or "")
    stay_date = str(record.get("stay_date") or record.get("checkin_date") or record.get("date") or today_str())
    room_type = normalize_room_type(room_name)
    return {
        "stay_date": stay_date,
        "checkin_date": str(record.get("checkin_date") or stay_date),
        "date": str(record.get("date") or stay_date),
        "room_name": room_name,
        "room_type": str(record.get("room_type", "") or ""),
        "normalized_room_type": room_type,
        "normalized_room_name": normalize_room_name(room_name),
        "price": price,
        "currency": str(record.get("currency", "") or "CNY"),
        "breakfast": str(record.get("breakfast", "") or ""),
        "cancel_policy": str(record.get("cancel_policy", "") or ""),
        "available": available,
        "source_type": source_type,
        "source": str(record.get("source", "") or ""),
        "status": str(record.get("status", "") or ""),
        "source_url": str(record.get("source_url", "") or ""),
        "response_url": str(record.get("response_url", "") or ""),
        "confidence": _confidence(source_type, record),
        "note": str(record.get("note", "") or record.get("error_reason", "") or ""),
    }


def _lowest_available_rooms(rooms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for (_stay_date, _room_type), room in sorted(lowest_available_rooms_by_type(rooms).items()):
        rows.append(
            {
                "stay_date": room.get("stay_date", ""),
                "checkin_date": room.get("checkin_date", room.get("stay_date", "")),
                "date": room.get("date", room.get("stay_date", "")),
                "normalized_room_type": room.get("normalized_room_type", ""),
                "normalized_room_name": room.get("normalized_room_name", ""),
                "room_name": room.get("room_name", ""),
                "price": room.get("price"),
                "currency": room.get("currency", "CNY"),
                "available": room.get("available", "unknown"),
                "source_type": room.get("source_type", ""),
                "source": room.get("source", ""),
                "status": room.get("status", ""),
            }
        )
    return rows


def _write_outputs(
    latest_dir: Path,
    payload: dict[str, Any],
    rooms_all: list[dict[str, Any]],
    price_compare: dict[str, Any],
    run_summary: dict[str, Any],
) -> list[str]:
    files = [
        latest_dir / "hermes_payload.json",
        latest_dir / "rooms_all.jsonl",
        latest_dir / "price_compare.json",
        latest_dir / "run_summary.json",
    ]
    for stale_path in (latest_dir / "agent_brief.md", latest_dir / "ctrip_hotels.xlsx"):
        if stale_path.exists():
            stale_path.unlink()
    _write_json(files[0], payload)
    with files[1].open("w", encoding="utf-8", newline="\n") as handle:
        for row in rooms_all:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    _write_json(files[2], price_compare)
    _write_json(files[3], run_summary)
    return [str(path) for path in files]


def _price_compare_payload(price_compare: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "metadata": {
            "row_count": len(price_compare),
            "fields": [
                "stay_date",
                "checkin_date",
                "date",
                "normalized_room_type",
                "own_hotel_name",
                "own_room_name",
                "own_price",
                "competitor_hotel_name",
                "competitor_room_name",
                "competitor_price",
                "price_diff",
                "price_diff_pct",
            ],
            "rule": "hotel + stay_date + normalized_room_type -> lowest available valid OTA price",
        },
        "comparisons": price_compare,
        "grouped_by_date": _group_price_compare_by_date(price_compare),
    }


def _group_price_compare_by_date(price_compare: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in price_compare:
        key = str(item.get("stay_date") or item.get("checkin_date") or item.get("date") or "unknown")
        grouped.setdefault(key, []).append(item)
    return grouped


def _rooms_all(captured_at: str, own_hotels: list[dict], competitor_hotels: list[dict], unknown_hotels: list[dict]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for hotel in [*own_hotels, *competitor_hotels, *unknown_hotels]:
        for room in hotel.get("rooms", []):
            rows.append(
                {
                    "captured_at": captured_at,
                    "platform": "ctrip",
                    "hotel_key": hotel.get("key", ""),
                    "hotel_role": hotel.get("role", ""),
                    "hotel_name": hotel.get("name", ""),
                    "stay_date": room.get("stay_date", ""),
                    "checkin_date": room.get("checkin_date", ""),
                    "date": room.get("date", ""),
                    "room_name": room.get("room_name", ""),
                    "normalized_room_type": room.get("normalized_room_type", ""),
                    "normalized_room_name": room.get("normalized_room_name", ""),
                    "price": room.get("price"),
                    "currency": room.get("currency", "CNY"),
                    "breakfast": room.get("breakfast", ""),
                    "cancel_policy": room.get("cancel_policy", ""),
                    "available": room.get("available", "unknown"),
                    "source_type": room.get("source_type", ""),
                    "source": room.get("source", ""),
                    "status": room.get("status", ""),
                    "note": room.get("note") or room.get("error_reason", ""),
                }
            )
    return rows


def _price_alerts(price_compare: list[dict[str, Any]]) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    for item in price_compare:
        level = item.get("alert_level")
        if level == "warning":
            message = "本店某房型价格明显高于某竞品，关注转化率"
        elif level == "info":
            message = "本店某房型价格明显低于某竞品，可评估提价空间"
        else:
            continue
        alerts.append(
            {
                "level": level,
                "message": message,
                "own_hotel_name": item.get("own_hotel_name", ""),
                "competitor_hotel_name": item.get("competitor_hotel_name", ""),
                "stay_date": item.get("stay_date", item.get("date", "")),
                "normalized_room_type": item.get("normalized_room_type", ""),
                "room_name": item.get("own_room_name", ""),
                "price_diff": item.get("price_diff"),
                "price_diff_pct": item.get("price_diff_pct"),
            }
        )
    return alerts


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _records_status(records: list[dict[str, Any]]) -> str:
    for record in records:
        status = str(record.get("status", "") or "")
        if status:
            return status
    return ""


def _valid_price_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if 50 <= price <= 10000 else None


def _available_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("true"):
        return "true"
    if text.startswith("false"):
        return "false"
    if text in {"1", "yes", "available", "bookable"}:
        return "true"
    if any(token in text for token in ("可订", "在售", "仅剩", "预订", "有房")):
        return "true"
    if text in {"0", "no", "soldout", "sold_out"}:
        return "false"
    if any(token in text for token in ("售罄", "不可订", "满房", "无房", "已售完")):
        return "false"
    return "unknown"


def _confidence(source_type: str, record: dict[str, Any]) -> str:
    note = str(record.get("note", "") or "")
    if note == "ctrip_room_list":
        return "high"
    if source_type == "json":
        return "medium"
    return "low"


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _overall_status(alerts: list[dict[str, Any]], missing_outputs: list[dict], skipped_hotels: list[dict]) -> str:
    if any(alert.get("level") == "critical" for alert in alerts):
        return "critical"
    if alerts or missing_outputs or skipped_hotels:
        return "warning"
    return "success"


def _summary_message(status: str, need_manual_login: bool, need_parser_update: bool, missing_outputs: list[dict]) -> str:
    if need_manual_login:
        return "存在需要重新人工登录的酒店。"
    if need_parser_update:
        return "存在抓到 JSON 但解析失败的酒店。"
    if missing_outputs:
        return "部分酒店缺少 ctrip_rooms.json 输出。"
    if status == "success":
        return "Hermes 文件生成完成。"
    return "Hermes 文件生成完成，但存在提醒。"
