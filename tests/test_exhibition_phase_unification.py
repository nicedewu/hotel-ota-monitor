import unittest

from src.exhibition_timing import annotate_events_for_period, partition_events_for_day
from src.price_decision import _event_demand_level
from src.weekly_review import _event_lines


EVENTS = [
    {
        "event_name": "2026世界人工智能大会",
        "event_start_date": "2026-07-17",
        "event_end_date": "2026-07-20",
        "stay_start_date": "2026-07-15",
        "stay_end_date": "2026-07-21",
        "demand_level": "high",
    },
    {
        "event_name": "2026上海婚纱摄影与视觉影像产业展群",
        "event_start_date": "2026-07-23",
        "event_end_date": "2026-07-25",
        "stay_start_date": "2026-07-21",
        "stay_end_date": "2026-07-26",
        "demand_level": "high",
    },
]


class ExhibitionPhaseUnificationTest(unittest.TestCase):
    def test_coverage_events_do_not_supply_price_demand(self) -> None:
        active, coverage = partition_events_for_day("2026-07-21", EVENTS)

        self.assertEqual([], active)
        self.assertEqual(["post_event_checkout", "pre_event_stay"], [event["event_phase"] for event in coverage])
        self.assertEqual("unknown", _event_demand_level(active))

    def test_weekly_coverage_is_rendered_as_coverage_not_event_day(self) -> None:
        rows = annotate_events_for_period("2026-07-21", "2026-07-27", EVENTS)
        lines = _event_lines(rows)

        self.assertEqual(["coverage_only", "active_in_period"], [row["period_phase"] for row in rows])
        self.assertIn("仅为住宿覆盖信息", lines[0])
        self.assertIn("实际展期 2026-07-23 至 2026-07-25", lines[1])


if __name__ == "__main__":
    unittest.main()
