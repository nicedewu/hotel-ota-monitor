from datetime import date
import unittest

from src.weekly_review import WeeklyHotel, summarize_metrics


def _pms_row(stay_date: date, sold_rooms: int = 8) -> dict[str, object]:
    return {
        "stay_date": stay_date,
        "collected_at": f"{stay_date.isoformat()}T05:00:00+08:00",
        "room_total": 10,
        "sold_rooms": sold_rooms,
        "available_rooms": 10 - sold_rooms,
    }


class WeeklyFinalOccupancyTests(unittest.TestCase):
    def test_missing_previous_final_disables_week_over_week_comparison(self):
        hotel = WeeklyHotel("own_demo", "示例店", "own", [], True, "", False, "")
        current = [_pms_row(date(2026, 8, day)) for day in range(2, 9)]
        previous = [_pms_row(date(2026, 7, day)) for day in range(26, 32)]
        metrics = summarize_metrics(hotel, {
            "pms_rows": current,
            "previous_pms_rows": previous,
            "pms_current_week_final_missing_dates": [],
            "pms_previous_week_final_missing_dates": ["2026-08-01"],
            "source_status": {"pms": "hotel_daily_operating_metrics.metrics.snapshots.final"},
        })

        self.assertEqual(metrics["data_status"], "degraded")
        self.assertEqual(metrics["valid_days"], 7)
        self.assertEqual(metrics["previous_valid_days"], 6)
        self.assertFalse(metrics["week_over_week_available"])
        self.assertIsNone(metrics["previous_week_sales_rate"])
        self.assertIsNone(metrics["week_over_week_pp"])
        self.assertIn("环比基准 final 入住率缺失日期：2026-08-01", metrics["missing_data"])


if __name__ == "__main__":
    unittest.main()
