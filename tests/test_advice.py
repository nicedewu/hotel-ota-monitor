from src.advice import summarize_competitor_price_for_business_snapshot
from src.business_snapshot import _business_compare_details, _price_summary_lines, _recommendations
from src.price_decision import _decision_values


def _decision(category: str, level: str, own_price: float, competitor_price: float) -> dict:
    return {
        "room_category": category,
        "own_current_price": own_price,
        "competitor_min_price": competitor_price,
        "raw": {
            "match_level": level,
            "own_price_source": {"rows": [{"room_name": f"我方{category}", "price": own_price}]},
            "competitor_min_source": {"hotel_name": "竞品", "room_name": f"竞品{category}"},
        },
    }


def test_business_snapshot_excludes_benchmark_from_price_conclusions() -> None:
    summary = summarize_competitor_price_for_business_snapshot(
        {
            "decisions": [
                _decision("esports_hexa", "benchmark", 769, 293),
                _decision("esports_double", "exact", 419, 315),
            ],
            "comparisons": [],
            "own_prices": [],
        }
    )

    assert summary["has_price_data"]
    assert [item["normalized_room_type"] for item in summary["comparisons"]] == ["esports_double"]
    assert all(item["competitor_price"] != 293 for item in summary["comparisons"])


def test_business_snapshot_uses_current_comparison_when_legacy_decision_raw_lacks_match_level() -> None:
    summary = summarize_competitor_price_for_business_snapshot(
        {
            "decisions": [{
                "room_category": "esports_double",
                "own_current_price": 695,
                "competitor_min_price": 608,
                "raw": {},
            }],
            "comparisons": [{
                "room_category": "esports_double",
                "match_level": "exact",
                "own_room_name": "电竞双人房",
                "own_price": 695,
                "competitor_min_price": 608,
            }],
            "own_prices": [],
        }
    )

    assert summary["has_price_data"]
    assert summary["comparisons"][0]["competitor_price"] == 608


def test_decision_jsonb_raw_preserves_variant_match_level() -> None:
    values = _decision_values({
        "run_id": "run", "decision_at": "2026-07-25T15:00:00+08:00", "hotel_key": "own",
        "stay_date": "2026-07-25", "room_category": "esports_double", "core_feature_signature": None,
        "occupancy_rate": 0.5, "own_current_price": 695, "competitor_min_price": 608,
        "recommended_price": 695, "recommended_action": "维持", "confidence": 0.8, "reason": "test",
        "match_level": "exact", "variant_key": "esports_double|occupancy=2", "raw": {"existing": True},
    })

    assert values[-1].obj["match_level"] == "exact"
    assert values[-1].obj["variant_key"] == "esports_double|occupancy=2"


def test_business_snapshot_keeps_variant_room_names_and_excludes_reference_only_rows() -> None:
    bathtub_key = "esports_double|bathtub=true"
    standard_key = "esports_double|bathtub=false"
    details = _business_compare_details(
        {
            "summary_by_room_type": [
                {
                    "stay_date": "2026-07-26",
                    "normalized_room_type": "esports_double",
                    "variant_key": bathtub_key,
                    "own_room_name": "电竞浴缸大床房",
                    "own_min_price": 428,
                    "match_level": "comparable",
                    "can_drive_pricing": False,
                },
                {
                    "stay_date": "2026-07-26",
                    "normalized_room_type": "esports_double",
                    "variant_key": standard_key,
                    "own_room_name": "雅致电竞大床房",
                    "own_min_price": 297,
                    "competitor_min_price": 242,
                    "match_level": "exact",
                    "can_drive_pricing": True,
                },
            ]
        },
        ["2026-07-26"],
    )
    summary = summarize_competitor_price_for_business_snapshot(
        {
            "decisions": [
                {
                    "room_category": "esports_double",
                    "own_current_price": 428,
                    "competitor_min_price": None,
                    "raw": {"match_level": "comparable", "variant_key": bathtub_key, "can_drive_pricing": False},
                },
                {
                    "room_category": "esports_double",
                    "own_current_price": 297,
                    "competitor_min_price": 242,
                    "raw": {"match_level": "exact", "variant_key": standard_key, "can_drive_pricing": True},
                },
            ],
            "comparisons": details,
            "own_prices": [
                {"room_category": "bathtub_double", "room_name": "电竞浴缸大床房", "price": 428, "collected_at": "2026-07-26T11:10:00+08:00"},
                {"room_category": "esports_double", "room_name": "雅致电竞大床房", "price": 297, "collected_at": "2026-07-26T11:10:00+08:00"},
            ],
        }
    )

    assert [(row["variant_key"], row["own_room_name"]) for row in details] == [
        (bathtub_key, "电竞浴缸大床房"),
        (standard_key, "雅致电竞大床房"),
    ]
    assert [(row["own_room_name"], row["own_price"]) for row in summary["comparisons"]] == [
        ("雅致电竞大床房", 297),
    ]

