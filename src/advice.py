from __future__ import annotations

from collections import defaultdict
from typing import Any


MAX_PRICE_COMPARISONS = 5
COMPARABLE_MATCH_LEVELS = {"exact", "comparable"}


def summarize_competitor_price_for_business_snapshot(
    price_data: dict[str, Any] | None,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = price_data or {}
    comparisons_by_variant = {
        _comparison_key(item): item
        for item in data.get("comparisons", [])
        if isinstance(item, dict) and _comparison_key(item)
    }
    rows: list[dict[str, Any]] = []
    seen_variants: set[str] = set()
    handled_categories: set[str] = set()
    excluded_room_names: set[str] = set()
    displayed_room_names: set[str] = set()

    for decision in data.get("decisions", []) or []:
        if not isinstance(decision, dict):
            continue
        category = str(decision.get("room_category") or "").strip()
        variant_key = _comparison_key(decision)
        own_price = _number(decision.get("own_current_price"))
        match_level = _match_level(decision)
        if match_level not in COMPARABLE_MATCH_LEVELS:
            # benchmark is retained for internal market context, not a price conclusion.
            # Older persisted decisions do not carry match_level in raw JSONB.
            # Keep their category eligible for the current comparison artifact below.
            if match_level is not None:
                seen_variants.add(variant_key)
                handled_categories.add(category)
                _remember_room_name(excluded_room_names, comparisons_by_variant.get(variant_key, {}))
            continue
        if not category or own_price is None:
            continue
        raw = decision.get("raw") if isinstance(decision.get("raw"), dict) else {}
        compare = comparisons_by_variant.get(variant_key, {})
        if _can_drive_pricing(decision, compare) is False:
            seen_variants.add(variant_key)
            handled_categories.add(category)
            _remember_room_name(excluded_room_names, compare)
            continue
        competitor = raw.get("competitor_min_source") if isinstance(raw.get("competitor_min_source"), dict) else {}
        own_sources = (raw.get("own_price_source") or {}).get("rows", []) if isinstance(raw.get("own_price_source"), dict) else []
        own_source = _minimum_valid_row(own_sources)
        result = _comparison(
            category=category,
            own_room_name=(own_source or {}).get("room_name") or compare.get("own_room_name") or category,
            own_price=own_price,
            competitor_hotel_name=competitor.get("hotel_name") or compare.get("competitor_hotel_name"),
            competitor_room_name=competitor.get("room_name") or compare.get("competitor_room_name"),
            competitor_price=_number(decision.get("competitor_min_price")),
            snapshot=snapshot,
        )
        rows.append(result)
        _remember_room_name(displayed_room_names, result)
        seen_variants.add(variant_key)
        handled_categories.add(category)

    remaining = [
        item for variant_key, item in comparisons_by_variant.items()
        if variant_key not in seen_variants
    ]
    remaining.sort(key=lambda item: abs(_number(item.get("price_diff")) or 0), reverse=True)
    for item in remaining:
        category = str(item.get("room_category") or "").strip()
        variant_key = _comparison_key(item)
        if _match_level(item) not in (None, *COMPARABLE_MATCH_LEVELS) or _can_drive_pricing(item) is False:
            seen_variants.add(variant_key)
            handled_categories.add(category)
            _remember_room_name(excluded_room_names, item)
            continue
        own_price = _number(item.get("own_price"))
        if not category or own_price is None:
            continue
        result = _comparison(
            category=category,
            own_room_name=item.get("own_room_name") or category,
            own_price=own_price,
            competitor_hotel_name=item.get("competitor_hotel_name"),
            competitor_room_name=item.get("competitor_room_name"),
            competitor_price=_number(item.get("competitor_min_price")),
            snapshot=snapshot,
        )
        rows.append(result)
        _remember_room_name(displayed_room_names, result)
        seen_variants.add(variant_key)
        handled_categories.add(category)

    for category, own in _latest_own_prices(data.get("own_prices", [])).items():
        if category in handled_categories:
            continue
        room_name = str(own.get("room_name") or "").strip()
        if room_name and room_name in excluded_room_names | displayed_room_names:
            continue
        result = _comparison(
            category=category,
            own_room_name=own.get("room_name") or category,
            own_price=own["price"],
            competitor_hotel_name=None,
            competitor_room_name=None,
            competitor_price=None,
            snapshot=snapshot,
        )
        rows.append(result)
        _remember_room_name(displayed_room_names, result)
        handled_categories.add(category)

    selected = rows[:MAX_PRICE_COMPARISONS]
    references = _reference_comparisons(data.get("summary_by_room_type", []))
    return {"has_price_data": bool(selected), "comparisons": selected, "reference_comparisons": references[:MAX_PRICE_COMPARISONS]}


def _reference_comparisons(items: Any) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or _match_level(item) not in COMPARABLE_MATCH_LEVELS or _can_drive_pricing(item) is True:
            continue
        selected = item.get("selected_match") if isinstance(item.get("selected_match"), dict) else None
        own_price = _number(item.get("own_min_price"))
        competitor_price = _number((selected or {}).get("price"))
        key = _comparison_key(item)
        if not key or key in seen or own_price is None or competitor_price is None:
            continue
        seen.add(key)
        references.append({
            "variant_key": key,
            "own_room_name": item.get("own_room_name") or item.get("normalized_room_type") or "未识别房型",
            "own_price": own_price,
            "competitor_hotel_name": selected.get("hotel_name") or selected.get("hotel_key") or "竞品",
            "competitor_room_name": selected.get("room_name") or "未识别房型",
            "competitor_price": competitor_price,
            "price_diff": round(own_price - competitor_price, 2),
            "room_difference_note": item.get("room_difference_note") or "产品属性不同",
            "comparison_role": item.get("comparison_role") or "reference",
        })
    return references


def generate_business_snapshot_advice(
    snapshot: dict[str, Any],
    price_summary: dict[str, Any] | None = None,
) -> list[str]:
    available = _number(snapshot.get("total_available"))
    advice: list[str] = []

    tight_rooms = [
        item for item in snapshot.get("room_type_inventory", [])
        if isinstance(item, dict) and _number(item.get("available")) is not None
        and 0 < _number(item.get("available")) <= 2
    ]
    if tight_rooms:
        item = tight_rooms[0]
        name = str(item.get("roomType") or item.get("room_type") or "未命名房型")
        advice.append(f"{name}仅余{_number(item.get('available')):g}间。")

    sold_out = [
        item for item in snapshot.get("room_type_inventory", [])
        if isinstance(item, dict) and _number(item.get("available")) == 0
    ]
    if sold_out:
        name = str(sold_out[0].get("roomType") or sold_out[0].get("room_type") or "未命名房型")
        advice.append(f"{name}已无可售房。")

    summary = price_summary or {}
    comparisons = summary.get("comparisons", []) if summary.get("has_price_data") else []
    if comparisons:
        item = comparisons[0]
        name = str(item.get("own_room_name") or "").strip()
        own = _number(item.get("own_price"))
        rival = _number(item.get("competitor_price"))
        if name and own is not None and rival is not None and own != rival:
            advice.insert(0, f"{name}我方挂牌¥{own:g}、直接可比竞品¥{rival:g}；价差本身不足以确定调价。")

    if not advice and available is not None and available <= 5:
        advice.append(f"全店今日可售{available:g}间。")

    unique = list(dict.fromkeys(advice))
    return (unique or ["当前没有足够的可核实数据。"])[:2]


def _comparison(
    *,
    category: str,
    own_room_name: Any,
    own_price: float,
    competitor_hotel_name: Any,
    competitor_room_name: Any,
    competitor_price: float | None,
    snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    if competitor_price is None:
        return {
            "normalized_room_type": category,
            "own_room_name": str(own_room_name or category),
            "own_price": own_price,
            "competitor_hotel_name": None,
            "competitor_room_name": None,
            "competitor_price": None,
            "price_diff": None,
            "judgement": "无同类竞品有效价格",
            "advice": "不做竞品价差结论，仅结合库存判断。",
        }

    diff = round(own_price - competitor_price, 2)
    if diff < -10:
        judgement = "我方价格偏低"
        advice = "库存偏紧时可谨慎上调。" if _inventory_tight(snapshot) else "库存充足时先观察订单增长。"
    elif diff > 10:
        judgement = "我方价格偏高"
        advice = "优先观察订单转化，不盲目涨价。"
    else:
        judgement = "我方价格接近竞品"
        advice = "价差不大，优先观察订单变化。"
    return {
        "normalized_room_type": category,
        "own_room_name": str(own_room_name or category),
        "own_price": own_price,
        "competitor_hotel_name": competitor_hotel_name,
        "competitor_room_name": competitor_room_name,
        "competitor_price": competitor_price,
        "price_diff": diff,
        "judgement": judgement,
        "advice": advice,
    }


def _latest_own_prices(rows: Any) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in rows if isinstance(rows, list) else []:
        if not isinstance(item, dict):
            continue
        category = str(item.get("room_category") or item.get("normalized_room_type") or "").strip()
        price = _number(item.get("price"))
        if category and price is not None and price > 0:
            grouped[category].append({**item, "price": price})

    selected: dict[str, dict[str, Any]] = {}
    for category, candidates in grouped.items():
        latest_at = max(str(item.get("collected_at") or "") for item in candidates)
        latest = [item for item in candidates if str(item.get("collected_at") or "") == latest_at]
        selected[category] = min(latest, key=lambda item: item["price"])
    return selected


def _minimum_valid_row(rows: Any) -> dict[str, Any] | None:
    valid = [item for item in rows if isinstance(item, dict) and _number(item.get("price")) is not None] if isinstance(rows, list) else []
    return min(valid, key=lambda item: _number(item.get("price")) or 0) if valid else None


def _comparison_key(item: dict[str, Any]) -> str:
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
    return str(item.get("variant_key") or raw.get("variant_key") or item.get("room_category") or "").strip()


def _remember_room_name(target: set[str], item: dict[str, Any]) -> None:
    name = str(item.get("own_room_name") or "").strip()
    if name:
        target.add(name)


def _can_drive_pricing(*items: dict[str, Any]) -> bool | None:
    for item in items:
        raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
        value = item.get("can_drive_pricing") if "can_drive_pricing" in item else raw.get("can_drive_pricing")
        if value is not None:
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes"}
            return bool(value)
    return None


def _match_level(item: dict[str, Any]) -> str | None:
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
    value = item.get("match_level") or raw.get("match_level")
    value = str(value).strip().lower() if value is not None else ""
    return value or None


def _inventory_tight(snapshot: dict[str, Any] | None) -> bool:
    available = _number((snapshot or {}).get("total_available"))
    return available is not None and available <= 10


def _ratio(value: Any) -> float | None:
    number = _number(value)
    return number / 100 if number is not None and number > 1 else number


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
