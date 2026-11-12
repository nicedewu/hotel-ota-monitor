from src.business_snapshot import _fetch_hotel_exhibitions


class _Cursor:
    def __init__(self) -> None:
        self.query = ""
        self.params = ()
        self.first_query = ""
        self.first_params = ()

    def execute(self, query: str, params: tuple) -> None:
        self.query = query
        self.params = params
        if not self.first_query:
            self.first_query, self.first_params = query, params

    def fetchall(self) -> list[dict]:
        if "SELECT venue, impact_level" in self.query:
            return []
        return [
            {
                "venue": "东方体育中心",
                "event_name": "大型电竞赛事",
                "event_start_date": "2026-08-20",
                "event_end_date": "2026-08-23",
                "stay_start_date": "2026-08-19",
                "stay_end_date": "2026-08-24",
                "category": "esports",
                "demand_level": "high",
                "hotel_impact_level": None,
            }
        ]


def test_unmapped_venue_is_excluded_from_operating_context() -> None:
    cursor = _Cursor()

    events = _fetch_hotel_exhibitions(cursor, "own_demo_a", "2026-08-21")

    assert "LEFT JOIN hotel_venue_impact_rules" in cursor.first_query
    assert cursor.first_params == ("own_demo_a", "2026-08-21", "2026-08-21")
    assert events == []


def test_known_high_impact_event_remains_available() -> None:
    cursor = _Cursor()
    rows = cursor.fetchall()
    rows[0]["hotel_impact_level"] = "high"
    cursor.fetchall = lambda: rows
    events = _fetch_hotel_exhibitions(cursor, "own_demo_a", "2026-08-21")
    assert events[0]["event_name"] == "大型电竞赛事"
    assert events[0]["hotel_impact_level"] == "high"
    assert events[0]["effective_event_level"] == "high"


def test_known_venue_alias_is_not_lost_by_today_filter() -> None:
    cursor = _Cursor()
    rows = cursor.fetchall()
    rows[0]["venue"] = "上海浦发银行东方体育中心"
    cursor.fetchall = lambda: ([{"venue": "上海东方体育中心", "impact_level": "high"}]
                             if "SELECT venue, impact_level" in cursor.query else rows)
    events = _fetch_hotel_exhibitions(cursor, "own_demo_a", "2026-08-21")
    assert events[0]["hotel_impact_level"] == "high"
    assert events[0]["display_venue"] == "上海东方体育中心"
