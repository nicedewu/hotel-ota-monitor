from __future__ import annotations

import re
import argparse
import json
import html as html_lib
from pathlib import Path
from typing import Any, Iterable

from .utils import now_iso, read_json


ROOM_KEYS = {
    "roomName",
    "room_name",
    "roomTypeName",
    "roomType",
    "baseRoomName",
    "base_room_name",
    "name",
    "title",
    "roomTitle",
    "displayName",
    "productName",
    "ratePlanName",
    "rateName",
}
PRICE_KEYS = {
    "price",
    "salePrice",
    "amount",
    "basePrice",
    "displayPrice",
    "avgPrice",
    "roomPrice",
    "totalPrice",
    "priceInfo",
    "priceValue",
    "value",
    "minPrice",
}
CURRENCY_KEYS = {"currency", "currencyCode", "currency_code", "priceCurrency"}
BREAKFAST_KEYS = {"breakfast", "meal", "mealType", "meal_type", "breakfastDesc", "breakfast_desc", "mealDesc"}
CANCEL_KEYS = {"cancel", "cancellation", "cancelPolicy", "cancel_policy", "cancelDesc", "cancel_desc", "penalty", "refund"}
AVAILABLE_KEYS = {"available", "availability", "remain", "remainRoom", "stock", "inventory", "roomStatus", "status", "canBook", "bookable"}
SOLD_OUT_KEYS = {"soldOut", "isSoldOut", "isFullRoom", "sold_out"}

PRICE_RE = re.compile(r"(?:￥|¥|RMB\s*|CNY\s*)\s*([0-9]{2,5}(?:\.[0-9]{1,2})?)|([0-9]{2,5}(?:\.[0-9]{1,2})?)\s*元", re.I)
ROOM_NAME_RE = re.compile(r".*(房|间)【.+】$|.*(大床房|双床房|单人间|特大床房|四人间|三人间)$")
NOISE_ROOM_NAMES = {
    "房费",
    "优惠",
    "十亿豪补",
    "学生专享",
    "品牌首单",
    "节日专享",
    "门店首单",
    "选择房间",
    "今日价格",
    "可住人数",
    "以下房间可满足您的部分要求：",
}
NOISE_NAME_PARTS = ("优惠", "房费", "豪补", "专享", "首单", "仅剩", "点评", "距酒店", "有人预订", "选择房间")
SKIP_GENERIC_URL_PARTS = ("ctGetNearbyHotelList", "getHotelCommentInfo", "ctgethotelalbum")


