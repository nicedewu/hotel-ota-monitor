from decimal import Decimal

from src.compare import normalize_room_type
from src.multi_hotel import _comparison_variant_key, _json_safe, _room_profile, build_variant_key, match_competitor_candidates, resolve_room_type_mapping
from src.price_decision import _build_decision_room_variants, _build_stay_decisions


def _room(room_type: str, name: str, price: float, hotel_key: str = "competitor") -> dict:
    return {"normalized_room_type": room_type, "room_name": name, "price": price, "hotel_key": hotel_key}


def test_match_levels_follow_capacity_before_name() -> None:
    own = {
        "esports_double": _room("esports_double", "我方电竞大床房 有窗【4060+2K240】", 500, "own"),
        "big_bed": _room("big_bed", "我方商务大床房 有窗", 450, "own"),
        "esports_penta": _room("esports_penta", "我方电竞五人房 有窗【4060】", 800, "own"),
    }
    competitors = [
        _room("esports_double", "竞品电竞大床房 有窗【4060+2K240】", 420),
        _room("twin", "竞品双床房 有窗", 390),
        _room("esports_triple", "竞品电竞三人房 有窗【4060】", 520),
    ]

    matches = match_competitor_candidates(own, competitors)

    assert matches["esports_double"]["match_level"] == "exact"
    assert matches["esports_double"]["competitor_min_price"] == 420
    assert matches["big_bed"]["match_level"] == "comparable"
    assert matches["big_bed"]["can_drive_pricing"] is False
    assert matches["big_bed"]["selected_match"]["normalized_room_type"] == "esports_double"
    assert "大床" in matches["big_bed"]["selected_match"]["room_name"]
    assert matches["esports_penta"]["match_level"] == "benchmark"


def test_none_requires_no_valid_competitor_price() -> None:
    own = {"esports_double": _room("esports_double", "我方电竞大床房", 500, "own")}
    assert match_competitor_candidates(own, [])["esports_double"]["match_level"] == "none"


def test_premium_bathtub_room_is_not_an_exact_or_pricing_peer() -> None:
    own = {"bath": _room("esports_double", "我方浴缸电竞大床房", 699, "own")}
    competitors = [_room("esports_double", "竞品普通电竞大床房", 399)]
    match = match_competitor_candidates(own, competitors)["bath"]
    assert match["match_level"] == "comparable"
    assert match["comparison_role"] == "premium_mismatch_reference"
    assert match["can_drive_pricing"] is False


def test_comparison_summary_key_keeps_same_name_variants_separate() -> None:
    ordinary = {"stay_date": "2026-07-23", "normalized_room_type": "esports_double", "core_feature_signature": "bathtub=false", "marker": "ordinary"}
    bathtub = {"stay_date": "2026-07-23", "normalized_room_type": "esports_double", "core_feature_signature": "bathtub=true", "marker": "bathtub"}
    summaries = {_comparison_variant_key(row): row for row in (ordinary, bathtub)}

    assert _comparison_variant_key(ordinary) != _comparison_variant_key(bathtub)
    assert summaries[_comparison_variant_key(ordinary)]["marker"] == "ordinary"
    assert summaries[_comparison_variant_key(bathtub)]["marker"] == "bathtub"


def test_hotel_mapping_attribute_overrides_win_and_reach_the_room_profile() -> None:
    mappings = [
        {"hotel_key": None, "match_keyword": "电竞大床", "normalized_room_type": "esports_double", "priority": 1, "attribute_overrides": {"has_bathtub": False}},
        {"hotel_key": "own", "match_keyword": "浴缸电竞大床", "normalized_room_type": "esports_double", "priority": 999, "attribute_overrides": {"has_bathtub": True}},
    ]

    mapping = resolve_room_type_mapping("浴缸电竞大床房", mappings)

    assert mapping is not None
    assert mapping["hotel_key"] == "own"
    assert mapping["attribute_overrides"] == {"has_bathtub": True}
    assert _room_profile("esports_double", {"room_name": "浴缸电竞大床房", "attribute_overrides": mapping["attribute_overrides"]})["has_bathtub"] is True


def test_none_match_has_complete_fail_closed_contract() -> None:
    own = {"variant": _room("esports_double", "我方电竞大床房", 500, "own")}
    match = match_competitor_candidates(own, [])["variant"]

    assert match["comparison_role"] == "unavailable"
    assert match["can_drive_pricing"] is False
    assert match["selected_match"] is None
    assert match["pricing_min_price"] is None
    assert match["market_min_price"] is None


def test_premium_reference_keeps_market_but_clears_direct_pricing_fields() -> None:
    own = {"bath": _room("esports_double", "我方浴缸电竞大床房", 699, "own")}
    match = match_competitor_candidates(own, [_room("esports_double", "竞品普通电竞大床房", 399)])["bath"]

    assert match["market_min_price"] == 399
    assert match["pricing_min_price"] is None
    assert match["competitor_min_price"] is None


def test_decision_variant_adapter_rejects_wrong_source_run() -> None:
    variant = {"stay_date": "2026-07-23", "normalized_room_type": "esports_double", "core_feature_signature": "bathtub=true", "variant_key": build_variant_key("esports_double", "bathtub=true")}
    payload = {"own_hotel_key": "own", "source_run_id": "source-a", "summary_by_room_type": [variant]}

    rows, status = _build_decision_room_variants(payload, "own", "2026-07-23", "source-b")

    assert rows == []
    assert status == "batch_mismatch"


