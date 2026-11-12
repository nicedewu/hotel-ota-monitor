from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from scripts.weekly_review_candidates import generate
from src.weekly_review import (
    WeeklyHotel,
    WeeklyReviewError,
    _build_fact_snapshot,
    _weekly_preflight_targets,
    run_weekly_review,
)


def _row(stay_date: date) -> dict[str, object]:
    return {"stay_date": stay_date, "room_total": 10, "sold_rooms": 8, "available_rooms": 2}


def test_preflight_requires_current_and_previous_final_days() -> None:
    hotel = WeeklyHotel("own_demo", "示例店", "own", [], True, "", False, "")
    facts = {
        "pms_rows": [_row(date(2026, 8, day)) for day in range(3, 9)],
        "previous_pms_rows": [_row(date(2026, 7, day)) for day in range(27, 32)] + [_row(date(2026, 8, day)) for day in range(1, 3)],
        "pms_current_week_final_missing_dates": ["2026-08-09"],
        "pms_previous_week_final_missing_dates": [],
        "source_status": {"pms": "hotel_daily_operating_metrics.metrics.snapshots.final"},
    }
    with patch("src.weekly_review.fetch_weekly_facts", return_value=facts):
        result = _weekly_preflight_targets([hotel], date(2026, 8, 3), date(2026, 8, 9))
    assert result["ready"] is False
    assert result["hotel_results"][0]["valid_final_days"] == 6
    assert "2026-08-09" in " ".join(result["hotel_results"][0]["missing_final_data"])


def test_required_preflight_stops_generation_before_report_writes() -> None:
    hotel = WeeklyHotel("own_demo", "示例店", "own", [], True, "", False, "")
    failed = {"ready": False, "hotel_results": [{"hotel_key": "own_demo", "ready": False, "missing_final_data": ["本周 final 入住率缺失"]}]}
    with (
        patch("src.weekly_review.load_weekly_hotels", return_value={hotel.key: hotel}),
        patch("src.weekly_review._weekly_preflight_targets", return_value=failed),
        patch("src.weekly_review.build_single_hotel_review") as build,
    ):
        with pytest.raises(WeeklyReviewError, match="预检未通过"):
            run_weekly_review(
                week_start="2026-08-03", week_end="2026-08-09", auto_last_week=False,
                hotel_key="all", dry_run=False, notify=False, write_db=False,
                admin_overview=False, output_dir="output/test", require_complete_final=True,
            )
    build.assert_not_called()


def test_fact_snapshot_omits_raw_payloads() -> None:
    hotel = WeeklyHotel("own_demo", "示例店", "own", [], True, "", False, "")
    snapshot = _build_fact_snapshot(
        hotel, date(2026, 8, 3), date(2026, 8, 9), "run-1",
        {"data_status": "complete", "daily_pms": []},
        {
            "price_comparisons": [{"stay_date": "2026-08-03", "raw": {"payload": "x" * 1000}}],
            "price_decisions": [{"stay_date": "2026-08-03", "input_snapshot": {"payload": "x" * 1000}}],
            "exhibition_events": [],
            "inventory_change_events": [],
        },
        [],
    )
    assert "raw" not in snapshot["facts"]["price_comparisons"][0]
    assert "input_snapshot" not in snapshot["facts"]["price_decisions"][0]


def test_degraded_analysis_does_not_create_candidates() -> None:
    connection = MagicMock()
    cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = {
        "id": 1,
        "report_json": {"analysis_status": "completed", "fact_quality_passed": True, "analysis_mode": "rules_fallback", "analysis_degraded": True},
        "fact_snapshot_json": {},
    }
    with patch("scripts.weekly_review_candidates.connect", return_value=connection):
        result = generate("2026-08-03", "own_demo", write=True)
    assert result["skip_reason"] == "analysis_degraded"
    assert result["drafts"] == []
    assert cursor.execute.call_count == 1
