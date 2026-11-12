#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.memory_provider import recall_for_pricing_decision, recall_global


STAGING_FILES = (
    Path("output/memory_staging/mnemosyne_pending.jsonl"),
    Path("output/memory_staging/existing_memory_export.jsonl"),
    Path("output/memory_staging/timescale_experience_candidates.jsonl"),
)


def load_staging(paths: tuple[Path, ...] = STAGING_FILES) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                records.append(item)
    return records


def _matches(records: list[dict[str, Any]], predicate: Callable[[str], bool]) -> bool:
    for item in records:
        text = " ".join(
            str(value)
            for value in (item.get("content"), item.get("hotel_key"), item.get("memory_type"))
            if value is not None
        )
        if predicate(text.lower()):
            return True
    return False


def verify() -> bool:
    enabled = os.getenv("MNEMOSYNE_ENABLED", "false").strip().lower() == "true"
    if enabled:
        global_records = recall_global("PMS 价格 飞书 删除代码 安全规则", limit=20)
        hotel_records = recall_for_pricing_decision(
            "own_demo_a",
            query="调价因素 入住率 73.68 可售 4 间 竞品 展会",
            limit=20,
        )
        records = global_records + hotel_records
        source = "Mnemosyne"
        if not records:
            records = load_staging()
            source = "staging JSONL fallback"
    else:
        records = load_staging()
        source = "staging JSONL"

    checks = (
        (
            "PMS 能不能查价格？",
            lambda text: "pms" in text and "价格" in text and any(word in text for word in ("禁止", "不能", "禁用", "不允许")),
        ),
        (
            "飞书能不能删除代码？",
            lambda text: "飞书" in text and "删除" in text and any(word in text for word in ("禁止", "不能", "只读", "不允许")),
        ),
        (
            "own_demo_a 的调价要看哪些因素？",
            lambda text: ("own_demo_a" in text or "调价" in text)
            and sum(word in text for word in ("真实销售率", "入住率", "可售", "竞品", "ota", "展会")) >= 2,
        ),
        (
            "入住率 73.68% 但可售只剩 4 间是什么原因？",
            lambda text: ("入住率" in text or "在住率" in text)
            and "可售" in text
            and any(word in text for word in ("73.68", "4 间", "待入住", "真实销售率", "当前在住率")),
        ),
    )
    passed = True
    for question, predicate in checks:
        ok = _matches(records, predicate)
        print(f"{'PASS' if ok else 'FAIL'} - {question}")
        passed = passed and ok
    print(f"检查来源: {source}；记录数: {len(records)}")
    return passed


if __name__ == "__main__":
    raise SystemExit(0 if verify() else 1)
