from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.memory_provider import filter_and_sort_pricing_rules, memory_health, recall_for_pricing_decision
from src.memory_router import recall_clean_staging


def rule_metadata(room_category: str = "standard") -> dict:
    return {
        "tenant": "hotel_ops",
        "hotel_key": "own",
        "hotel_short_name": "测试店",
        "rule_type": "business_rule",
        "scope": "room_type",
        "priority": 10,
        "source": "approved_weekly_review_candidate",
        "confidence": "high",
        "valid_from": "2026-01-01",
        "valid_to": None,
        "room_category": room_category,
    }


class FakeCursor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def fetchall(self) -> list[dict]:
        return self.rows


class FakeConnection:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def execute(self, _query: str) -> FakeCursor:
        return FakeCursor(self.rows)


class FakeMemory:
    def __init__(self, rows: list[dict], beam_rows: list[dict] | None = None) -> None:
        self.conn = FakeConnection(rows)
        self._beam_rows = beam_rows or []

    def recall(self, _query: str, top_k: int) -> list[dict]:
        return self._beam_rows[:top_k]

    def get_all_memories(self) -> list[dict]:
        return self._beam_rows

    def get_stats(self) -> dict:
        return {"total_memories": len(self.conn.rows), "last_memory": "2026-08-09T00:00:00"}


class MemoryRecallHealthTests(unittest.TestCase):
    def test_pricing_filter_requires_complete_metadata_and_matching_room(self) -> None:
        complete = rule_metadata()
        incomplete = dict(complete)
        incomplete.pop("valid_to")
        items = [
            {"content": "匹配规则", "hotel_key": "own", "memory_type": "business_rule", "metadata": complete},
            {"content": "不完整规则", "hotel_key": "own", "memory_type": "business_rule", "metadata": incomplete},
            {"content": "其他房型", "hotel_key": "own", "memory_type": "business_rule", "metadata": rule_metadata("suite")},
        ]
        rows = filter_and_sort_pricing_rules(items, "own", "2026-08-09", "standard")
        self.assertEqual([row["content"] for row in rows], ["匹配规则"])

    def test_staging_returns_no_memory_for_zero_match_query(self) -> None:
        record = {
            "scope": "hotel",
            "hotel_key": "own",
            "content": "低销售率且价差过大时，先检查库存后再决定调价。",
            "memory_type": "business_rule",
            "metadata": rule_metadata(),
        }
        with TemporaryDirectory() as directory:
            staging = Path(directory) / "cleaned.jsonl"
            staging.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
            with patch("src.memory_router.CLEAN_STAGING_FILE", staging):
                self.assertEqual(recall_clean_staging("own", "zzzz_unique_no_match", 8, "2026-08-09", "standard"), [])

    def test_legacy_metadata_is_restored_for_pricing_recall_and_health(self) -> None:
        metadata = rule_metadata()
        outer = {
            "scope": "hotel",
            "hotel_key": "own",
            "memory_type": "business_rule",
            "confidence": "high",
            "metadata": metadata,
        }
        rows = [
            {
                "id": "memory-1",
                "content": "调价时应参考同房型竞品价差。",
                "source": "approved_weekly_review_candidate",
                "timestamp": "2026-08-01T00:00:00",
                "session_id": "own",
                "importance": 0.9,
                "metadata_json": json.dumps(outer, ensure_ascii=False),
            }
        ]
        memory = FakeMemory(rows)
        with patch("src.memory_provider._enabled", return_value=True), patch("src.memory_provider._mnemosyne_instance", return_value=memory):
            recalled = recall_for_pricing_decision("own", "2026-08-09", "standard", "调价 竞品", 8)
            health = memory_health("hotel", "own")
        self.assertEqual(len(recalled), 1)
        self.assertEqual(recalled[0]["metadata"]["room_category"], "standard")
        self.assertEqual(health["recallable_records"], 1)
        self.assertEqual(health["active_pricing_rules"], 1)


if __name__ == "__main__":
    unittest.main()