def test_recommendations_accepts_comparison_without_competitor_price() -> None:
    rows = _recommendations(
        [{
            "stay_date": "2026-07-28",
            "room_category": "esports_double",
            "own_room_name": "电竞浴缸大床房",
            "own_price": 399,
            "competitor_min_price": None,
        }],
        {"actual_sales_rate": None, "occupancy_rate": None, "available_rooms": None, "pending_arrival_rooms": None},
        [],
        [],
    )

    assert len(rows) == 1
    assert rows[0]["action"] == "维持"
    assert rows[0]["competitor_reference"]["price"] is None


def test_reference_price_is_labeled_without_becoming_a_price_conclusion() -> None:
    summary = summarize_competitor_price_for_business_snapshot({
        "decisions": [],
        "comparisons": [],
        "summary_by_room_type": [{
            "variant_key": "esports_double|gpu=5060",
            "own_room_name": "我方电竞大床房",
            "own_min_price": 500,
            "match_level": "comparable",
            "can_drive_pricing": False,
            "comparison_role": "hardware_mismatch_reference",
            "room_difference_note": "显卡：我方5060/竞品4060",
            "selected_match": {"hotel_name": "竞品", "room_name": "竞品电竞大床房", "price": 420},
        }],
        "own_prices": [],
    })

    assert summary["has_price_data"] is False
    assert summary["comparisons"] == []
    assert summary["reference_comparisons"][0]["room_difference_note"] == "显卡：我方5060/竞品4060"
    assert "【参考对比｜共1项，不驱动调价】" in "\n".join(_price_summary_lines(summary))


def test_reference_price_display_separates_own_competitor_and_difference() -> None:
    summary = {
        "reference_comparisons": [
            {
                "own_room_name": "奢华浴缸电竞大床房",
                "own_price": 335,
                "competitor_room_name": "突围丨护航丨大床房",
                "competitor_price": 338,
                "room_difference_note": "浴缸：我方有/竞品未注明；显卡：我方4070/竞品5070",
            },
            {
                "own_room_name": "电竞五人棋牌房",
                "own_price": 493,
                "competitor_room_name": "战队丨竞技丨五人套房",
                "competitor_price": 851,
                "room_difference_note": "棋牌：我方有/竞品未注明；房型结构：我方标准/竞品套房；显卡：我方4070TI/竞品5070",
            },
        ],
    }

    assert _price_summary_lines(summary) == [
        "【直接可比｜共0项】",
        "暂无直接可比数据",
        "",
        "【参考对比｜共2项，不驱动调价】",
        "1. 我方：奢华浴缸电竞大床房｜¥335",
        "   竞品：突围丨护航丨大床房｜¥338｜差异：浴缸：我方有/竞品未注明；显卡：我方4070/竞品5070",
        "",
        "2. 我方：电竞五人棋牌房｜¥493",
        "   竞品：战队丨竞技丨五人套房｜¥851｜差异：棋牌：我方有/竞品未注明；房型结构：我方标准/竞品套房；显卡：我方4070TI/竞品5070",
    ]


def test_today_reference_summary_input_remains_visible_to_business_snapshot() -> None:
    price_compare = {
        "summary_by_room_type": [{
            "variant_key": "esports_double|gpu=5070",
            "own_room_name": "我方电竞大床房",
            "own_min_price": 699,
            "match_level": "comparable",
            "can_drive_pricing": False,
            "comparison_role": "hardware_mismatch_reference",
            "room_difference_note": "显卡：我方5070/竞品4070",
            "selected_match": {"hotel_name": "竞品", "room_name": "竞品电竞大床房", "price": 599},
        }],
    }
    summary = summarize_competitor_price_for_business_snapshot({
        "decisions": [], "comparisons": [],
        "summary_by_room_type": price_compare["summary_by_room_type"], "own_prices": [],
    })

    assert summary["has_price_data"] is False
    assert [(item["own_room_name"], item["competitor_price"]) for item in summary["reference_comparisons"]] == [("我方电竞大床房", 599)]


def test_reference_price_excludes_cross_occupancy_market_benchmark() -> None:
    summary = summarize_competitor_price_for_business_snapshot({
        "decisions": [], "comparisons": [],
        "summary_by_room_type": [{
            "variant_key": "esports_six|gpu=5070",
            "own_room_name": "我方电竞六人房", "own_min_price": 1200,
            "match_level": "benchmark", "can_drive_pricing": False,
            "selected_match": {"room_name": "竞品电竞单人房", "price": 400},
        }], "own_prices": [],
    })

    assert summary["reference_comparisons"] == []


def test_business_advice_omits_no_direct_comparison_message() -> None:
    from src.advice import generate_business_snapshot_advice

    advice = generate_business_snapshot_advice({"actual_sales_rate": None, "occupancy_rate": None, "total_available": None, "room_type_inventory": []}, {"has_price_data": False, "comparisons": []})
    assert all("没有可直接用于竞价判断" not in item for item in advice)
