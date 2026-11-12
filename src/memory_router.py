from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from src.memory_provider import (
    contains_sensitive_data,
    filter_and_sort_pricing_rules,
    get_memory_setting,
    recall_for_pricing_decision as recall_mnemosyne_for_pricing,
)


CLEAN_STAGING_FILE = Path("output/memory_staging/mnemosyne_import_cleaned.jsonl")


def memory_router_status() -> dict[str, Any]:
    return {
        "memory_primary_backend": get_memory_setting("MEMORY_PRIMARY_BACKEND", "staging").lower(),
        "memory_auxiliary_backend": get_memory_setting("MEMORY_AUXILIARY_BACKEND", "none").lower(),
        "fallback_backend": "staging",
    }


def recall_for_pricing_decision(
    hotel_key: str,
    stay_date: str | None = None,
    room_type: str | None = None,
    query: str | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    status = memory_router_status()
    if status["memory_primary_backend"] == "mnemosyne":
        recalled = recall_mnemosyne_for_pricing(hotel_key, stay_date, room_type, query, limit)
        if recalled:
            return recalled
    return recall_clean_staging(hotel_key, query or "调价 入住率 可售 竞品 展会", limit, stay_date, room_type)


def recall_clean_staging(hotel_key: str, query: str, limit: int = 8, stay_date: str | None = None, room_type: str | None = None) -> list[dict[str, Any]]:
    if limit <= 0 or not CLEAN_STAGING_FILE.is_file():
        return []
    terms = {term.lower() for term in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{2,}", query)}
    ranked: list[tuple[int, dict[str, Any]]] = []
    for line in CLEAN_STAGING_FILE.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict) or item.get("scope") not in ("global", "hotel"):
            continue
        content = str(item.get("content") or "").strip()
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        if not content or contains_sensitive_data(content, metadata):
            continue
        lowered = content.lower()
        score = sum(term in lowered for term in terms)
        if score > 0:
            ranked.append((score, item))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return filter_and_sort_pricing_rules([item for _, item in ranked], hotel_key, stay_date, room_type, limit)
