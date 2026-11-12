from datetime import date

from src.demand_attribution import (
    _persist_anomaly,
    build_city_anomaly,
    build_shadow_alert_card,
    evaluation_dates,
    rank_event_evidence,
)


def _signal(hotel_key: str, pickup: int, score: int = 45, window_hours: int = 24) -> dict:
    return {
        "hotel_key": hotel_key,
        "net_pickup": pickup,
        "score": score,
        "status": "preliminary_watch",
        "window_hours": window_hours,
    }


class _DictRowCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def execute(self, statement: str, params: object = None) -> None:
        self.calls.append((statement, params))

    def fetchone(self) -> dict[str, int]:
        return {"id": 42}


def test_multi_hotel_pickup_becomes_a_preliminary_city_signal() -> None:
    anomaly = build_city_anomaly(
        date(2026, 8, 20),
        [_signal("hotel_a", 4), _signal("hotel_b", 4)],
    )

    assert anomaly["total_net_pickup"] == 8
    assert anomaly["hotel_keys"] == ["hotel_a", "hotel_b"]
    assert anomaly["status"] == "preliminary"
    assert anomaly["features"]["sales_claimed"] is False


def test_single_hotel_signal_does_not_claim_a_city_event() -> None:
    anomaly = build_city_anomaly(date(2026, 8, 20), [_signal("hotel_a", 10)])

    assert anomaly["status"] == "single_hotel_observation"


def test_long_window_confirms_persistence_without_double_counting_a_hotel() -> None:
    anomaly = build_city_anomaly(
        date(2026, 8, 20),
        [
            _signal("hotel_a", 4, 45, 24),
            _signal("hotel_a", 8, 55, 72),
            _signal("hotel_b", 4, 45, 24),
            _signal("hotel_b", 8, 55, 72),
        ],
    )

    assert anomaly["signal_count"] == 2
    assert anomaly["total_net_pickup"] == 8
    assert anomaly["features"]["persistent_hotel_count"] == 2
    assert anomaly["features"]["persistence_points"] == 10


def test_no_pickup_is_not_mislabeled_as_a_data_quality_failure() -> None:
    anomaly = build_city_anomaly(date(2026, 8, 20), [_signal("hotel_a", 0)])

    assert anomaly["signal_count"] == 0
    assert anomaly["features"]["data_quality_blocked_signals"] == 0


def test_evaluation_dates_cover_the_exact_requested_window() -> None:
    assert evaluation_dates(date(2026, 8, 20), 3) == [
        date(2026, 8, 20),
        date(2026, 8, 21),
        date(2026, 8, 22),
    ]


def test_persist_anomaly_reads_the_id_from_a_dict_row() -> None:
    cursor = _DictRowCursor()
    anomaly = build_city_anomaly(date(2026, 8, 20), [_signal("hotel_a", 1)])

    assert _persist_anomaly(cursor, anomaly, []) == 42
    assert len(cursor.calls) == 2


def test_official_confirmed_event_ranks_above_unverified_clue() -> None:
    anomaly = build_city_anomaly(
        date(2026, 8, 20),
        [_signal("hotel_a", 4), _signal("hotel_b", 4)],
    )
    official = rank_event_evidence(
        anomaly,
        {
            "event_kind": "canonical",
            "event_reference": "canonical:1",
            "event_name": "DOTA2 国际邀请赛",
            "source_confidence": "authoritative",
            "event_status": "confirmed",
            "date_confidence": 100,
        },
    )
    clue = rank_event_evidence(
        anomaly,
        {
            "event_kind": "candidate",
            "event_reference": "candidate:2",
            "event_name": "疑似活动",
            "source_confidence": "discovery",
            "event_status": "discovered",
            "date_confidence": 0,
        },
    )

    assert official["attribution_score"] > clue["attribution_score"]
    assert official["attribution_score"] >= 80


def test_high_confidence_multi_hotel_signal_builds_an_eligible_shadow_alert() -> None:
    anomaly = build_city_anomaly(
        date(2026, 8, 20),
        [_signal("hotel_a", 5, 65), _signal("hotel_b", 5, 65)],
    )
    attribution = rank_event_evidence(
        anomaly,
        {
            "event_kind": "canonical",
            "event_reference": "canonical:1",
            "event_name": "大型展会",
            "event_start_date": date(2026, 8, 19),
            "event_end_date": date(2026, 8, 21),
            "source_confidence": "authoritative",
            "event_status": "confirmed",
            "date_confidence": 100,
            "venue_confidence": 100,
            "demand_level": "high",
            "hotel_venue_impacts": {"hotel_a": "high", "hotel_b": "medium"},
        },
    )

    card = build_shadow_alert_card(anomaly, [attribution])

    assert card is not None
    assert card["mode"] == "shadow"
    assert card["eligible_for_business_alert"] is True
    assert card["priority"] == "high"
    assert "不自动调价" in card["guardrails"][1]


def test_strong_multi_hotel_demand_is_eligible_without_an_event_explanation() -> None:
    anomaly = build_city_anomaly(
        date(2026, 8, 20),
        [_signal("hotel_a", 5, 65), _signal("hotel_b", 5, 65)],
    )

    card = build_shadow_alert_card(anomaly, [])

    assert card is not None
    assert card["eligible_for_business_alert"] is True
    assert card["target_hotel_keys"] == ["hotel_a", "hotel_b"]
    assert "原因待核实" in card["title"]


def test_persistent_single_hotel_demand_is_eligible() -> None:
    anomaly = build_city_anomaly(
        date(2026, 8, 20),
        [_signal("hotel_a", 10, 55, 24), _signal("hotel_a", 12, 60, 72)],
    )
    attribution = rank_event_evidence(
        anomaly,
        {
            "event_kind": "canonical",
            "event_reference": "canonical:1",
            "event_name": "大型展会",
            "source_confidence": "authoritative",
            "event_status": "confirmed",
            "date_confidence": 100,
            "offline_mode": "offline",
        },
    )

    card = build_shadow_alert_card(anomaly, [attribution])

    assert card is not None
    assert card["eligible_for_business_alert"] is True
    assert card["target_hotel_keys"] == ["hotel_a"]


def test_medium_signal_requires_high_venue_impact_for_that_hotel() -> None:
    anomaly = build_city_anomaly(date(2026, 8, 20), [_signal("hotel_a", 5, 45)])
    attribution = rank_event_evidence(
        anomaly,
        {
            "event_kind": "candidate",
            "event_reference": "candidate:1",
            "event_name": "大型线下活动",
            "source_confidence": "official",
            "event_status": "confirmed",
            "date_confidence": 95,
            "offline_mode": "offline",
            "hotel_venue_impacts": {"hotel_a": "high"},
        },
    )

    card = build_shadow_alert_card(anomaly, [attribution])

    assert card is not None
    assert card["eligible_for_business_alert"] is True
    assert card["target_reasons"]["hotel_a"] == "中等需求异常且本店场馆影响高"
