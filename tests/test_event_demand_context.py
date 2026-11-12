from datetime import date

from src.event_demand_context import _fusion_advice, _snapshot_priority, _snapshot_impact, _venue_key
from src.future_event_alert import _impact
from src.business_snapshot import _future_event_demand_lines, _recent_event_lines


def test_fusion_requires_a_confirmed_demand_signal() -> None:
    event = {
        "event_reference": "candidate:809",
        "event_name": "DOTA2 国际邀请赛",
        "event_start_date": date(2026, 8, 20),
        "event_end_date": date(2026, 8, 23),
        "priority": "high",
    }
    observed = {"stay_date": date(2026, 8, 20), "confirmed": False, "state": "开始起量", "primary_score": 50}
    confirmed = {"stay_date": date(2026, 8, 20), "confirmed": True, "state": "持续起量", "primary_score": 62}

    assert _fusion_advice("own_demo_a", [event], [observed]) == []
    assert len(_fusion_advice("own_demo_a", [event], [confirmed])) == 1


def test_qwen_low_lodging_relevance_blocks_even_a_known_venue() -> None:
    event = {"category": "performance", "source_confidence": "authoritative", "search_impact_level": "low"}
    assert _snapshot_priority(event, "high") is None
    assert _impact(event, "own_demo_a", {"own_demo_a": "high"})[0] == "low"


def test_hotel_specific_judgment_respects_each_store_rule() -> None:
    event = {"venue": "示例展馆 A", "hotel_relevance": [
        {"hotel_key": "own_demo_a", "impact_level": "high", "reason": "事实：场馆举办专业展会；推断：跨城参展住宿。", "source_indexes": [1]},
        {"hotel_key": "own_minhang", "impact_level": "high", "reason": "模型给出较高判断", "source_indexes": [1]},
    ]}
    assert _snapshot_impact(event, "own_demo_a", {event["venue"]: "high"})[0] == "high"
    assert _snapshot_impact(event, "own_minhang", {event["venue"]: "low"})[0] == "low"
    assert _snapshot_impact(event, "own_unknown", {event["venue"]: "high"})[0] == "unknown"
    assert _snapshot_impact(event, "own_demo_a", {})[0] == "unknown"


def test_reported_tourism_event_is_hidden_today_and_in_future() -> None:
    event = {"event_name": "“乡约古镇·乐游青溪”活动", "venue": "百联奥特莱斯品牌广场二期中心广场",
             "event_start_date": date(2026, 9, 13), "event_end_date": date(2026, 10, 6),
             "category": "performance", "search_impact_level": "low"}
    lines = _recent_event_lines(
        {"events": [{**event, "event_phase": "active", "hotel_impact_level": "unknown"}]},
        {"anchor_date": "2026-09-12", "events": [{**event, "impact_level": "high"}]},
    )
    assert "乡约古镇" not in "\n".join(lines)
    assert "本店影响待判断" not in "\n".join(lines)
    assert "暂无与本店明显相关" in "\n".join(lines)


def test_snapshot_lines_keep_event_and_demand_in_one_section() -> None:
    lines = _future_event_demand_lines(
        {
            "events": [],
            "demand_signals": [],
            "price_control_advice": [
                {
                    "stay_date": date(2026, 8, 20),
                    "event_name": "DOTA2 国际邀请赛",
                    "demand_state": "持续起量",
                    "demand_score": 62,
                    "recommended_action": "人工核对后评估调价。",
                }
            ],
        }
    )

    assert any("DOTA2" in line for line in lines)
    assert any("不自动调价" in line for line in lines)
    assert not any("信号" in line for line in lines)


