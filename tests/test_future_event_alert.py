from datetime import date

from src.future_event_alert import _impact, _message, _priority, _stage, _venue_impacts, _venue_matches


def test_d14_stage_covers_ten_day_notice() -> None:
    assert _stage(10) == "D-14"


def test_configured_high_impact_esports_event_is_high_priority() -> None:
    assert _priority(
        {"category": "esports", "source_confidence": "official", "demand_level": None},
        "high",
    ) == "high"


def test_unmapped_esports_event_does_not_receive_an_invented_hotel_impact() -> None:
    impact_level, _ = _impact({"category": "esports", "source_confidence": "official"}, "own_demo_a", {})

    assert impact_level == ""
    assert _priority({"category": "esports", "demand_level": "normal"}, impact_level) is None


def test_non_relevant_event_without_hotel_impact_is_not_pushed() -> None:
    assert _priority(
        {"category": "exhibition", "source_confidence": "official", "demand_level": "normal"},
        "",
    ) is None


def test_configured_high_impact_exhibition_is_a_future_event_push() -> None:
    assert _priority(
        {"category": "exhibition", "source_confidence": "official", "demand_level": "normal"},
        "high",
    ) == "high"


def test_corroborated_alert_is_explicitly_labeled_as_non_official() -> None:
    message = _message({
        "hotel_name": "测试酒店",
        "lead_days": 7,
        "priority": "high",
        "impact_note": "场馆影响规则已匹配",
        "event": {
            "event_name": "大型赛事",
            "event_start_date": date(2026, 9, 12),
            "event_end_date": date(2026, 9, 13),
            "venue": "东方体育中心",
            "source_confidence": "corroborated",
        },
    })

    assert "至少3个独立网站交叉证实" in message
    assert "尚未抓取到官方页面" in message


def test_sponsored_venue_name_matches_base_name_without_fuzzy_collision() -> None:
    assert _venue_matches("上海浦发银行东方体育中心", "上海东方体育中心")
    assert _venue_matches(
        "静安体育中心（小组赛及淘汰赛）、梅赛德斯-奔驰文化中心（总决赛）",
        "静安体育中心 / 梅赛德斯-奔驰文化中心",
    )
    assert not _venue_matches("上海体育馆", "上海体育场")


def test_venue_impacts_reads_rule_venue_for_alias_matching() -> None:
    class Cursor:
        def execute(self, *_args):
            pass

        def fetchall(self):
            return [{"own_hotel_key": "hotel-1", "venue": "上海东方体育中心", "impact_level": "high"}]

    assert _venue_impacts(Cursor(), ["hotel-1"], "上海浦发银行东方体育中心") == {"hotel-1": "high"}
