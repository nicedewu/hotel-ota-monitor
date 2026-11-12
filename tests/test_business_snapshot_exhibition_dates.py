import unittest

from src.business_snapshot import _build_exhibition_context


class ExhibitionDateContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.events = [
            {
                "event_name": "2026世界人工智能大会",
                "event_start_date": "2026-07-17",
                "event_end_date": "2026-07-20",
                "stay_start_date": "2026-07-15",
                "stay_end_date": "2026-07-21",
            },
            {
                "event_name": "2026上海婚纱摄影与视觉影像产业展群",
                "event_start_date": "2026-07-23",
                "event_end_date": "2026-07-25",
                "stay_start_date": "2026-07-21",
                "stay_end_date": "2026-07-26",
            },
        ]

    def test_coverage_day_is_not_an_active_exhibition_day(self) -> None:
        context = _build_exhibition_context("2026-07-21", self.events, True)

        self.assertFalse(context["matched"])
        self.assertEqual("post_event_checkout", context["events"][0]["event_phase"])
        self.assertEqual("pre_event_stay", context["events"][1]["event_phase"])
        self.assertIn("今日无进行中展会", context["message"])


if __name__ == "__main__":
    unittest.main()
