from datetime import date
import unittest

from src.historical_facts import (
    fact_comparison_records,
    fact_comparison_rows,
    fact_decision_rows,
    merge_prefer_new,
    metrics_to_pms_rows,
)


class HistoricalFactsTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {
            "id": 9,
            "hotel_key": "own_demo",
            "generated_at": "2026-07-20T20:00:00+08:00",
            "fact_snapshot_json": {
                "run_id": "run-9",
                "facts": {
                    "price_decisions": [{
                        "id": 1,
                        "hotel_key": "own_demo",
                        "stay_date": "2026-07-20",
                        "room_category": "double",
                        "current_price": 300,
                        "competitor_min_price": 280,
                        "suggested_price": 290,
                        "action": "小降",
                        "decision_at": "2026-07-20T18:00:00+08:00",
                        "raw": {"own_daily_status_source": {"room_total": 10, "sold_rooms": 8, "available_rooms": 2, "occupancy_rate": 80}},
                    }],
                    "price_comparisons": [{
                        "own_hotel_key": "own_demo",
                        "competitor_hotel_key": "comp_demo",
                        "stay_date": "2026-07-20",
                        "normalized_room_type": "double",
                        "own_price": 300,
                        "competitor_price": 280,
                        "own_room_name": "自有大床",
                        "competitor_room_name": "竞品大床",
                        "collected_at": "2026-07-20T18:00:00+08:00",
                    }],
                },
            },
        }

    def test_flatten_structured_facts(self):
        decisions = fact_decision_rows([self.snapshot], start_date=date(2026, 7, 20), end_date=date(2026, 7, 21))
        self.assertEqual(decisions[0]["_source_ref"], "weekly_reviews.fact_snapshot_json:9")
        self.assertEqual(decisions[0]["available_rooms"], 2)
        prices = fact_comparison_rows([self.snapshot], start_date=date(2026, 7, 20), end_date=date(2026, 7, 20), own_hotel_key="own_demo")
        self.assertEqual({row["hotel_key"] for row in prices}, {"own_demo", "comp_demo"})
        self.assertEqual(fact_comparison_records([self.snapshot], own_hotel_key="own_demo")[0]["normalized_room_type"], "double")

    def test_metrics_final_only_ignores_intraday_snapshots_and_missing_final(self):
        rows = metrics_to_pms_rows([{
            "business_date": date(2026, 7, 20),
            "updated_at": "2026-07-20T20:00:00+08:00",
            "metrics": {
                "hotel_key": "own_demo",
                "snapshots": {
                    "morning": {"room_total": 10, "sold_rooms": 5, "available_rooms": 5, "collected_at": "2026-07-20T09:00:00+08:00"},
                    "evening": {"room_total": 10, "sold_rooms": 8, "available_rooms": 2, "collected_at": "2026-07-20T18:00:00+08:00"},
                },
            },
        }], required_slot="final")
        self.assertEqual(rows, [])

        rows = metrics_to_pms_rows([{
            "business_date": date(2026, 7, 20),
            "updated_at": "2026-07-20T20:00:00+08:00",
            "metrics": {
                "hotel_key": "own_demo",
                "snapshots": {
                    "evening": {"room_total": 10, "sold_rooms": 8, "available_rooms": 2, "collected_at": "2026-07-20T18:00:00+08:00"},
                    "final": {"room_total": 10, "sold_rooms": 9, "available_rooms": 1, "collected_at": "2026-07-21T05:00:00+08:00"},
                },
            },
        }], required_slot="final")
        merged = merge_prefer_new(rows, [{"stay_date": date(2026, 7, 20), "room_total": 99}], ("stay_date",))
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["sold_rooms"], 9)
        self.assertEqual(merged[0]["raw"]["snapshot_slot"], "final")


if __name__ == "__main__":
    unittest.main()
