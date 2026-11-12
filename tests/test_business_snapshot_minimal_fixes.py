from types import SimpleNamespace

from src import business_snapshot
from src.business_snapshot import (
    _business_advice_lines,
    _competitor_room_supply_alerts,
    _competitor_supply_alert_lines,
    _evening_warning_summary,
    _exhibition_lines,
    _fetch_latest_decisions,
    _fetch_prices,
    _inventory_attention_lines,
    _pms_change_lines,
    _pms_change_summary,
    _price_summary_lines,
    _recommendations,
    render_feishu_message,
)
from src.multi_hotel import _build_business_snapshot_report
from src.price_decision import _build_decision


class FakeCursor:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.calls = []

    def execute(self, query, params):
        self.calls.append((query, params))

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_corroborated_event_name_is_labeled_as_pending_official_verification() -> None:
    text = business_snapshot._event_names_text([
        {"event_name": "大型赛事", "source_confidence": "corroborated"}
    ])

    assert text == "大型赛事（多源证实，待官方核验）"


def test_today_event_lines_show_date_venue_and_hotel_impact() -> None:
    lines = business_snapshot._today_event_lines([
        {
            "event_name": "2026无畏契约全球冠军赛",
            "event_start_date": "2026-09-24",
            "event_end_date": "2026-10-18",
            "venue": "静安体育中心 / 梅赛德斯-奔驰文化中心",
            "hotel_impact_level": "high",
            "effective_event_level": "normal",
        }
    ])

    assert lines == [
        "今天：",
        "• 9月24日—10月18日｜静安体育中心 / 梅赛德斯-奔驰文化中心｜本店影响较大",
        "  活动：2026无畏契约全球冠军赛",
    ]


def _supply_row(snapshot, hotel_key, room_name, room_category, available, price=None):
    return {
        "snapshot": snapshot,
        "hotel_key": hotel_key,
        "hotel_name": "我方酒店" if hotel_key == "own" else "竞品酒店",
        "stay_date": "2026-08-31",
        "room_name": room_name,
        "room_category": room_category,
        "available": available,
        "price": price,
    }


def test_competitor_supply_alert_requires_two_explicit_unavailable_batches() -> None:
    rows = [
        _supply_row("current", "own", "电竞双人房 有窗【5060】", "esports_double", True, 500),
        _supply_row("current", "competitor", "电竞双人房 有窗【5060】", "esports_double", False),
        _supply_row("current", "competitor", "电竞三人房 有窗【5060】", "esports_triple", True, 600),
        _supply_row("previous", "competitor", "电竞双人房 有窗【5060】", "esports_double", False),
        _supply_row("previous", "competitor", "电竞三人房 有窗【5060】", "esports_triple", True, 600),
    ]

    alerts = _competitor_room_supply_alerts(
        rows,
        "own",
        [SimpleNamespace(key="competitor", name="竞品酒店")],
        "2026-08-31",
    )

    assert len(alerts) == 1
    assert alerts[0]["unavailable_confirmations"] == 2
    assert "连续两批明确不可售" in _competitor_supply_alert_lines(alerts)[0]
    assert "¥" not in _competitor_supply_alert_lines(alerts)[0]


def test_competitor_supply_alert_rejects_unknown_or_another_direct_available_room() -> None:
    rows = [
        _supply_row("current", "own", "电竞双人房 有窗【5060】", "esports_double", True, 500),
        _supply_row("current", "competitor", "电竞双人房 有窗【5060】", "esports_double", None),
        _supply_row("current", "competitor", "电竞三人房 有窗【5060】", "esports_triple", True, 600),
        _supply_row("previous", "competitor", "电竞双人房 有窗【5060】", "esports_double", False),
        _supply_row("previous", "competitor", "电竞三人房 有窗【5060】", "esports_triple", True, 600),
    ]

    assert _competitor_room_supply_alerts(
        rows,
        "own",
        [SimpleNamespace(key="competitor", name="竞品酒店")],
        "2026-08-31",
    ) == []


