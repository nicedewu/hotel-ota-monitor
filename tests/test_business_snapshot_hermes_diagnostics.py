import json
from subprocess import CompletedProcess, TimeoutExpired
from unittest import TestCase
from unittest.mock import patch

from src.business_snapshot import (
    BusinessSnapshotError,
    _business_advice_lines,
    _render_hermes_decision,
    _business_evidence,
    _parse_hermes_analysis_output,
    _pms_history_context,
    _previous_business_action_context,
    _summarize_hermes_stderr,
    generate_hermes_business_analysis,
)


class HermesAnalysisDiagnosticsTests(TestCase):
    def test_import_error_is_classified_without_leaking_secret(self) -> None:
        diagnostics: dict[str, object] = {}
        result = CompletedProcess(
            args=["hermes"],
            returncode=1,
            stdout="",
            stderr="ImportError: cannot import name 'x'; api_key=should-not-appear",
        )

        with (
            patch("src.business_snapshot.Path.is_file", return_value=True),
            patch("src.business_snapshot._hermes_version", return_value="0.18.2"),
            patch("src.business_snapshot.subprocess.run", return_value=result),
        ):
            with self.assertRaises(BusinessSnapshotError):
                generate_hermes_business_analysis(
                    {"exhibition_context": {}},
                    None,
                    diagnostics=diagnostics,
                )

        self.assertEqual(diagnostics["fallback_reason"], "hermes_cli_import_error")
        self.assertEqual(diagnostics["hermes_returncode"], 1)
        self.assertNotIn("should-not-appear", str(diagnostics["hermes_stderr_summary"]))

    def test_structured_output_is_rendered_without_exhibition_context(self) -> None:
        diagnostics: dict[str, object] = {}
        result = CompletedProcess(
            args=["hermes"],
            returncode=0,
            stdout=json.dumps({"actions": [{"kind": "review_price", "target": "comparison:0", "evidence": ["comparison:0"], "reason": "有效价差较大，需要先确认两家的取消权益", "recheck": "下批核对同日同权益挂牌价"}]}, ensure_ascii=False),
            stderr="",
        )

        with (
            patch("src.business_snapshot.Path.is_file", return_value=True),
            patch("src.business_snapshot._hermes_version", return_value="0.18.2"),
            patch("src.business_snapshot.subprocess.run", return_value=result) as runner,
        ):
            analysis = generate_hermes_business_analysis(
                {
                    "stay_date": "2026-07-30",
                    "exhibition_context": {"event_name": "不应进入模型输入"},
                    "business_snapshot": {"actual_sales_rate": 0.5, "sales_rate_source": "inventory_estimate_not_order_api"},
                    "analysis_context": {"current_period": "下午", "pms_change": {"actual_sold_rooms_delta": 2}},
                    "future_event_demand_context": {
                        "anchor_date": "2026-07-30",
                        "events": [{
                            "event_name": "测试活动",
                            "venue": "测试场馆",
                            "event_start_date": "2026-07-31",
                            "event_end_date": "2026-07-31",
                            "impact_level": "high",
                        }],
                        "demand_signals": [{
                            "stay_date": "2026-07-31",
                            "state": "观察",
                            "confirmed": False,
                            "primary_score": 99,
                        }],
                    },
                },
                {"comparisons": [{"own_room_name": "大床房", "own_price": 500, "competitor_price": 420}]},
                diagnostics=diagnostics,
            )

        self.assertEqual(analysis["actions"][0]["kind"], "review_price")
        self.assertGreater(runner.call_args.kwargs["timeout"], 299)
        self.assertLessEqual(runner.call_args.kwargs["timeout"], 300)
        self.assertIn("测试活动", runner.call_args.args[0][3])
        self.assertIsNone(diagnostics.get("fallback_reason"))
        self.assertNotIn("exhibition_context", runner.call_args.args[0][3])
        self.assertNotIn("不应进入模型输入", runner.call_args.args[0][3])
        self.assertIn("sales_rate_source", runner.call_args.args[0][3])
        self.assertIn("pms_change", runner.call_args.args[0][3])
        self.assertIn("evidence_catalog", runner.call_args.args[0][3])
        prompt = runner.call_args.args[0][3]
        self.assertIn("review_price 和 hold_price 只能使用有效 comparison", prompt)
        self.assertIn("prioritize_sales 使用 inventory", prompt)
        self.assertIn("trial_price 只能使用系统给出的 decision", prompt)
        self.assertIn("prepare_event 只能使用标记为可行动的 future_event", prompt)
        self.assertIn("缺少竞品只限制竞品驱动的价格动作", prompt)
        self.assertIn("所有数字都必须出现在该条 evidence 引用的证据文本中", prompt)
        self.assertNotIn("价格类 target 用 comparison、inventory 或 decision", prompt)
        self.assertIn('"future_event:0"', runner.call_args.args[0][3])
        self.assertNotIn("demand_signals", runner.call_args.args[0][3])
        self.assertNotIn("primary_score", runner.call_args.args[0][3])

    def test_schema_failure_records_specific_code_for_each_attempt(self) -> None:
        diagnostics: dict[str, object] = {}
        result = CompletedProcess(
            args=["hermes"],
            returncode=0,
            stdout=json.dumps({"actions": [{
                "kind": "review_price",
                "target": "inventory:0",
                "evidence": ["inventory:0"],
                "reason": "库存需要复查",
                "recheck": "下一批查看房态",
            }]}, ensure_ascii=False),
            stderr="",
        )
        snapshot = {
            "business_snapshot": {
                "room_type_inventory": [{"roomType": "大床房", "available": 2}],
            },
        }

        with (
            patch("src.business_snapshot.Path.is_file", return_value=True),
            patch("src.business_snapshot._hermes_version", return_value="0.18.2"),
            patch("src.business_snapshot.subprocess.run", return_value=result),
        ):
            with self.assertRaises(BusinessSnapshotError):
                generate_hermes_business_analysis(snapshot, None, diagnostics=diagnostics)

        self.assertEqual(diagnostics["fallback_reason"], "invalid_json_schema")
        self.assertEqual(diagnostics["hermes_failure_code"], "target_evidence_kind")
        self.assertEqual(diagnostics["hermes_failure_message"], "动作 target 的证据类型不允许")
        self.assertEqual(diagnostics["hermes_attempts"], 2)
        self.assertTrue(diagnostics["analysis_retry_used"])
        attempts = diagnostics["hermes_attempt_details"]
        self.assertEqual(len(attempts), 2)
        self.assertEqual([item["returncode"] for item in attempts], [0, 0])
        self.assertEqual([item["hermes_failure_code"] for item in attempts], ["target_evidence_kind", "target_evidence_kind"])
        self.assertTrue(all(item["stdout_chars"] > 0 for item in attempts))

    def test_retry_accepts_corrected_output_and_records_attempt_durations(self) -> None:
        diagnostics: dict[str, object] = {}
        first = CompletedProcess(
            args=["hermes"],
            returncode=0,
            stdout=json.dumps({"actions": [{
                "kind": "review_price",
                "target": "inventory:0",
                "evidence": ["inventory:0"],
                "reason": "库存需要复查",
                "recheck": "下一批查看房态",
            }]}, ensure_ascii=False),
            stderr="",
        )
        second = CompletedProcess(
            args=["hermes"],
            returncode=0,
            stdout=json.dumps({"actions": [{
                "kind": "review_price",
                "target": "comparison:0",
                "evidence": ["comparison:0"],
                "reason": "直接可比条件需要复核",
                "recheck": "下一批查看该房型挂牌价",
            }]}, ensure_ascii=False),
            stderr="",
        )
        snapshot = {
            "business_snapshot": {
                "room_type_inventory": [{"roomType": "大床房", "available": 2}],
            },
        }
        price_summary = {
            "comparisons": [{"own_room_name": "大床房", "own_price": 500, "competitor_price": 450}],
        }
        monotonic_values = [0.0, 0.0, 0.01, 0.21, 0.22, 0.22, 0.23, 0.53, 0.54]

        with (
            patch("src.business_snapshot.Path.is_file", return_value=True),
            patch("src.business_snapshot._hermes_version", return_value="0.18.2"),
            patch("src.business_snapshot.subprocess.run", side_effect=[first, second]),
            patch("src.business_snapshot.time.monotonic", side_effect=monotonic_values),
        ):
            analysis = generate_hermes_business_analysis(snapshot, price_summary, diagnostics=diagnostics)

        self.assertEqual(analysis["actions"][0]["target"], "comparison:0")
        self.assertIsNone(diagnostics["fallback_reason"])
        self.assertIsNone(diagnostics["hermes_failure_code"])
        self.assertTrue(diagnostics["analysis_retry_used"])
        self.assertEqual(diagnostics["hermes_attempts"], 2)
        attempts = diagnostics["hermes_attempt_details"]
        self.assertEqual([item["duration_ms"] for item in attempts], [210, 310])
        self.assertEqual(attempts[0]["hermes_failure_code"], "target_evidence_kind")
        self.assertNotIn("hermes_failure_code", attempts[1])

    def test_timeout_records_bounded_attempt_diagnostics(self) -> None:
        diagnostics: dict[str, object] = {}
        timeout = TimeoutExpired(cmd=["hermes"], timeout=300, output="", stderr="")

        with (
            patch("src.business_snapshot.Path.is_file", return_value=True),
            patch("src.business_snapshot._hermes_version", return_value="0.18.2"),
            patch("src.business_snapshot.subprocess.run", side_effect=timeout),
        ):
            with self.assertRaises(BusinessSnapshotError):
                generate_hermes_business_analysis({}, None, diagnostics=diagnostics)

        self.assertEqual(diagnostics["fallback_reason"], "analysis_timeout")
        self.assertEqual(diagnostics["hermes_failure_code"], "analysis_timeout")
        self.assertEqual(diagnostics["hermes_attempts"], 1)
        self.assertFalse(diagnostics["analysis_retry_used"])
        attempts = diagnostics["hermes_attempt_details"]
        self.assertEqual(len(attempts), 1)
        self.assertIsNone(attempts[0]["returncode"])
        self.assertEqual(attempts[0]["fallback_reason"], "analysis_timeout")

    def test_more_than_two_actions_is_rejected(self) -> None:
        diagnostics: dict[str, object] = {}
        result = CompletedProcess(
            args=["hermes"],
            returncode=0,
            stdout=json.dumps({"actions": [{"kind": "hold_price", "target": "overview", "evidence": ["overview"], "reason": "先看库存", "recheck": "下一批核对可售"}] * 3}, ensure_ascii=False),
            stderr="",
        )

        with (
            patch("src.business_snapshot.Path.is_file", return_value=True),
            patch("src.business_snapshot._hermes_version", return_value="0.18.2"),
            patch("src.business_snapshot.subprocess.run", return_value=result),
        ):
            with self.assertRaises(BusinessSnapshotError):
                generate_hermes_business_analysis({}, None, diagnostics=diagnostics)

        self.assertEqual(diagnostics["fallback_reason"], "invalid_json_schema")

    def test_duplicate_action_target_is_rejected(self) -> None:
        diagnostics: dict[str, object] = {}
        result = CompletedProcess(
            args=["hermes"],
            returncode=0,
            stdout=json.dumps({"actions": [{"kind": "hold_price", "target": "overview", "evidence": ["overview"], "reason": "先看库存", "recheck": "下一批核对可售"}] * 2}, ensure_ascii=False),
            stderr="",
        )

        with (
            patch("src.business_snapshot.Path.is_file", return_value=True),
            patch("src.business_snapshot._hermes_version", return_value="0.18.2"),
            patch("src.business_snapshot.subprocess.run", return_value=result),
        ):
            with self.assertRaises(BusinessSnapshotError):
                generate_hermes_business_analysis({"business_snapshot": {"today_available": 5}}, None, diagnostics=diagnostics)

        self.assertEqual(diagnostics["fallback_reason"], "invalid_json_schema")

    def test_invalid_json_is_classified(self) -> None:
        diagnostics: dict[str, object] = {}
        result = CompletedProcess(args=["hermes"], returncode=0, stdout="不是 JSON", stderr="")

        with (
            patch("src.business_snapshot.Path.is_file", return_value=True),
            patch("src.business_snapshot._hermes_version", return_value="0.18.2"),
            patch("src.business_snapshot.subprocess.run", return_value=result),
        ):
            with self.assertRaises(BusinessSnapshotError):
                generate_hermes_business_analysis({}, None, diagnostics=diagnostics)

        self.assertEqual(diagnostics["fallback_reason"], "invalid_json")

    def test_stderr_summary_redacts_common_secret_values(self) -> None:
        summary = _summarize_hermes_stderr("token: secret-token password=hidden")

        self.assertNotIn("secret-token", summary)
        self.assertNotIn("hidden", summary)

    def test_price_action_combines_verified_stock_price_and_event(self) -> None:
        decision_json = {"actions": [{
            "kind": "review_price", "target": "comparison:0",
            "evidence": ["comparison:0", "inventory:0", "event:0"],
            "reason": "价差较大但活动只是需求背景，先复核可比条件",
            "recheck": "下一批复核该房型余量与直接竞品价格",
        }]}
        result = CompletedProcess(args=["hermes"], returncode=0, stdout=json.dumps(decision_json, ensure_ascii=False), stderr="")
        snapshot = {
            "business_snapshot": {"today_available": 19, "room_type_inventory": [{"roomType": "雅致三人间", "available": 2}]},
            "event_signal": {"active": True, "rules_available": True, "events": [{"event_name": "今日活动", "effective_event_level": "high"}]},
        }
        price_summary = {"comparisons": [{"own_room_name": "雅致三人间", "own_price": 747, "competitor_price": 418}]}
        with (
            patch("src.business_snapshot.Path.is_file", return_value=True),
            patch("src.business_snapshot._hermes_version", return_value="0.18.2"),
            patch("src.business_snapshot.subprocess.run", return_value=result),
        ):
            decision = generate_hermes_business_analysis(snapshot, price_summary)
        message = "\n".join(_render_hermes_decision(decision))
        self.assertIn("雅致三人间：复核同日同权益报价", message)
        self.assertIn("价差较大但活动只是需求背景", message)
        self.assertIn("下一批复核该房型余量与直接竞品价格", message)
        self.assertIn("我方挂牌¥747", message)
        self.assertIn("直接可比竞品¥418", message)
        self.assertIn("今日可售2间", message)
        self.assertIn("今日活动", message)

    def test_event_alone_cannot_trigger_price_action(self) -> None:
        result = CompletedProcess(args=["hermes"], returncode=0, stdout=json.dumps({"actions": [{
            "kind": "hold_price", "target": "event:0", "evidence": ["event:0"],
            "reason": "活动较强", "recheck": "下一批核对库存",
        }]}, ensure_ascii=False), stderr="")
        snapshot = {"event_signal": {"rules_available": True, "events": [{"event_name": "今日活动", "effective_event_level": "high"}]}}
        with (
            patch("src.business_snapshot.Path.is_file", return_value=True),
            patch("src.business_snapshot._hermes_version", return_value="0.18.2"),
            patch("src.business_snapshot.subprocess.run", return_value=result),
        ):
            with self.assertRaises(BusinessSnapshotError):
                generate_hermes_business_analysis(snapshot, None)

    def test_confirmed_event_signal_can_drive_today_preparation_without_competitor(self) -> None:
        result = CompletedProcess(args=["hermes"], returncode=0, stdout=json.dumps({"actions": [{
            "kind": "prepare_event", "target": "future_event:0", "evidence": ["future_event:0"],
            "reason": "活动日期与已确认的库存代理信号重合，今天先核对对应入住日的房型与价格安排",
            "recheck": "下一批核对该入住日库存代理信号与在售房态",
        }]}, ensure_ascii=False), stderr="")
        snapshot = {
            "stay_date": "2026-09-15",
            "future_event_demand_context": {
                "anchor_date": "2026-09-15",
                "events": [{
                    "event_name": "测试展会", "venue": "测试场馆",
                    "event_start_date": "2026-09-16", "event_end_date": "2026-09-18",
                    "impact_level": "medium",
                }],
                "demand_signals": [{
                    "stay_date": "2026-09-16", "state": "持续起量",
                    "confirmed": True, "persistent": True,
                }],
            },
        }
        with (
            patch("src.business_snapshot.Path.is_file", return_value=True),
            patch("src.business_snapshot._hermes_version", return_value="0.18.2"),
            patch("src.business_snapshot.subprocess.run", return_value=result) as runner,
        ):
            decision = generate_hermes_business_analysis(snapshot, None)

        rendered = "\n".join(_render_hermes_decision(decision))
        self.assertIn("2026-09-16 测试展会", rendered)
        self.assertIn("活动入住日的房型、在售价格与低价库存安排", rendered)
        prompt = runner.call_args.args[0][3]
        self.assertIn("库存代理信号达到持续起量条件", prompt)
        self.assertIn('"evidence_catalog": {"future_event:0"', prompt)
        self.assertNotIn('"evidence_catalog": {"comparison:', prompt)

    def test_abnormally_long_event_cannot_drive_preparation(self) -> None:
        result = CompletedProcess(args=["hermes"], returncode=0, stdout=json.dumps({"actions": [{
            "kind": "prepare_event", "target": "future_event:0", "evidence": ["future_event:0"],
            "reason": "活动日期与库存代理信号重合，今天核对房型安排",
            "recheck": "下一批核对对应入住日房态",
        }]}, ensure_ascii=False), stderr="")
        snapshot = {
            "stay_date": "2026-09-15",
            "future_event_demand_context": {
                "anchor_date": "2026-09-15",
                "events": [{
                    "event_name": "异常长活动", "venue": "测试场馆",
                    "event_start_date": "2026-09-16", "event_end_date": "2027-03-15",
                    "impact_level": "high",
                }],
                "demand_signals": [{
                    "stay_date": "2026-09-16", "state": "持续起量",
                    "confirmed": True, "persistent": True,
                }],
            },
        }
        with (
            patch("src.business_snapshot.Path.is_file", return_value=True),
            patch("src.business_snapshot._hermes_version", return_value="0.18.2"),
            patch("src.business_snapshot.subprocess.run", return_value=result),
        ):
            with self.assertRaises(BusinessSnapshotError):
                generate_hermes_business_analysis(snapshot, None)

    def test_confirmed_unmatched_future_demand_can_drive_review(self) -> None:
        result = CompletedProcess(args=["hermes"], returncode=0, stdout=json.dumps({"actions": [{
            "kind": "review_future_demand", "target": "demand_signal:0", "evidence": ["demand_signal:0"],
            "reason": "未来入住日库存代理信号达到规则条件，今天复核该日期的房型与价格安排",
            "recheck": "下一批核对该入住日库存代理信号和在售房态",
        }]}, ensure_ascii=False), stderr="")
        snapshot = {
            "stay_date": "2026-09-15",
            "future_event_demand_context": {
                "anchor_date": "2026-09-15", "events": [],
                "demand_signals": [{
                    "stay_date": "2026-09-20", "state": "持续起量",
                    "confirmed": True, "persistent": True,
                }],
            },
        }
        with (
            patch("src.business_snapshot.Path.is_file", return_value=True),
            patch("src.business_snapshot._hermes_version", return_value="0.18.2"),
            patch("src.business_snapshot.subprocess.run", return_value=result),
        ):
            decision = generate_hermes_business_analysis(snapshot, None)

        rendered = "\n".join(_render_hermes_decision(decision))
        self.assertIn("2026-09-20入住日", rendered)
        self.assertIn("复核该入住日的房型、在售价格与低价库存安排", rendered)

    def test_confirmed_future_signal_does_not_allow_demand_causation(self) -> None:
        facts = {"demand_signal:0": {
            "kind": "demand_signal", "confirmed": True,
            "target_label": "未来入住日", "stay_date": "2026-09-20",
            "text": "2026-09-20未来入住日库存代理信号达到持续起量条件（不等同已确认订单）",
        }}
        payload = {"actions": [{
            "kind": "review_future_demand", "target": "demand_signal:0",
            "evidence": ["demand_signal:0"],
            "reason": "需求正在上升，今天复核该日期价格安排",
            "recheck": "下一批核对该入住日房态",
        }]}
        with self.assertRaises(BusinessSnapshotError):
            _parse_hermes_analysis_output(json.dumps(payload, ensure_ascii=False), {}, facts)

    def test_missing_inventory_is_not_a_valid_action_target(self) -> None:
        result = CompletedProcess(args=["hermes"], returncode=0, stdout=json.dumps({"actions": [{
            "kind": "check_inventory", "target": "inventory:0", "evidence": ["inventory:0"],
            "reason": "库存需要核对", "recheck": "下一批核对房态",
        }]}, ensure_ascii=False), stderr="")
        snapshot = {"business_snapshot": {"room_type_inventory": [{"roomType": "未知房型", "available": None}]}}
        with (
            patch("src.business_snapshot.Path.is_file", return_value=True),
            patch("src.business_snapshot._hermes_version", return_value="0.18.2"),
            patch("src.business_snapshot.subprocess.run", return_value=result),
        ):
            with self.assertRaises(BusinessSnapshotError):
                generate_hermes_business_analysis(snapshot, None)

    def test_price_action_rejects_cross_room_or_future_evidence(self) -> None:
        facts = {
            "comparison:0": {"kind": "comparison", "room_name": "大床房", "text": "大床价差"},
            "inventory:0": {"kind": "inventory", "room_name": "双床房", "text": "双床余量"},
            "future_event:0": {"kind": "future_event", "text": "下周活动"},
        }
        for ref in ("inventory:0", "future_event:0"):
            with self.subTest(ref=ref), self.assertRaises(BusinessSnapshotError):
                _parse_hermes_analysis_output(json.dumps({"actions": [{
                    "kind": "review_price", "target": "comparison:0", "evidence": ["comparison:0", ref],
                    "reason": "价差需要复查", "recheck": "下一批查看可比价格",
                }]}, ensure_ascii=False), {}, facts)

    def test_trial_candidate_respects_existing_five_percent_reduction_limit(self) -> None:
        base = {"own_price": 500, "has_direct_comparison": True, "room_name": "大床房", "action": "小降"}
        facts = _business_evidence({"recommendations": [
            {**base, "suggested_adjustment": 400},
            {**base, "suggested_adjustment": 475},
            {**base, "suggested_adjustment": 480, "action": "人工确认"},
        ]}, {})
        self.assertNotIn("decision:0", facts)
        self.assertEqual(facts["decision:1"]["suggested_price"], 475)
        self.assertNotIn("decision:2", facts)

    def test_history_excludes_different_capacity_and_source(self) -> None:
        def row(total, available, source):
            return {"room_total": total, "available_rooms": available, "raw": {"sales_rate_source": source}, "stay_date": "2026-09-05", "collected_at": "2026-09-05T11:00:00+08:00"}
        current = {"room_total": 40, "sales_rate_source": "inventory_estimate_not_order_api"}
        samples = [row(40, 20, current["sales_rate_source"]), row(50, 20, current["sales_rate_source"]), row(40, 10, "order_api")]
        context = _pms_history_context(current, [], samples)
        self.assertEqual(context["sample_count"], 1)
        self.assertIsNone(context["median_sales_rate"])

    def test_room_baseline_uses_only_comparable_room_ids(self) -> None:
        def row(room_id, available, source="inventory_estimate_not_order_api"):
            return {
                "room_total": 40, "available_rooms": 20, "stay_date": "2026-09-05",
                "collected_at": "2026-09-05T11:00:00+08:00",
                "raw": {"sales_rate_source": source, "room_type_inventory": [
                    {"roomTypeID": room_id, "roomType": "双床房", "available": available},
                ]},
            }
        current = {"room_total": 40, "sales_rate_source": "inventory_estimate_not_order_api"}
        rooms = [{"roomTypeID": "123", "roomType": "双床房", "available": 7}]
        context = _pms_history_context(current, [], [row("123", 2), row("123", 3), row("123", 4), row("999", 5)], rooms)
        self.assertEqual(context["room_baselines"]["123"], {"sample_count": 3, "median_available": 3})

    def test_inventory_surplus_requires_room_baseline_and_keeps_reason(self) -> None:
        facts = _business_evidence({
            "business_snapshot": {"room_type_inventory": [{"roomTypeID": "123", "roomType": "双床房", "available": 7}]},
            "analysis_context": {"pms_history": {"room_baselines": {"123": {"sample_count": 6, "median_available": 3}}}},
        }, {})
        payload = {"actions": [{"kind": "prioritize_sales", "target": "inventory:0", "evidence": ["inventory:0"],
                               "reason": "同房型可售高于可比时段，先让该房型获得更多展示", "recheck": "下一批核对该房型可售是否收敛"}]}
        decision = _parse_hermes_analysis_output(json.dumps(payload, ensure_ascii=False), {}, facts)
        rendered = "\n".join(_render_hermes_decision(decision))
        self.assertIn("同房型可售高于可比时段", rendered)
        self.assertIn("可售中位数3间", rendered)
        facts["inventory:0"].pop("baseline_delta")
        with self.assertRaises(BusinessSnapshotError):
            _parse_hermes_analysis_output(json.dumps(payload, ensure_ascii=False), {}, facts)

    def test_inventory_action_can_use_store_pace_when_room_baseline_is_missing(self) -> None:
        facts = _business_evidence({
            "business_snapshot": {
                "today_available": 20, "actual_sales_rate": 0.30,
                "sales_rate_source": "inventory_estimate_not_order_api",
                "room_type_inventory": [{"roomType": "豪华大床房", "available": 5}],
            },
            "analysis_context": {"pms_history": {
                "sample_count": 6, "median_sales_rate": 0.50, "room_baselines": {},
            }},
        }, {})
        payload = {"actions": [{
            "kind": "prioritize_sales", "target": "inventory:0",
            "evidence": ["inventory:0", "overview", "history"],
            "reason": "全店库存售卖率估算低于可比时段中位数，今天优先承接适配该房型的需求",
            "recheck": "下一批核对全店售卖率估算与该房型可售",
        }]}
        decision = _parse_hermes_analysis_output(json.dumps(payload, ensure_ascii=False), {}, facts)
        self.assertEqual(decision["actions"][0]["kind"], "prioritize_sales")

    def test_unverified_price_trend_is_rejected(self) -> None:
        facts = {"comparison:0": {"kind": "comparison", "room_name": "大床房", "own_price": 500,
                                   "competitor_price": 350, "text": "大床房有效价差"}}
        payload = {"actions": [{"kind": "review_price", "target": "comparison:0", "evidence": ["comparison:0"],
                               "reason": "竞品价差继续扩大，说明需求正在上升", "recheck": "下一批核对挂牌价"}]}
        with self.assertRaises(BusinessSnapshotError):
            _parse_hermes_analysis_output(json.dumps(payload, ensure_ascii=False), {}, facts)

    def test_real_stadium_retry_keeps_grounded_price_actions(self) -> None:
        facts = {
            "comparison:1": {
                "kind": "comparison", "room_name": "雅致电竞三人间",
                "own_price": 472, "competitor_price": 304,
                "text": "雅致电竞三人间我方挂牌¥472，直接可比竞品¥304，价差¥168（我方高）",
            },
            "decision:2": {
                "kind": "decision", "room_name": "豪华电竞大床房",
                "suggested_price": 345.80,
                "text": "豪华电竞大床房现价¥364，系统候选试探价¥345.80（须人工确认）",
            },
        }
        payload = {"actions": [
            {"kind": "review_price", "target": "comparison:1", "evidence": ["comparison:1"],
             "reason": "雅致电竞三人间我方挂牌¥472、直接可比竞品挂牌¥304，价差¥168（我方高），价差显著。今日先人工核对双方挂牌口径与今日可售房态，确认是否真正直接可比后再决定是否调整，不直接降价。",
             "recheck": "下一批核对：该房型我方挂牌是否仍为¥472、该直接可比竞品挂牌是否仍为¥304，以及该房型今日可售房态，仅比对挂牌与可售。"},
            {"kind": "trial_price", "target": "decision:2", "evidence": ["decision:2"],
             "reason": "豪华电竞大床房现价¥364，系统给出候选试探价¥345.80（须人工确认）。可按该候选价小步下调挂牌做试探，验证该房型挂牌的市场接受度；挂牌与库存估算不等同成交或需求，需人工确认。",
             "recheck": "下一批核对：该房型挂牌是否按¥345.80生效，以及该房型今日可售房态；仅核对挂牌与可售，不看成交价。"},
        ]}

        decision = _parse_hermes_analysis_output(json.dumps(payload, ensure_ascii=False), {}, facts)
        rendered = "\n".join(_render_hermes_decision(decision))
        self.assertEqual([action["kind"] for action in decision["actions"]], ["review_price", "trial_price"])
        self.assertIn("雅致电竞三人间", rendered)
        self.assertIn("豪华电竞大床房", rendered)

        payload["actions"][1]["reason"] = "候选价已带来成交增长，建议继续下调挂牌"
        with self.assertRaises(BusinessSnapshotError):
            _parse_hermes_analysis_output(json.dumps(payload, ensure_ascii=False), {}, facts)

    def test_unsupported_cross_room_ranking_is_rejected(self) -> None:
        facts = {"inventory:0": {"kind": "inventory", "room_name": "大床房", "available": 3,
                                  "baseline_delta": 2, "text": "大床房今日可售3间；同星期同时段6天可售中位数1间，偏离+2间"}}
        payload = {"actions": [{"kind": "prioritize_sales", "target": "inventory:0", "evidence": ["inventory:0"],
                               "reason": "该房型是各房型中可售最多的，应优先展示", "recheck": "下一批核对该房型可售"}]}
        with self.assertRaises(BusinessSnapshotError):
            _parse_hermes_analysis_output(json.dumps(payload, ensure_ascii=False), {}, facts)

    def test_grounded_numbers_are_allowed_but_new_numbers_are_rejected(self) -> None:
        facts = {"comparison:0": {"kind": "comparison", "room_name": "大床房", "own_price": 500,
                                   "competitor_price": 350, "text": "大床房我方挂牌¥500，直接可比竞品¥350，价差¥150"}}
        grounded = {"actions": [{"kind": "review_price", "target": "comparison:0", "evidence": ["comparison:0"],
                                  "reason": "现价500高于竞品350，先复核权益", "recheck": "下一批核对价差150"}]}
        result = _parse_hermes_analysis_output(json.dumps(grounded, ensure_ascii=False), {}, facts)
        self.assertEqual(result["actions"][0]["reason"], "现价500高于竞品350，先复核权益")
        ungrounded = {"actions": [{**grounded["actions"][0], "reason": "现价600高于竞品350，先复核权益"}]}
        with self.assertRaises(BusinessSnapshotError):
            _parse_hermes_analysis_output(json.dumps(ungrounded, ensure_ascii=False), {}, facts)

    def test_previous_action_requires_same_hotel_date_and_earlier_time(self) -> None:
        report = {
            "metadata": {"own_hotel_key": "own", "snapshot_date": "2026-09-12", "run_id": "old", "generated_at": "2026-09-12T09:00:00+08:00"},
            "hermes_decision": {"actions": [{"kind": "hold_price", "room_name": "大床房", "reason": "库存偏紧", "recheck": "下一批看余量"}]},
        }
        result = _previous_business_action_context(report, "own", "2026-09-12", "2026-09-12T11:00:00+08:00", "new")
        self.assertFalse(result["execution_confirmed"])
        self.assertEqual(result["actions"][0]["recheck"], "下一批看余量")
        self.assertEqual(_previous_business_action_context(report, "other", "2026-09-12", "2026-09-12T11:00:00+08:00", "new"), {})
        self.assertEqual(_previous_business_action_context(report, "own", "2026-09-12", "2026-09-12T08:00:00+08:00", "new"), {})

    def test_fallback_distinguishes_unknown_sold_out_and_low_inventory(self) -> None:
        from src.advice import generate_business_snapshot_advice
        lines = generate_business_snapshot_advice({"room_type_inventory": [
            {"roomType": "未知库存", "available": None},
            {"roomType": "售罄大床", "available": 0},
            {"roomType": "余房双床", "available": 1},
        ]})
        text = "\n".join(lines)
        self.assertNotIn("未知库存", text)
        self.assertIn("售罄大床已无可售房", text)
        self.assertIn("余房双床仅余1间", text)

    def test_model_timeout_falls_back_to_specific_direct_comparison(self) -> None:
        report = {
            "hermes_decision": None,
            "price_summary": {"comparisons": [{"own_room_name": "大床房", "own_price": 500, "competitor_price": 350}]},
            "pms_room_type_details": [{"roomType": "大床房", "available": 3}],
        }
        lines = _business_advice_lines(report)
        self.assertIn("大床房", "\n".join(lines))
        self.assertIn("我方高¥150", "\n".join(lines))
        self.assertIn("可售3间", "\n".join(lines))
        report["pms_room_type_details"][0]["available"] = 0
        self.assertNotIn("核对同日同权益挂牌价", "\n".join(_business_advice_lines(report)))

    def test_empty_model_action_does_not_invent_inventory_task(self) -> None:
        report = {
            "analysis_source": "hermes", "hermes_decision": {"actions": []},
            "pms_room_type_details": [{"roomType": "大床房", "available": 2}],
            "price_summary": {"comparisons": []},
            "pms_summary": {"available_rooms": 5, "actual_sales_rate": 0.88, "sales_rate_source": "inventory_estimate_not_order_api"},
            "pms_change": {"available_rooms_delta": -8},
        }
        lines = _business_advice_lines(report)
        self.assertIn("本批综合经营数据后未形成需立即执行的新动作", "\n".join(lines))
        self.assertIn("缺少有效直接比价仅限制竞品驱动的价格动作", "\n".join(lines))
        self.assertNotIn("核对 PMS", "\n".join(lines))

    def test_empty_or_failed_model_keeps_confirmed_future_action_without_competitor(self) -> None:
        report = {
            "analysis_source": "rules", "hermes_decision": None,
            "pms_summary": {"available_rooms": 20, "actual_sales_rate": 0.50,
                            "sales_rate_source": "inventory_estimate_not_order_api"},
            "pms_room_type_details": [{"roomType": "大床房", "available": 5}],
            "price_summary": {"comparisons": []},
            "future_event_demand_context": {
                "anchor_date": "2026-09-15",
                "events": [{
                    "event_name": "测试展会", "venue": "测试场馆",
                    "event_start_date": "2026-09-16", "event_end_date": "2026-09-18",
                    "impact_level": "medium",
                }],
                "demand_signals": [{
                    "stay_date": "2026-09-16", "state": "持续起量",
                    "confirmed": True, "persistent": True,
                }],
            },
        }
        message = "\n".join(_business_advice_lines(report))
        self.assertIn("【今日动作｜数据规则兜底】", message)
        self.assertIn("2026-09-16 测试展会", message)
        self.assertIn("活动入住日的房型、在售价格与低价库存安排", message)
        self.assertNotIn("本次经营分析未完成，暂未生成可核实的新动作", message)

        report.pop("pms_room_type_details")
        self.assertIn("2026-09-16 测试展会", "\n".join(_business_advice_lines(report)))

    def test_timeout_is_distinguished_from_no_intervention(self) -> None:
        report = {
            "analysis_source": "rules", "hermes_decision": None,
            "price_summary": {"comparisons": []},
            "pms_summary": {"available_rooms": 2, "actual_sales_rate": 0.95, "sales_rate_source": "inventory_estimate_not_order_api"},
            "pms_change": {"available_rooms_delta": -10},
        }
        self.assertIn("经营分析未完成", "\n".join(_business_advice_lines(report)))
        self.assertNotIn("可售净减", "\n".join(_business_advice_lines(report)))
