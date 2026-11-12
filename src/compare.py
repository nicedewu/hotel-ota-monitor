from __future__ import annotations

import re
from typing import Any


def normalize_room_type(room_name: str) -> str:
    text = str(room_name or "").lower()
    text = re.sub(r"【[^】]*】", "", text)
    text = re.sub(r"[\s\(\)（）\[\]【】,，。:：;；/\\|+_\-—·、]", "", text)

    if "浴缸" in text:
        return "bathtub_double"
    esports = "电竞" in text or "游戏" in text or "单机" in text
    if esports:
        for room_type, keywords in (
            ("esports_single", ("单机", "单人房", "电竞单人", "单人间", "一人", "1人")),
            ("esports_triple", ("三人", "3人")),
            ("esports_quad", ("四人", "4人")),
            ("esports_penta", ("五人", "5人")),
            ("esports_hexa", ("六人", "6人")),
            ("esports_twin", ("双床", "标间", "标准间", "双人双床")),
            ("esports_double", ("大床", "特大床", "双人间", "双人房", "情侣房", "二人", "2人", "双人双机", "双机房")),
        ):
            if any(keyword in text for keyword in keywords):
                return room_type
        return "esports_other"
    if any(keyword in text for keyword in ("大床", "特大床")):
        return "big_bed"
    if any(keyword in text for keyword in ("双床", "双人床", "标间", "标准间")):
        return "twin"
    return "other"


def normalize_room_name(room_name: str) -> str:
    return normalize_room_type(room_name)


def build_price_compare(own_hotels: list[dict[str, Any]], competitor_hotels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    comparisons: list[dict[str, Any]] = []
    for own_hotel in own_hotels:
        own_rooms = lowest_available_rooms_by_type(own_hotel.get("available_rooms", own_hotel.get("rooms", [])))
        for competitor_hotel in competitor_hotels:
            competitor_rooms = lowest_available_rooms_by_type(competitor_hotel.get("available_rooms", competitor_hotel.get("rooms", [])))
            for compare_key, own_room in own_rooms.items():
                competitor_room = competitor_rooms.get(compare_key)
                if not competitor_room:
                    continue
                own_price = float(own_room["price"])
                competitor_price = float(competitor_room["price"])
                price_diff = round(own_price - competitor_price, 2)
                price_diff_pct = round(price_diff / competitor_price, 4) if competitor_price else None
                suggestion, alert_level = _suggestion(own_price, competitor_price)
                comparisons.append(
                    {
                        "own_hotel_key": own_hotel.get("key", ""),
                        "own_hotel_name": own_hotel.get("name", ""),
                        "competitor_hotel_key": competitor_hotel.get("key", ""),
                        "competitor_hotel_name": competitor_hotel.get("name", ""),
                        "stay_date": own_room.get("stay_date", ""),
                        "checkin_date": own_room.get("checkin_date", own_room.get("stay_date", "")),
                        "date": own_room.get("date", own_room.get("stay_date", "")),
                        "normalized_room_type": own_room.get("normalized_room_type", ""),
                        "normalized_room_name": own_room.get("normalized_room_type", ""),
                        "own_room_name": own_room.get("room_name", ""),
                        "own_price": own_price,
                        "own_available": own_room.get("available", "unknown"),
                        "competitor_room_name": competitor_room.get("room_name", ""),
                        "competitor_price": competitor_price,
                        "competitor_available": competitor_room.get("available", "unknown"),
                        "price_diff": price_diff,
                        "price_diff_pct": price_diff_pct,
                        "suggestion": suggestion,
                        "alert_level": alert_level,
                        "note": "lowest_available_by_date_room_type",
                    }
                )
    return comparisons


def lowest_available_rooms_by_type(rooms: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for room in available_room_records(rooms):
        price = float(room["price"])
        stay_date = _room_date(room)
        room_type = room.get("normalized_room_type") or normalize_room_type(str(room.get("room_name", "")))
        candidate = {**room, "price": price, "stay_date": stay_date, "normalized_room_type": room_type, "normalized_room_name": room_type}
        compare_key = (stay_date, room_type)
        current = best.get(compare_key)
        if current is None or price < float(current["price"]):
            best[compare_key] = candidate
    return best


def available_room_records(rooms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    available_rooms: list[dict[str, Any]] = []
    for room in rooms:
        if room.get("status") != "success":
            continue
        if _available_bool(room.get("available")) is not True:
            continue
        price = _valid_price(room.get("price"))
        if price is None:
            continue
        room_type = room.get("normalized_room_type") or normalize_room_type(str(room.get("room_name", "")))
        available_rooms.append(
            {
                **room,
                "price": price,
                "stay_date": _room_date(room),
                "normalized_room_type": room_type,
                "normalized_room_name": room_type,
            }
        )
    return available_rooms


def pick_lowest_available_room(rooms: list[dict[str, Any]], room_type: str, date: str) -> dict[str, Any] | None:
    return lowest_available_rooms_by_type(rooms).get((date, room_type))


def _valid_price(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if 50 <= price <= 10000:
        return price
    return None


def _available_bool(value: Any) -> bool | None:
    text = str(value or "").strip().lower()
    if text.startswith("true"):
        return True
    if text.startswith("false"):
        return False
    if text in {"1", "yes", "available", "bookable"}:
        return True
    if any(token in text for token in ("可订", "在售", "仅剩", "预订", "有房")):
        return True
    if text in {"0", "no", "soldout", "sold_out"}:
        return False
    if any(token in text for token in ("售罄", "不可订", "满房", "无房", "已售完")):
        return False
    return None


def _room_date(room: dict[str, Any]) -> str:
    return str(room.get("stay_date") or room.get("checkin_date") or room.get("date") or "")


def _suggestion(own_price: float, competitor_price: float) -> tuple[str, str]:
    if own_price > competitor_price * 1.1:
        return "本店明显高于竞品，关注转化率", "warning"
    if own_price < competitor_price * 0.9:
        return "本店明显低于竞品，可评估提价空间", "info"
    return "价格接近，维持观察", "none"