def test_snapshot_prioritizes_high_impact_event_over_earlier_medium_events() -> None:
    lines = _future_event_demand_lines(
        {
            "anchor_date": "2026-09-05",
            "events": [
                {
                    "event_name": "普通展会A",
                    "event_start_date": date(2026, 9, 7),
                    "event_end_date": date(2026, 9, 7),
                    "display_venue": "场馆A",
                    "impact_level": "medium",
                },
                {
                    "event_name": "普通展会B",
                    "event_start_date": date(2026, 9, 8),
                    "event_end_date": date(2026, 9, 8),
                    "display_venue": "场馆B",
                    "impact_level": "medium",
                },
                {
                    "event_name": "普通展会C",
                    "event_start_date": date(2026, 9, 9),
                    "event_end_date": date(2026, 9, 9),
                    "display_venue": "场馆C",
                    "impact_level": "medium",
                },
                {
                    "event_name": "高影响赛事",
                    "event_start_date": date(2026, 9, 12),
                    "event_end_date": date(2026, 9, 13),
                    "display_venue": "赛事场馆",
                    "impact_level": "high",
                },
            ],
            "demand_signals": [],
            "price_control_advice": [],
        }
    )

    text = "\n".join(lines)
    assert "普通展会A" in text
    assert "普通展会B" in text
    assert "普通展会C" not in text
    assert "高影响赛事" in text
    assert text.index("普通展会A") < text.index("高影响赛事")


def test_snapshot_includes_medium_or_high_impact_exhibitions() -> None:
    event = {"category": "exhibition", "demand_level": "normal"}

    assert _snapshot_priority(event, "medium") == "medium"
    assert _snapshot_priority(event, "low") is None


def test_snapshot_excludes_trusted_major_event_without_hotel_association() -> None:
    assert _snapshot_priority(
        {"category": "performance", "source_confidence": "official"},
        "unknown",
    ) is None
    assert _snapshot_priority(
        {"category": "performance", "source_confidence": "discovery"},
        "unknown",
    ) is None
    assert _snapshot_priority(
        {"category": "exhibition", "source_confidence": "official"},
        "unknown",
    ) is None


def test_venue_aliases_share_the_same_snapshot_key() -> None:
    assert _venue_key("上海新国际博览中心") == _venue_key("新国际博览中心（浦东新区）")


def test_recent_reminder_hides_long_unrelated_events() -> None:
    lines = _recent_event_lines(
        {
            "events": [
                {
                    "event_name": "边缘形象艺术展",
                    "event_start_date": date(2026, 8, 15),
                    "event_end_date": date(2026, 12, 13),
                    "event_phase": "active",
                    "category": "exhibition",
                    "hotel_impact_level": "unknown",
                },
                {
                    "event_name": "短期设计展",
                    "event_start_date": date(2026, 9, 24),
                    "event_end_date": date(2026, 9, 29),
                    "event_phase": "active",
                    "category": "exhibition",
                    "hotel_impact_level": "medium",
                },
            ]
        },
        {
            "anchor_date": "2026-09-25",
            "events": [
                {
                    "event_name": "长期艺术展",
                    "event_start_date": date(2026, 9, 26),
                    "event_end_date": date(2027, 3, 2),
                    "display_venue": "艺仓美术馆",
                    "category": "exhibition",
                    "impact_level": "unknown",
                },
                {
                    "event_name": "20天设计展",
                    "event_start_date": date(2026, 9, 26),
                    "event_end_date": date(2026, 10, 15),
                    "display_venue": "设计馆",
                    "category": "exhibition",
                    "impact_level": "medium",
                },
                {
                    "event_name": "21天普通展览",
                    "event_start_date": date(2026, 9, 26),
                    "event_end_date": date(2026, 10, 16),
                    "display_venue": "普通展馆",
                    "category": "exhibition",
                    "impact_level": "medium",
                },
                {
                    "event_name": "长期电竞赛事",
                    "event_start_date": date(2026, 9, 26),
                    "event_end_date": date(2027, 3, 2),
                    "display_venue": "电竞馆",
                    "category": "esports",
                    "impact_level": "high",
                },
            ],
            "demand_signals": [],
            "price_control_advice": [],
        },
    )

    reminder = "\n".join(lines)
    assert "边缘形象艺术展" not in reminder
    assert "长期艺术展" not in reminder
    assert "20天设计展" in reminder
    assert "21天普通展览" not in reminder
    assert "短期设计展" in reminder
    assert "长期电竞赛事" in reminder