def _valid_price(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        match = re.search(r"[0-9]{2,5}(?:\.[0-9]{1,2})?", value.replace(",", ""))
        if not match:
            return None
        number = float(match.group(0))
    else:
        return None
    if 50 <= number <= 10000:
        return int(number) if number.is_integer() else number
    return None


def _valid_room_name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = " ".join(value.split())
    if len(cleaned) < 2:
        return ""
    if cleaned.isdigit():
        return ""
    return cleaned


def _valid_strict_room_name(value: Any) -> str:
    cleaned = _valid_room_name(value)
    if not cleaned:
        return ""
    if cleaned in NOISE_ROOM_NAMES:
        return ""
    if any(part in cleaned for part in NOISE_NAME_PARTS):
        return ""
    if len(cleaned) > 80:
        return ""
    if any(part in cleaned for part in ("酒店会", "房间卫生", "显示所有")):
        return ""
    if not ROOM_NAME_RE.search(cleaned):
        return ""
    return cleaned


def _valid_ctrip_physical_room_name(value: Any) -> str:
    """Accept trusted Ctrip physical-room names, including new bracket formats."""
    cleaned = _valid_room_name(value)
    if not cleaned or len(cleaned) > 120:
        return ""
    if cleaned in NOISE_ROOM_NAMES or any(part in cleaned for part in NOISE_NAME_PARTS):
        return ""
    return cleaned


def _find_first_by_keys(data: Any, keys: set[str]) -> Any:
    if isinstance(data, dict):
        for key, value in data.items():
            if key in keys:
                return value
        for value in data.values():
            found = _find_first_by_keys(value, keys)
            if found not in (None, ""):
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find_first_by_keys(item, keys)
            if found not in (None, ""):
                return found
    return None


def _find_first_room_name(data: Any) -> str:
    if isinstance(data, dict):
        for key, value in data.items():
            if key in ROOM_KEYS:
                room_name = _valid_strict_room_name(value)
                if room_name:
                    return room_name
        for value in data.values():
            room_name = _find_first_room_name(value)
            if room_name:
                return room_name
    elif isinstance(data, list):
        for value in data:
            room_name = _find_first_room_name(value)
            if room_name:
                return room_name
    return ""


def _find_first_price(data: Any) -> float | None:
    direct = _valid_price(data)
    if direct is not None:
        return direct
    if isinstance(data, dict):
        for value in data.values():
            found = _find_first_price(value)
            if found is not None:
                return found
    elif isinstance(data, list):
        for value in data:
            found = _find_first_price(value)
            if found is not None:
                return found
    return None


def _find_first_price_by_keys(data: Any) -> float | None:
    if isinstance(data, dict):
        for key, value in data.items():
            if key in PRICE_KEYS:
                found = _find_first_price(value)
                if found is not None:
                    return found
        for value in data.values():
            found = _find_first_price_by_keys(value)
            if found is not None:
                return found
    elif isinstance(data, list):
        for value in data:
            found = _find_first_price_by_keys(value)
            if found is not None:
                return found
    return None


def _walk(data: Any) -> Iterable[dict]:
    if isinstance(data, dict):
        yield data
        for value in data.values():
            yield from _walk(value)
    elif isinstance(data, list):
        for item in data:
            yield from _walk(item)


def _has_hidden_price(data: Any) -> bool:
    if isinstance(data, dict):
        for key, value in data.items():
            if key == "isHidePrice" and str(value).strip().lower() in {"true", "1"}:
                return True
            if _has_hidden_price(value):
                return True
    elif isinstance(data, list):
        return any(_has_hidden_price(item) for item in data)
    return isinstance(data, str) and "登录看低价" in data


def has_hidden_price_signal(json_paths: Iterable[Path]) -> bool:
    for json_path in json_paths:
        try:
            payload = read_json(json_path)
        except Exception:
            continue
        if isinstance(payload, dict) and "_capture_meta" in payload:
            payload = payload.get("data")
        if _has_hidden_price(payload):
            return True
    return False


def _iter_ctrip_room_maps(payload: Any) -> Iterable[tuple[dict, dict]]:
    for item in _walk(payload):
        physic_room_map = item.get("physicRoomMap")
        sale_room_map = item.get("saleRoomMap")
        if isinstance(physic_room_map, dict) and isinstance(sale_room_map, dict):
            yield physic_room_map, sale_room_map


def _extract_ctrip_room_list(payload: Any, hotel_name: str, source_url: str, response_url: str, json_path: Path, captured_at: str) -> list[dict]:
    if not isinstance(payload, dict) or _has_hidden_price(payload):
        return []

    records: list[dict] = []
    seen: set[tuple] = set()
    for physic_room_map, sale_room_map in _iter_ctrip_room_maps(payload):
        for sale_room in sale_room_map.values():
            if not isinstance(sale_room, dict):
                continue
            room_id = sale_room.get("physicalRoomId")
            room_info = physic_room_map.get(str(room_id))
            if not isinstance(room_info, dict):
                continue

            room_name = _valid_ctrip_physical_room_name(room_info.get("name"))
            if not room_name:
                continue

            price_info = sale_room.get("priceInfo")
            price_value = None
            currency = "CNY"
            if isinstance(price_info, dict):
                price_value = _valid_price(price_info.get("price")) or _valid_price(price_info.get("displayPrice"))
                currency = str(price_info.get("currency") or currency)
            if price_value is None:
                price_value = _find_first_price_by_keys(sale_room)
            if price_value is None:
                continue

            booking_info = sale_room.get("bookingStatusInfo") if isinstance(sale_room.get("bookingStatusInfo"), dict) else {}
            meal_info = sale_room.get("mealInfo") if isinstance(sale_room.get("mealInfo"), dict) else {}
            cancel_policy = _find_first_by_keys(sale_room, CANCEL_KEYS) or ""
            available = booking_info.get("isBooking")
            remain = booking_info.get("remainRoomQuantity")
            available_text = _available_text(available, remain, sale_room)

            key = (room_name, price_value)
            if key in seen:
                continue
            seen.add(key)
            records.append(
                _record(
                    hotel_name=hotel_name,
                    source_url=source_url,
                    response_url=response_url,
                    json_path=json_path,
                    captured_at=captured_at,
                    room_name=room_name,
                    price_value=price_value,
                    currency=currency,
                    breakfast=str(meal_info.get("title") or meal_info.get("allIncludeMealDesc") or ""),
                    cancel_policy=str(cancel_policy),
                    available=available_text,
                    note="ctrip_room_list",
                    source="getHotelRoomListInland",
                )
            )
    return records


def _extract_recursive_room_candidates(payload: Any, hotel_name: str, source_url: str, response_url: str, json_path: Path, captured_at: str) -> list[dict]:
    records: list[dict] = []
    seen: set[tuple] = set()
    for item in _walk(payload):
        room_name = _find_first_room_name(item)
        if not room_name:
            continue
        price_value = _find_first_price_by_keys(item)
        if price_value is None:
            continue

        currency = _find_first_by_keys(item, CURRENCY_KEYS) or "CNY"
        breakfast = _find_first_by_keys(item, BREAKFAST_KEYS) or ""
        cancel_policy = _find_first_by_keys(item, CANCEL_KEYS) or ""
        available = _available_text(_find_first_by_keys(item, AVAILABLE_KEYS), _find_first_by_keys(item, {"remain", "remainRoom", "stock", "inventory"}), item)
        key = (room_name, price_value)
        if key in seen:
            continue
        seen.add(key)
        records.append(
            _record(
                hotel_name=hotel_name,
                source_url=source_url,
                response_url=response_url,
                json_path=json_path,
                captured_at=captured_at,
                room_name=room_name,
                price_value=price_value,
                currency=str(currency),
                breakfast=str(breakfast),
                cancel_policy=str(cancel_policy),
                available=available,
                note="recursive_json_candidate",
                source="network_json",
            )
        )
    return records


def _record(
    hotel_name: str,
    source_url: str,
    response_url: str,
    json_path: Path,
    captured_at: str,
    room_name: str,
    price_value: float,
    currency: str,
    breakfast: str,
    cancel_policy: str,
    available: str,
    note: str,
    source: str,
) -> dict:
    return {
        "platform": "ctrip",
        "hotel_name": hotel_name,
        "source_type": "json",
        "source": source,
        "status": "success",
        "error_reason": "",
        "room_name": room_name,
        "room_type": "",
        "price": price_value,
        "currency": currency,
        "breakfast": breakfast,
        "cancel_policy": cancel_policy,
        "available": available,
        "source_url": source_url,
        "response_url": response_url,
        "json_path": str(json_path),
        "captured_at": captured_at,
        "note": note,
    }


def _available_text(available: Any, remain: Any, item: Any) -> str:
    sold_out = _find_first_by_keys(item, SOLD_OUT_KEYS)
    if isinstance(sold_out, bool):
        base = "False" if sold_out else "True"
    elif isinstance(available, bool):
        base = str(available)
    elif available not in (None, ""):
        base = str(available)
    else:
        base = ""
    if remain not in (None, ""):
        return f"{base}; remain={remain}" if base else f"remain={remain}"
    return base


def extract_json_records(json_paths: Iterable[Path], hotel_name: str, source_url: str) -> list[dict]:
    records: list[dict] = []
    seen: set[tuple] = set()

    for json_path in json_paths:
        try:
            payload = read_json(json_path)
        except Exception:
            continue

        response_url = ""
        captured_at = now_iso()
        if isinstance(payload, dict) and "_capture_meta" in payload:
            meta = payload.get("_capture_meta") or {}
            response_url = str(meta.get("response_url", ""))
            captured_at = str(meta.get("captured_at", captured_at))
            payload = payload.get("data")

        if _has_hidden_price(payload):
            continue

        ctrip_records = _extract_ctrip_room_list(payload, hotel_name, source_url, response_url, json_path, captured_at)
        for record in ctrip_records:
            key = (record["room_name"], record["price"])
            if key not in seen:
                seen.add(key)
                records.append(record)
        if ctrip_records or any(part in response_url for part in SKIP_GENERIC_URL_PARTS):
            continue

        recursive_records = _extract_recursive_room_candidates(payload, hotel_name, source_url, response_url, json_path, captured_at)
        for record in recursive_records:
            key = (record["room_name"], record["price"])
            if key not in seen:
                seen.add(key)
                records.append(record)
        if recursive_records:
            continue

        for item in _walk(payload):
            room_name = ""
            for key in ROOM_KEYS:
                if key in item:
                    room_name = _valid_strict_room_name(item.get(key))
                    if room_name:
                        break
            if not room_name:
                continue

            price_value = None
            for key in PRICE_KEYS:
                if key in item:
                    price_value = _find_first_price(item.get(key))
                    if price_value is not None:
                        break
            if price_value is None:
                continue

            currency = _find_first_by_keys(item, CURRENCY_KEYS) or "CNY"
            breakfast = _find_first_by_keys(item, BREAKFAST_KEYS) or ""
            cancel_policy = _find_first_by_keys(item, CANCEL_KEYS) or ""
            available = _find_first_by_keys(item, AVAILABLE_KEYS)
            key = (room_name, price_value)
            if key in seen:
                continue
            seen.add(key)
            records.append(
                {
                    "platform": "ctrip",
                    "hotel_name": hotel_name,
                    "source_type": "json",
                    "source": "network_json",
                    "status": "success",
                    "error_reason": "",
                    "room_name": room_name,
                    "room_type": "",
                    "price": price_value,
                    "currency": str(currency),
                    "breakfast": str(breakfast),
                    "cancel_policy": str(cancel_policy),
                    "available": "" if available is None else str(available),
                    "source_url": source_url,
                    "response_url": response_url,
                    "json_path": str(json_path),
                    "captured_at": captured_at,
                    "note": "",
                }
            )

    return records


def extract_dom_records(page_text_path: Path, hotel_name: str, source_url: str, page_html_path: Path | None = None) -> list[dict]:
    records: list[dict] = []
    seen: set[tuple] = set()
    for text, source in _dom_text_candidates(page_text_path, page_html_path):
        _append_dom_records(text, hotel_name, source_url, source, records, seen)
    return records


def _append_dom_records(text: str, hotel_name: str, source_url: str, source: str, records: list[dict], seen: set[tuple]) -> None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    room_section = _room_section_lines(lines)
    index = 0
    while index < len(room_section):
        room_name = _valid_strict_room_name(room_section[index])
        if not room_name:
            index += 1
            continue

        next_index = index + 1
        while next_index < len(room_section) and not _valid_strict_room_name(room_section[next_index]):
            next_index += 1
        block = room_section[index:next_index]
        prices = _prices_in_lines(block)
        if prices:
            price = min(prices)
            breakfast = _first_line_containing(block, ("早餐",))
            cancel_policy = _first_line_containing(block, ("取消",))
            available = _first_line_containing(block, ("仅剩", "可订", "预订"))
            key = (room_name, price)
            if key not in seen:
                seen.add(key)
                records.append(
                    {
                        "platform": "ctrip",
                        "hotel_name": hotel_name,
                        "source_type": "dom_text",
                        "source": source,
                        "status": "success",
                        "error_reason": "",
                        "room_name": room_name,
                        "room_type": "",
                        "price": price,
                        "currency": "CNY",
                        "breakfast": breakfast,
                        "cancel_policy": cancel_policy,
                        "available": available,
                        "source_url": source_url,
                        "response_url": "",
                        "json_path": "",
                        "captured_at": now_iso(),
                        "note": "dom_text_room_section",
                    }
                )
        index = next_index


def _dom_text_candidates(page_text_path: Path, page_html_path: Path | None) -> Iterable[tuple[str, str]]:
    if page_text_path.exists():
        text = page_text_path.read_text(encoding="utf-8", errors="ignore")
        if text.strip():
            yield text, "page_text"
    if page_html_path and page_html_path.exists():
        html_text = _html_to_text(page_html_path.read_text(encoding="utf-8", errors="ignore"))
        if html_text.strip():
            yield html_text, "page_html"


def _html_to_text(raw_html: str) -> str:
    without_script = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "\n", raw_html, flags=re.I | re.S)
    spaced = re.sub(r"</?(br|p|div|li|tr|td|th|h[1-6])\b[^>]*>", "\n", without_script, flags=re.I)
    text = re.sub(r"<[^>]+>", "\n", spaced)
    return html_lib.unescape(text)