def test_comparison_raw_context_is_json_safe_for_database_decimals() -> None:
    assert _json_safe({"price": Decimal("399.50")}) == {"price": "399.50"}


def test_benchmark_never_drives_a_mechanical_price_action() -> None:
    decisions, skipped = _build_stay_decisions(
        stay_date="2026-07-16",
        own_hotel_key="own",
        competitor_hotel_keys={"competitor"},
        status=None,
        price_rows=[
            {"hotel_key": "own", "room_category": "esports_hexa", "room_name": "我方电竞六人房", "stay_date": "2026-07-16", "price": 900, "collected_at": "2026-07-16T08:00:00+08:00"},
            {"hotel_key": "competitor", "room_category": "esports_triple", "room_name": "竞品电竞三人房", "stay_date": "2026-07-16", "price": 500, "collected_at": "2026-07-16T08:00:00+08:00"},
        ],
        exhibitions=[],
        memory_context=[],
        router_status={},
    )

    assert not skipped
    assert decisions[0]["match_level"] == "benchmark"
    assert decisions[0]["competitor_min_price"] is None
    assert decisions[0]["decision_rule"] == "missing_competitor_keep"
    assert "不按此价机械跟价" in decisions[0]["reason"]


def test_hardware_window_and_bed_differences_are_reference_only() -> None:
    own = {"room": _room("esports_double", "我方电竞大床房 有窗【5060+2K300】", 500, "own")}
    gpu = match_competitor_candidates(own, [_room("esports_double", "竞品电竞大床房 有窗【4060+2K300】", 420)])["room"]
    assert gpu["match_level"] == "comparable"
    assert gpu["comparison_role"] == "hardware_mismatch_reference"
    assert gpu["can_drive_pricing"] is False
    assert "显卡：我方5060/竞品4060" in gpu["room_difference_note"]

    window = match_competitor_candidates(own, [_room("esports_double", "竞品电竞大床房 无窗【5060+2K300】", 420)])["room"]
    assert window["can_drive_pricing"] is False
    assert "窗型：我方有窗/竞品无窗" in window["room_difference_note"]

    bed = match_competitor_candidates(own, [_room("esports_double", "竞品电竞双人房 有窗【5060+2K300】", 420)])["room"]
    assert bed["match_level"] == "none"
    assert bed["comparison_role"] == "unavailable"
    assert bed["can_drive_pricing"] is False
    assert bed["competitor_min_price"] is None


def test_window_defaults_to_exterior_unless_explicitly_marked() -> None:
    assert _room_profile("esports_double", _room("esports_double", "电竞大床房【5060】", 500))["window_type"] == "window"
    assert _room_profile("esports_double", _room("esports_double", "电竞大床房 无窗【5060】", 500))["window_type"] == "no_window"
    assert _room_profile("esports_double", _room("esports_double", "电竞大床房 内窗【5060】", 500))["window_type"] == "inner_window"


def test_twin_bed_and_double_room_are_the_same_double_bed_product() -> None:
    own = {"room": _room("esports_double", "我方电竞双床房 有窗【5060】", 500, "own")}
    match = match_competitor_candidates(own, [_room("esports_double", "竞品电竞双人房 有窗【5060】", 420)])["room"]
    assert match["match_level"] == "exact"
    assert match["can_drive_pricing"] is True


def test_matching_gpu_with_missing_secondary_hardware_still_drives_pricing() -> None:
    own = {"room": _room("esports_penta", "我方电竞五人房 有窗【5070+9700X】", 1100, "own")}
    match = match_competitor_candidates(own, [_room("esports_penta", "竞品电竞五人房 有窗【5070】", 730)])["room"]

    assert match["match_level"] == "exact"
    assert match["can_drive_pricing"] is True
    assert "CPU" not in match["room_difference_note"]
    assert "床型未注明" not in match["room_difference_note"]


def test_secondary_hardware_is_ignored_when_gpu_matches() -> None:
    own = {"room": _room("esports_penta", "我方电竞五人房 有窗【5070+9700X】", 1100, "own")}
    match = match_competitor_candidates(own, [_room("esports_penta", "竞品电竞五人房 有窗【5070+I7-12700】", 730)])["room"]

    assert match["match_level"] == "exact"
    assert match["can_drive_pricing"] is True
    assert "CPU" not in match["room_difference_note"]


def test_reference_note_explains_mahjong_and_loft_product_differences() -> None:
    own = {"room": _room("esports_penta", "我方电竞Loft五人棋牌房 有窗【5070】", 1100, "own")}
    match = match_competitor_candidates(own, [_room("esports_penta", "竞品电竞五人房 有窗【5070】", 730)])["room"]

    assert match["can_drive_pricing"] is False
    assert "棋牌：我方有/竞品未注明" in match["room_difference_note"]
    assert "房型结构：我方Loft/竞品标准" in match["room_difference_note"]


def test_single_machine_big_bed_is_a_single_occupancy_product() -> None:
    name = "甄选单机大床房【5070+2K360】"

    assert normalize_room_type(name) == "esports_single"
    assert _room_profile("esports_single", _room("esports_single", name, 800))["occupancy"] == 1

    own = {"single_machine": _room("esports_single", name, 800, "own")}
    competitor = [_room("esports_double", "竞品电竞大床房【5070+2K360】", 700)]
    match = match_competitor_candidates(own, competitor)["single_machine"]

    assert match["match_level"] == "benchmark"
    assert match["can_drive_pricing"] is False
