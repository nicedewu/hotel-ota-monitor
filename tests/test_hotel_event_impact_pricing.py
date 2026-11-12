import unittest

from src.price_decision import _effective_event_level, _fetch_hotel_exhibitions, _with_hotel_event_impact


class _EmptyCursor:
    def __init__(self):
        self.sql = ""
        self.params = ()

    def execute(self, sql, params):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return []


class HotelVenueRuleLookupTest(unittest.TestCase):
    def test_unconfigured_venue_has_no_hotel_impact_signal(self):
        cursor = _EmptyCursor()
        self.assertEqual([], _fetch_hotel_exhibitions(cursor, "own_demo_a", "2026-07-31"))
        self.assertIn("hotel_venue_impact_rules", cursor.sql)
        self.assertNotIn("hotel_event_impact", cursor.sql)
        self.assertEqual(("own_demo_a", "2026-07-31", "2026-07-31"), cursor.params)


class HotelEventImpactPricingTest(unittest.TestCase):
    def test_confirmed_matrix(self):
        expected = {
            ("high", "high"): "high", ("high", "medium"): "high", ("high", "low"): "medium",
            ("medium", "high"): "high", ("medium", "medium"): "medium", ("medium", "low"): "normal",
            ("normal", "high"): "medium", ("normal", "medium"): "normal", ("normal", "low"): "normal",
        }
        for pair, level in expected.items():
            with self.subTest(pair=pair): self.assertEqual(level, _effective_event_level(*pair))

    def test_event_keeps_audit_levels(self):
        event = _with_hotel_event_impact({"event_name": "ChinaJoy", "demand_level": "high", "hotel_impact_level": "low"})
        self.assertEqual("high", event["global_event_demand_level"])
        self.assertEqual("low", event["hotel_impact_level"])
        self.assertEqual("medium", event["effective_event_level"])
        self.assertEqual("medium", event["demand_level"])