def _room_section_lines(lines: list[str]) -> list[str]:
    first_room_index = next((idx for idx, line in enumerate(lines) if _valid_strict_room_name(line) and "【" in line), 0)
    end_index = len(lines)
    for idx in range(first_room_index + 1, len(lines)):
        if lines[idx] == "点评":
            end_index = idx
            break
    return lines[first_room_index:end_index]


def _prices_in_lines(lines: list[str]) -> list[float]:
    prices: list[float] = []
    for line in lines:
        for match in PRICE_RE.finditer(line):
            number = match.group(1) or match.group(2)
            price = _valid_price(number)
            if price is not None:
                prices.append(price)
    return prices


def _first_line_containing(lines: list[str], tokens: tuple[str, ...]) -> str:
    for line in lines:
        if any(token in line for token in tokens):
            return line
    return ""


def _cli() -> None:
    parser = argparse.ArgumentParser(description="离线测试携程房型 JSON 解析")
    parser.add_argument("json_path")
    parser.add_argument("--hotel", default="offline_hotel")
    parser.add_argument("--url", default="")
    args = parser.parse_args()
    path = Path(args.json_path)
    records = extract_json_records([path], args.hotel, args.url)
    print(f"解析文件: {path}")
    print(f"房型数量: {len(records)}")
    if not records:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            body = payload.get("data", payload) if isinstance(payload, dict) else payload
            code = _find_first_by_keys(body, {"htlSpiderActionErrorCode", "errorCode", "code"})
            if code not in (None, ""):
                print(f"未解析到房型；检测到接口错误码: {code}")
        except Exception as exc:
            print(f"读取 JSON 失败: {exc}")
    for record in records[:5]:
        print(json.dumps({key: record.get(key) for key in ("room_name", "price", "available", "breakfast", "cancel_policy", "source", "note")}, ensure_ascii=False))


if __name__ == "__main__":
    _cli()