def test_branch_message_keeps_rule_judgement_and_ignores_unsafe_hermes_actions() -> None:
    report = {
        "metadata": {"slot": "afternoon", "snapshot_date": "2026-08-08"},
        "hotels": {"own": [{"hotel_name": "测试酒店"}]},
        "pms_summary": {
            "occupancy_rate": 0.37,
            "actual_sales_rate": 0.45,
            "available_rooms": 21,
            "occupied_rooms": 14,
            "actual_sold_rooms": 17,
            "pending_arrival_rooms": 3,
        },
        "pms_room_type_details": [
            {"roomType": "电竞大床房", "available": 0},
            {"roomType": "电竞双床房", "available": 2},
            {"roomType": "电竞三人间", "available": 5},
        ],
        "recommendations": [{
            "room_category": "电竞大床房",
            "action": "小降",
            "suggested_adjustment": 390.45,
            "reason": "本批次结构化决策",
            "risk": "调价后观察转化",
            "manual_review": True,
            "confidence": "low",
            "own_price": 420,
            "competitor_price": 390,
            "has_direct_comparison": True,
        }],
        "business_advice": ["重点房型库存偏紧，优先检查对应 OTA 价格。"],
        "hermes_analysis": "已售17另有3待入住，共20间并建议维持。",
        "analysis_source": "hermes",
    }

    message = render_feishu_message(report)

    assert "小降（人工确认）｜试探价 ¥390.45" in message
    assert "【今日动作】" in message
    assert "⚠️ 以下为低置信度试探价，需人工确认：调价后观察转化" in message
    assert "二、房型库存摘要" not in message
    assert "二、竞品价格对比" in message
    assert "三、近期展会与经营提醒" in message
    assert "当前酒店：测试酒店" not in message
    assert "四、经营建议" in message
    assert "库存结构：1个房型售罄，1个房型仅余1–2间" in message
    assert "电竞大床房售罄" not in message
    assert "已售17另有3待入住" not in message
    assert "重点房型库存偏紧，优先检查对应 OTA 价格。" not in message
    assert "结构化价格建议（完整明细）" not in message


def test_branch_message_shows_hold_decision_and_rule_fallback_is_not_duplicated() -> None:
    report = {
        "metadata": {"slot": "morning", "snapshot_date": "2026-08-08"},
        "hotels": {"own": [{"hotel_name": "测试酒店"}]},
        "pms_summary": {
            "occupancy_rate": 0.37,
            "actual_sales_rate": 0.45,
            "available_rooms": 21,
            "occupied_rooms": 14,
            "actual_sold_rooms": 17,
            "pending_arrival_rooms": 3,
        },
        "recommendations": [
            {
                "room_category": "电竞大床房",
                "action": "维持",
                "suggested_adjustment": 399,
                "reason": "继续观察",
                "risk": "暂无",
                "has_direct_comparison": True,
                "own_price": 399,
                "competitor_price": 395,
            },
            {
                "room_category": "电竞双床房",
                "action": "保持价格",
                "suggested_adjustment": 429,
                "reason": "继续观察",
                "risk": "暂无",
                "has_direct_comparison": True,
                "own_price": 429,
                "competitor_price": 425,
            },
        ],
        "business_advice": ["可售库存偏紧，优先关注热门房型。"],
        "hermes_analysis": "部分房型库存偏紧，竞品价格整体接近我方。",
        "analysis_source": "hermes",
    }

    message = render_feishu_message(report)

    assert "部分房型库存偏紧，竞品价格整体接近我方。" in message
    assert "可售库存偏紧，优先关注热门房型。" not in message
    assert "电竞大床房｜维持｜维持价 ¥399" in message
    assert "电竞双床房｜保持价格｜维持价 ¥429" in message
    assert "【今日动作】" in message
    assert "【判断依据】" in message
    assert "【价格异常提醒】" not in message
    assert "低置信度试探价" not in message


