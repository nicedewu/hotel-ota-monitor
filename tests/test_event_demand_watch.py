from datetime import date

from src.event_demand_watch import _scan_days


def test_event_tail_is_clipped_to_downstream_scan_horizon():
    days = _scan_days(date(2026, 9, 24), date(2026, 10, 18), date(2026, 9, 5), 30)

    assert days[0] == date(2026, 9, 24)
    assert days[-1] == date(2026, 10, 5)
    assert date(2026, 10, 6) not in days


def test_past_and_anchor_dates_are_not_sent_to_future_scan():
    assert _scan_days(date(2026, 9, 4), date(2026, 9, 5), date(2026, 9, 5), 30) == []
