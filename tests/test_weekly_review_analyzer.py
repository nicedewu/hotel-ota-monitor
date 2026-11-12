from __future__ import annotations

import json
from subprocess import CompletedProcess
from unittest import TestCase
from unittest.mock import patch

from src.hermes_json import normalize_hermes_json_output
from src.weekly_review_analyzer import analyze_weekly_snapshot


def _snapshot() -> dict:
    return {
        "schema_version": "1.0",
        "run_id": "batch-1",
        "hotel_key": "own_demo_a",
        "period_start": "2026-07-13",
        "period_end": "2026-07-19",
        "fact_references": ["fact:metrics:2026-07-13:own_demo_a"],
        "facts": {"metrics": {"weekly_sales_rate": 72.5}},
    }


def _analysis() -> dict:
    return {
        "schema_version": "1.0",
        "run_id": "batch-1",
        "hotel_key": "own_demo_a",
        "period_start": "2026-07-13",
        "period_end": "2026-07-19",
        "summary": "本周销售率稳定。",
        "key_findings": [{"finding": "销售率稳定。", "fact_refs": ["fact:metrics:2026-07-13:own_demo_a"]}],
        "risks": [{"risk": "持续关注库存。", "fact_refs": ["fact:metrics:2026-07-13:own_demo_a"]}],
        "actions": [{"action": "继续观察库存。", "reason": "销售率稳定。", "fact_refs": ["fact:metrics:2026-07-13:own_demo_a"]}],
        "quality": {"unsupported_claims": [], "warnings": []},
    }


class WeeklyReviewAnalyzerTests(TestCase):
    def test_complete_json_fence_is_normalized(self) -> None:
        raw = "```json\n{\"key\": \"value\"}\n```"
        self.assertEqual(normalize_hermes_json_output(raw), ('{\"key\": \"value\"}', "markdown_fence"))

    def test_weekly_analysis_accepts_fenced_json(self) -> None:
        raw_output = "```json\n" + json.dumps(_analysis(), ensure_ascii=False) + "\n```"
        result = CompletedProcess(args=["hermes"], returncode=0, stdout=raw_output, stderr="")
        with (
            patch("src.weekly_review_analyzer.Path.is_file", return_value=True),
            patch("src.weekly_review_analyzer._version", return_value="test-model"),
            patch("src.weekly_review_analyzer.subprocess.run", return_value=result),
        ):
            output, diagnostics = analyze_weekly_snapshot(_snapshot())
        self.assertEqual(output["hotel_key"], "own_demo_a")
        self.assertEqual(diagnostics["analysis_output_wrapper"], "markdown_fence")
