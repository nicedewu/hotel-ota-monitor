from __future__ import annotations

import json
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.weekly_review import WeeklyHotel, build_admin_review, notify_existing_weekly_reviews, notify_weekly_report, render_single_markdown
from src.weekly_review_analyzer import WeeklyAnalysisError, _compact_analysis_input, validate_weekly_analysis
from src.weekly_review_rules import build_rules_analysis


def snapshot() -> dict:
    return {
        "schema_version": "1.0",
        "run_id": "batch-1",
        "hotel_key": "own_demo_a",
        "period_start": "2026-07-13",
        "period_end": "2026-07-19",
        "fact_quality": {"passed": True, "missing_sources": [], "warnings": []},
        "facts": {"metrics": {"weekly_sales_rate": 72.5, "week_over_week_pp": -2, "min_available_rooms": 4}},
        "fact_references": ["fact:metrics:2026-07-13:own_demo_a", "fact:inventory:2026-07-13:own_demo_a"],
    }


def analysis() -> dict:
    return {
        "schema_version": "1.0", "run_id": "batch-1", "hotel_key": "own_demo_a",
        "period_start": "2026-07-13", "period_end": "2026-07-19", "summary": "本周销售率稳定。",
        "key_findings": [{"finding": "销售率为 72.5%。", "fact_refs": ["fact:metrics:2026-07-13:own_demo_a"]}],
        "risks": [{"risk": "库存较紧。", "fact_refs": ["fact:inventory:2026-07-13:own_demo_a"]}],
        "actions": [{"action": "继续观察库存。", "reason": "库存较紧。", "fact_refs": ["fact:inventory:2026-07-13:own_demo_a"]}],
        "quality": {"unsupported_claims": [], "warnings": []},
    }


class WeeklyAnalysisContractTests(unittest.TestCase):
    def test_rules_fallback_has_only_input_fact_refs(self) -> None:
        result = build_rules_analysis(snapshot(), "HERMES_TIMEOUT")
        validate_weekly_analysis(result, snapshot())
        self.assertIn("HERMES_TIMEOUT", result["quality"]["warnings"])

    def test_valid_hermes_result_is_accepted(self) -> None:
        validate_weekly_analysis(analysis(), snapshot())

    def test_wrong_hotel_is_rejected(self) -> None:
        value = analysis()
        value["hotel_key"] = "other_hotel"
        with self.assertRaisesRegex(WeeklyAnalysisError, "hotel_key"):
            validate_weekly_analysis(value, snapshot())

    def test_unknown_fact_ref_is_rejected(self) -> None:
        value = analysis()
        value["actions"][0]["fact_refs"] = ["fact:invented"]
        with self.assertRaisesRegex(WeeklyAnalysisError, "事实引用"):
            validate_weekly_analysis(value, snapshot())

    def test_more_than_three_actions_is_rejected(self) -> None:
        value = analysis()
        value["actions"] *= 4
        with self.assertRaisesRegex(WeeklyAnalysisError, "actions"):
            validate_weekly_analysis(value, snapshot())

    def test_action_without_reason_is_rejected(self) -> None:
        value = analysis()
        value["actions"][0].pop("reason")
        with self.assertRaisesRegex(WeeklyAnalysisError, "适用条件或事实原因"):
            validate_weekly_analysis(value, snapshot())

    def test_oversized_raw_facts_are_compacted_for_oneshot_argument(self) -> None:
        value = snapshot()
        value["facts"]["raw"] = [{"payload": "x" * 10000} for _ in range(40)]
        compacted = _compact_analysis_input(value)
        encoded = json.dumps(compacted, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.assertLess(len(encoded), 131072)
        self.assertNotIn("payload", json.dumps(compacted, ensure_ascii=False))
        self.assertEqual(compacted["run_id"], value["run_id"])
        self.assertEqual(compacted["fact_references"], value["fact_references"])


class WeeklyDeliveryContractTests(unittest.TestCase):
    def _hotel(self) -> WeeklyHotel:
        return WeeklyHotel("own_demo_a", "体育馆店", "own", [], True, "TEST_WEBHOOK", False, "", True)

    def test_rules_fallback_can_be_delivered_from_saved_record(self) -> None:
        payload = {
            "run_id": "batch-1", "hotel_key": "own_demo_a", "week_start": "2026-07-13", "week_end": "2026-07-19",
            "analysis_mode": "rules_fallback", "analysis_status": "completed", "fact_quality_passed": True,
        }
        state = {"own_demo_a": {"report_json": payload, "report_markdown": "# 周报", "run_id": "batch-1"}}
        with TemporaryDirectory() as directory, patch("src.weekly_review.load_weekly_hotels", return_value={"own_demo_a": self._hotel()}), patch("src.weekly_review.fetch_weekly_review_states", return_value=state), patch("src.weekly_review.notify_weekly_report", return_value="sent"), patch("src.weekly_review.update_weekly_review_notify_status", return_value="updated"):
            result = notify_existing_weekly_reviews(week_start="2026-07-13", week_end="2026-07-19", auto_last_week=False, hotel_key="all", dry_run=False, output_dir=directory)
        self.assertEqual(result["notify_status"], "sent")

    def test_weekly_delivery_uses_branch_ledger_identity(self) -> None:
        with patch("src.weekly_review.subprocess.run") as run:
            self.assertEqual(notify_weekly_report("report.md", "TEST_WEBHOOK", False, hotel_key="own_demo_a", source_record_id="run-1:own_demo_a"), "sent")
        command = run.call_args.args[0]
        self.assertIn("--branch-push-type", command)
        self.assertIn("weekly_review", command)
        self.assertIn("run-1:own_demo_a", command)

    def test_short_renderer_uses_analysis_not_free_text(self) -> None:
        payload = {"hotel_name": "体育馆店", "week_start": "2026-07-13", "week_end": "2026-07-19", "metrics": {}, "core_finding": "结论", "risk": "风险", "opportunity": "机会", "analysis": analysis()}
        text = render_single_markdown(payload)
        self.assertIn("继续观察库存", text)
        self.assertNotIn("memory_candidates", text)

    def test_admin_review_preserves_hotel_isolation(self) -> None:
        hotel = self._hotel()
        item = {"hotel": hotel, "json": {"data_status": "complete", "metrics": {"total_sold_room_nights": 10, "total_room_nights": 20, "previous_total_sold_room_nights": 9, "previous_total_room_nights": 20, "weekly_sales_rate": 50, "week_over_week_pp": 5}, "core_finding": "本店结论", "action": ["本店动作"]}}
        with TemporaryDirectory() as directory:
            result = build_admin_review([item], date(2026, 7, 13), date(2026, 7, 19), Path(directory))
        self.assertEqual(result["db_row"]["hotel_keys"], ["own_demo_a"])


if __name__ == "__main__":
    unittest.main()
