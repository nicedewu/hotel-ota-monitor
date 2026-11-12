#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.memory_provider import memory_backend_status, memory_health, recall_global, recall_hotel
from src.memory_router import CLEAN_STAGING_FILE, memory_router_status, recall_for_pricing_decision


def _matches(records: list[dict[str, Any]], predicate: Callable[[str], bool]) -> bool:
    return any(predicate(str(record.get("content") or "").lower()) for record in records)


def verify() -> bool:
    backend = memory_backend_status()
    router = memory_router_status()
    global_health = memory_health("global")
    hotel_health = memory_health("hotel", "own_demo_a")
    pms_records = recall_global("PMS 价格", 20)
    feishu_records = recall_global("飞书 删除 代码", 20)
    global_records = pms_records + feishu_records
    hotel_records = recall_hotel("own_demo_a", "调价 入住率 实际销售率 可售 竞品 展会", 20)
    pricing_context = recall_for_pricing_decision("own_demo_a", query="调价 入住率 可售 竞品 展会", limit=20)

    checks = (
        (
            "Mnemosyne healthcheck",
            backend["module_available"]
            and backend["enabled"]
            and global_health["recallable_records"] > 0
            and hotel_health["recallable_records"] > 0,
        ),
        (
            "PMS 能不能查价格？",
            _matches(pms_records + hotel_records, lambda text: "pms" in text and "价格" in text and any(word in text for word in ("禁止", "不能", "不得", "只用于"))),
        ),
        (
            "飞书能不能删除代码？",
            _matches(feishu_records, lambda text: "飞书" in text and ("删除" in text or "代码" in text) and any(word in text for word in ("禁止", "不能", "只读", "不允许"))),
        ),
        (
            "own_demo_a 调价要看哪些因素？",
            _matches(hotel_records + pricing_context, lambda text: "调价" in text and sum(word in text for word in ("入住率", "销售率", "可售", "竞品", "ota", "展会", "预订")) >= 3),
        ),
        (
            "入住率 73.68%，但可售只剩 4 间，是什么原因？",
            _matches(hotel_records + pricing_context, lambda text: ("入住率" in text or "在住率" in text) and "可售" in text and any(word in text for word in ("真实销售率", "当前在住率", "待入住", "已售"))),
        ),
        ("memory_router primary=mnemosyne", router["memory_primary_backend"] == "mnemosyne"),
        ("memory_router auxiliary=none", router["memory_auxiliary_backend"] == "none"),
        ("fallback=staging", router["fallback_backend"] == "staging" and CLEAN_STAGING_FILE.is_file()),
    )
    passed = True
    for label, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'} - {label}")
        passed = passed and bool(ok)
    print(f"global_recallable_records={global_health['recallable_records']}")
    print(f"own_demo_a_recallable_records={hotel_health['recallable_records']}")
    print(f"own_demo_a_active_pricing_rules={hotel_health['active_pricing_rules']}")
    print(f"own_demo_a_incomplete_rule_records={hotel_health['records_without_complete_rule_metadata']}")
    print(f"memory_context_used={len(pricing_context)}")
    return passed


if __name__ == "__main__":
    raise SystemExit(0 if verify() else 1)