def test_branch_message_uses_rule_advice_when_hermes_falls_back() -> None:
    report = {
        "metadata": {"slot": "afternoon", "snapshot_date": "2026-09-05"},
        "hotels": {"own": [{"hotel_name": "测试酒店"}]},
        "business_advice": [
            "库存偏紧，优先检查热门房型。",
            "检查低库存房型的 OTA 可订状态。",
            "展会参考：测试展会；仅作背景信息。",
        ],
        "hermes_analysis": "系统深度分析暂不可用，已使用规则建议。",
        "analysis_source": "rules",
        "recommendations": [],
    }

    message = render_feishu_message(report)

    assert "四、经营建议" in message
    assert "【今日动作】" in message
    assert "1. 库存偏紧，优先检查热门房型。" in message
    assert "2. 检查低库存房型的 OTA 可订状态。" in message
    assert "展会参考：测试展会" not in message
    assert "【判断依据】" not in message


def test_inventory_attention_summarizes_current_room_type_pressure() -> None:
    lines = _inventory_attention_lines({
        "pms_room_type_details": [
            {"roomType": "已售罄房型", "available": 0},
            {"roomType": "紧张房型", "available": 2},
            {"roomType": "充足房型", "available": 6},
        ],
        "pms_change": {
            "room_type_changes": [
                {"room_type": "下降房型", "available_delta": -3},
                {"room_type": "紧张房型", "available_delta": -4},
            ],
        },
    })

    assert lines == ["库存结构：1个房型售罄，1个房型仅余1–2间"]


def test_inventory_attention_reports_counts_without_overflow() -> None:
    lines = _inventory_attention_lines({
        "pms_room_type_details": [
            {"roomType": f"房型{index}", "available": 0}
            for index in range(1, 7)
        ],
    })

    assert lines == ["库存结构：6个房型售罄"]


def test_inventory_attention_ignores_previous_delta_without_current_pressure() -> None:
    lines = _inventory_attention_lines({
        "pms_room_type_details": [{"roomType": "下降但仍充足", "available": 6}],
        "pms_change": {"room_type_changes": [{"room_type": "下降但仍充足", "available_delta": -3}]},
    })

    assert lines == []


def test_pms_change_uses_previous_snapshot_and_room_type_deltas() -> None:
    change = _pms_change_summary(
        {
            "actual_sold_rooms": 19,
            "pending_arrival_rooms": 4,
            "available_rooms": 19,
            "actual_sales_rate": 0.50,
            "collected_at": "2026-08-08T15:00:00+08:00",
        },
        {
            "actual_sold_rooms": 17,
            "pending_arrival_rooms": 3,
            "available_rooms": 21,
            "actual_sales_rate": 0.4474,
            "collected_at": "2026-08-08T11:10:00+08:00",
        },
        [
            {"roomTypeID": "1", "roomType": "电竞大床房", "available": 6},
            {"roomTypeID": "2", "roomType": "电竞双床房", "available": 5},
        ],
        [
            {"roomTypeID": "1", "roomType": "电竞大床房", "available": 8},
            {"roomTypeID": "2", "roomType": "电竞双床房", "available": 5},
        ],
    )

    assert change["actual_sold_rooms_delta"] == 2
    assert change["pending_arrival_rooms_delta"] == 1
    assert change["available_rooms_delta"] == -2
    assert change["actual_sales_rate_delta_pp"] == 5.26
    assert change["room_type_changes"] == [{
        "room_type": "电竞大床房",
        "previous_available": 8.0,
        "current_available": 6.0,
        "available_delta": -2.0,
    }]
    assert change["all_room_type_changes"] == change["room_type_changes"]


def test_price_action_shows_room_inventory_change_and_decision_evidence() -> None:
    report = {
        "recommendations": [{
            "room_name": "电竞大床房",
            "action": "小降",
            "suggested_adjustment": 475,
            "reason": "库存充足且直接竞品更低",
            "risk": "库存变化仅作估算，不代表真实订单销量",
            "confidence": "low",
            "manual_review": True,
            "inventory_change": {
                "previous_available": 6,
                "current_available": 4,
                "available_delta": -2,
            },
            "own_price": 500,
            "competitor_price": 480,
            "has_direct_comparison": True,
        }],
        "business_advice": [],
        "analysis_source": "rules",
    }

    message = render_feishu_message(report)

    assert "库存：6→4间（净减少2间，库存变化估算）" in message
    assert "我方/直接竞品：¥500/¥480" in message
    assert "⚠️ 以下为低置信度试探价，需人工确认：库存变化仅作估算，不代表真实订单销量" in message
    assert "风险：" not in message
    assert "可信度：低" not in message


def test_price_action_hides_manual_placeholders_and_keeps_direct_trial_price() -> None:
    report = {
        "recommendations": [
            {
                "room_name": "无可比房型",
                "action": "人工确认",
                "suggested_adjustment": None,
                "reason": "缺少直接可比房型，不输出具体调价",
                "risk": "库存变化仅作估算，不代表真实订单销量",
                "confidence": "low",
                "manual_review": True,
                "has_direct_comparison": False,
            },
            {
                "room_name": "可比房型",
                "action": "小降",
                "suggested_adjustment": 435.1,
                "reason": "当前价高于竞品，已收敛为5%试探性降幅",
                "risk": "库存变化仅作估算，不代表真实订单销量",
                "confidence": "low",
                "manual_review": True,
                "own_price": 458,
                "competitor_price": 378,
                "has_direct_comparison": True,
            },
        ],
        "business_advice": [],
        "analysis_source": "rules",
    }

    message = render_feishu_message(report)

    assert "无可比房型" not in message
    assert "可比房型｜小降（人工确认）｜试探价 ¥435.10" in message
    assert message.count("库存变化仅作估算，不代表真实订单销量") == 1


def test_price_reduction_is_capped_at_five_percent() -> None:
    decision = _build_decision(
        stay_date="2026-08-09",
        room_category="esports_double",
        occupancy_rate=0.2,
        actual_sales_rate=None,
        actual_sold_rooms=None,
        pending_arrival_rooms=None,
        available_rooms=30,
        decision_rate=0.2,
        decision_rate_source="inventory_estimate",
        own_current_price=500,
        competitor_min_price=300,
    )

    assert decision["action"] == "小降"
    assert decision["discount_pct"] == 5
    assert decision["suggested_price"] == 475
    assert "原始降幅" in decision["reason"]
    assert "5%试探性降幅" in decision["reason"]


def test_existing_price_decision_is_also_capped_when_rendered() -> None:
    rows = _recommendations(
        [],
        {},
        [{
            "room_category": "esports_double",
            "own_current_price": 500,
            "competitor_min_price": 300,
            "recommended_action": "强降",
            "recommended_price": 375,
            "reason": "旧建议",
            "confidence": "low",
            "can_drive_pricing": True,
            "risk_note": "库存变化仅作估算",
        }],
        [],
    )

    assert rows[0]["action"] == "小降"
    assert rows[0]["suggested_adjustment"] == 475
    assert "5%试探性降幅" in rows[0]["reason"]


def test_evening_warning_is_limited_to_30_minutes_and_deduplicated(monkeypatch) -> None:
    cursor = FakeCursor([
        {"message": "同一房型库存下降"},
        {"message": "总库存下降"},
    ])

    class Connection:
        def cursor(self):
            return cursor

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Psycopg:
        rows = type("Rows", (), {"dict_row": object()})

        @staticmethod
        def connect(*args, **kwargs):
            return Connection()

    monkeypatch.setattr(business_snapshot, "psycopg", Psycopg)
    monkeypatch.setattr(business_snapshot, "_load_database_url", lambda: "unused")

    summary = _evening_warning_summary("own", "2026-08-08")

    assert summary == "同一房型库存下降；总库存下降"
    assert "INTERVAL '30 minutes'" in cursor.calls[0][0]
    assert "GROUP BY message" in cursor.calls[0][0]
    assert "NOT IN ('recovered', 'stable', 'filtered', 'suppressed', 'confirmed_no_push')" in cursor.calls[0][0]


def test_explicit_run_id_filters_prices_and_decisions() -> None:
    price_cursor = FakeCursor([{"hotel_key": "own", "price": 399}])
    decision_cursor = FakeCursor([{"run_id": "run-123", "room_category": "esports_double"}])

    prices = _fetch_prices(price_cursor, ["own"], ["2026-08-08"], "run-123")
    decisions = _fetch_latest_decisions(decision_cursor, ["own"], ["2026-08-08"], "run-123")

    assert prices[0]["price"] == 399
    assert "run_id = %s" in price_cursor.calls[0][0]
    assert price_cursor.calls[0][1][-1] == "run-123"
    assert decisions[0]["run_id"] == "run-123"
    assert len(decision_cursor.calls) == 1
    assert decision_cursor.calls[0][1][0] == "run-123"


def test_scheduled_report_forwards_job_run_id(tmp_path, monkeypatch) -> None:
    captured = {}

    def fake_build(**kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(business_snapshot, "build_business_snapshot_report", fake_build)

    _build_business_snapshot_report(
        "own",
        "2026-08-08",
        1,
        "config/hotels.json",
        publish_latest=False,
        run_id="run-123",
    )

    assert captured["run_id"] == "run-123"


def test_message_template_lists_every_exhibition_with_hotel_impact() -> None:
    lines = _exhibition_lines({
        "message": "今日进行中展会：有",
        "hotel_rule_available": True,
        "events": [
            {
                "event_name": "展会一",
                "venue": "上海新国际博览中心",
                "event_start_date": "2026-08-12",
                "event_end_date": "2026-08-14",
                "event_phase": "active",
                "global_event_demand_level": "medium",
                "hotel_impact_level": "low",
                "effective_event_level": "normal",
            },
            {
                "event_name": "展会二",
                "venue": "上海新国际博览中心",
                "event_start_date": "2026-08-12",
                "event_end_date": "2026-08-14",
                "event_phase": "active",
                "global_event_demand_level": "high",
                "hotel_impact_level": "high",
                "effective_event_level": "high",
            },
        ],
    })

    message = "\n".join(lines)

    assert "共2场｜涉及1个展馆" in message
    assert "【上海新国际博览中心｜2026-08-12 至 2026-08-14｜进行中｜2场】" in message
    assert "影响汇总：本店高1场、低1场｜最终高1场、常规1场" in message
    assert "1. 展会一" in message
    assert "2. 展会二" in message
    assert "本店低｜全局中｜最终常规" in message
    assert "本店高｜全局高｜最终高" in message
    assert message.count("经营口径：可作为今日需求背景，不单独触发调价。") == 1


def test_price_sections_display_all_direct_and_reference_comparisons() -> None:
    comparisons = [
        {
            "own_room_name": f"直接{i}",
            "own_price": 400 + i,
            "competitor_price": 390 + i,
            "price_diff": 10,
            "judgement": "我方价格偏高",
            "advice": "观察转化",
        }
        for i in range(1, 7)
    ]
    references = [
        {
            "own_room_name": f"参考我方{i}",
            "own_price": 500 + i,
            "competitor_room_name": f"参考竞品{i}",
            "competitor_price": 480 + i,
            "room_difference_note": "配置不同",
        }
        for i in range(1, 5)
    ]

    message = "\n".join(_price_summary_lines({"comparisons": comparisons, "reference_comparisons": references}))

    assert "【直接可比｜共6项】" in message
    assert "直接6" in message
    assert "【参考对比｜共4项，不驱动调价】" in message
    assert "参考我方4" in message
    assert "竞品：参考竞品4｜¥484｜差异：配置不同" in message


def test_message_avoids_repeating_exhibition_rules_after_exhibition_details() -> None:
    report = {
        "hotels": {"own": [{"hotel_name": "测试酒店"}]},
        "exhibition_context": {"events": [{"event_name": "展会一"}]},
        "business_advice": ["展会参考：展会一；全局需求中、本店影响高、最终影响高。"],
        "analysis_source": "rules",
    }

    message = render_feishu_message(report)

    assert "展会参考：展会一" not in message


def test_business_judgement_hides_redundant_exhibition_and_missing_arrival_lines() -> None:
    report = {
        "analysis_source": "hermes",
        "hermes_analysis": "\n".join([
            "1. 高影响展会信号尚未充分转化为既定订单，晚间到店转化仍是主要增量窗口。",
            "2. 库存售卖率估算高于当前在住率，部分售出订单尚未到店兑现。",
            "3. 待入住数据缺失，无法推断后续进店数量。",
            "4. 参考数据不具备同类可比性，不形成竞品价差判断。",
            "5. 销售进度与剩余承接空间呈背离，需结合后续转化判断。",
            "6. 竞品对照存在配置差异与信息缺口，今日竞品价差不作强弱结论依据。",
        ]),
        "business_advice": [],
        "recommendations": [],
    }

    message = "\n".join(_business_advice_lines(report))

    assert "1. 高影响展会信号尚未充分转化为既定订单，晚间到店转化仍是主要增量窗口。" in message
    assert "2. 库存售卖率估算高于当前在住率，部分售出订单尚未到店兑现。" in message
    assert "3. 销售进度与剩余承接空间呈背离，需结合后续转化判断。" in message
    assert "参考数据不具备同类可比性" not in message
    assert "竞品对照存在配置差异" not in message
    assert "待入住数据缺失" not in message


def test_message_compacts_overview_hides_missing_arrivals_and_uses_generated_time() -> None:
    report = {
        "metadata": {"slot": "morning", "snapshot_date": "2026-08-12", "generated_at": "2026-08-12T11:19:21+08:00"},
        "hotels": {"own": [{"hotel_name": "测试酒店"}]},
        "pms_summary": {
            "occupancy_rate": 0.5,
            "actual_sales_rate": 0.4,
            "available_rooms": 12,
            "occupied_rooms": 20,
            "actual_sold_rooms": 8,
            "pending_arrival_rooms": None,
        },
        "pms_change": {
            "actual_sold_rooms_delta": 6,
            "pending_arrival_rooms_delta": -5,
            "available_rooms_delta": -6,
            "actual_sales_rate_delta_pp": 10.53,
            "sales_rate_label": "库存售卖率估算",
            "room_type_changes": [{
                "room_type": "豪华电竞大床",
                "previous_available": 7.0,
                "current_available": 4.0,
                "available_delta": -3,
            }],
        },
        "exhibition_context": {},
        "business_advice": [],
        "recommendations": [],
    }

    message = render_feishu_message(report)

    assert "📅 2026-08-12 11:19" in message
    assert "在住：20间｜在住率：50.00%" in message
    assert "销售：库存估算已售房8间｜销售率：40.00%" in message
    assert "库存：可售12间｜相对充足" in message
    assert "未入住预订" not in message
    assert "较上次" not in message
    assert "库存售卖率估算 +10.53 个百分点" not in message
    assert "豪华电竞大床：可售 7.0→4.0 间" not in message


def test_pms_change_hides_missing_metric_deltas() -> None:
    lines = _pms_change_lines({
        "actual_sold_rooms_delta": 2,
        "pending_arrival_rooms_delta": None,
        "available_rooms_delta": -2,
        "actual_sales_rate_delta_pp": None,
    })

    assert lines == ["较上次：已售 +2 间；可售 -2 间"]
