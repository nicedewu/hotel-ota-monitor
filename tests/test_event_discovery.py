from datetime import date

import pytest

import src.event_discovery as event_discovery


def _config() -> dict:
    return {
        "qwen": {
            "model": "qwen3.8-flash",
            "api_base": "https://dashscope.aliyuncs.com/api/v1",
            "min_sources_before_max": 0,
        },
        "trusted_source_domains": {"official.example": "official"},
    }


def _result(url: str) -> dict[str, str]:
    return {
        "title": "上海动漫展",
        "summary": "活动将于2026年9月10日至12日在上海举行。",
        "url": url,
    }


def test_untrusted_search_result_remains_discovery_clue() -> None:
    candidate = event_discovery._candidate_from_result(
        _result("https://untrusted.example/event"), date(2026, 9, 10), _config()
    )

    assert candidate is not None
    assert candidate["source_confidence"] == "discovery"
    assert candidate["event_status"] == "discovered"


def test_qwen_offline_mode_and_all_source_evidence_are_preserved() -> None:
    result = {
        **_result("https://one.example/event"),
        "event_start_date": "2026-09-10",
        "event_end_date": "2026-09-12",
        "venue": "东方体育中心",
        "offline_mode": "线下",
        "source_evidence": [
            {"url": "https://one.example/event", "title": "来源一"},
            {"url": "https://two.example/event", "title": "来源二"},
            {"url": "https://three.example/event", "title": "来源三"},
        ],
    }

    candidate = event_discovery._candidate_from_result(
        result, date(2026, 9, 10), {**_config(), "corroboration_min_domains": 3}
    )

    assert candidate is not None
    assert candidate["offline_mode"] == "offline"
    assert len(candidate["source_observations"]) == 3
    assert candidate["corroboration_min_domains"] == 3


def test_trusted_page_with_matching_shanghai_date_is_elevated(monkeypatch) -> None:
    monkeypatch.setattr(
        event_discovery,
        "_fetch_trusted_page",
        lambda _url, _config: "官方公告：上海动漫展将于2026年9月10日至12日在上海体育馆举办。",
    )

    candidate = event_discovery._candidate_from_result(
        {**_result("https://news.official.example/event"), "venue": "上海体育馆"}, date(2026, 9, 10), _config()
    )

    assert candidate is not None
    assert candidate["source_confidence"] == "official"
    assert candidate["event_status"] == "probable"


def test_trusted_page_fields_override_untrusted_search_summary(monkeypatch) -> None:
    monkeypatch.setattr(
        event_discovery,
        "_fetch_trusted_page",
        lambda _url, _config: "官方公告：上海动漫展将于2026年9月20日至21日在上海体育馆举办。",
    )
    result = {
        **_result("https://news.official.example/event"),
        "venue": "上海体育馆",
    }

    candidate = event_discovery._candidate_from_result(
        result,
        date(2026, 9, 20),
        _config(),
        window_end=date(2026, 9, 21),
    )

    assert candidate is not None
    assert candidate["source_confidence"] == "official"
    assert candidate["event_start_date"] == date(2026, 9, 20)
    assert candidate["event_end_date"] == date(2026, 9, 21)
    assert candidate["venue"] == "上海体育馆"
    assert candidate["venue_confidence"] == 100


def test_trusted_page_does_not_elevate_when_date_does_not_cover_anomaly(monkeypatch) -> None:
    monkeypatch.setattr(
        event_discovery,
        "_fetch_trusted_page",
        lambda _url, _config: "官方公告：上海活动将于2026年9月13日至15日举办。",
    )

    candidate = event_discovery._candidate_from_result(
        _result("https://news.official.example/event"), date(2026, 9, 10), _config()
    )

    assert candidate is not None
    assert candidate["source_confidence"] == "discovery"
    assert candidate["event_status"] == "discovered"


def test_trusted_domain_matching_rejects_lookalikes() -> None:
    assert event_discovery._trusted_source_level("https://news.official.example/event", _config()) == "official"
    assert event_discovery._trusted_source_level("https://official.example.evil.test/event", _config()) == ""


def test_candidate_verification_merges_qwen_and_shanghai_government_results(monkeypatch) -> None:
    import src.event_corroboration as corroboration

    captured = {}
    candidate = {
        "event_name": "2026无畏契约全球冠军赛",
        "aliases": ["VALORANT Champions Shanghai"],
        "event_start_date": date(2026, 9, 24),
        "event_end_date": date(2026, 10, 18),
        "venue": "梅赛德斯-奔驰文化中心",
        "source_observations": [],
        "raw": {"entry": {}},
    }
    config = {
        "trusted_source_domains": {"shanghai.gov.cn": "authoritative"},
        "shanghai_government_search": {"enabled": True, "results_per_event": 5},
        "verification_pages_per_event": 8,
    }

    def fake_qwen(_config, query, *_args, **_kwargs):
        captured["qwen"] = query
        return []

    def fake_government(_config, query, *, limit):
        captured["government"] = (query, limit)
        return [
            {
                "url": "https://www.shanghai.gov.cn/events/vct-current.html",
                "title": "无畏契约金秋抵沪",
                "source_level": "authoritative",
                "discovered_via": "shanghai_government_search",
            },
            {
                "url": "https://www.shanghai.gov.cn/events/vct-old.html",
                "title": "无畏契约往期报道",
                "source_level": "authoritative",
                "discovered_via": "shanghai_government_search",
            },
        ]

    monkeypatch.setattr(event_discovery, "_search", fake_qwen)
    monkeypatch.setattr(event_discovery, "shanghai_government_search", fake_government)
    monkeypatch.setattr(
        corroboration,
        "fetch_page",
        lambda _url: (
            "2026无畏契约全球冠军赛于2026年9月24日至10月18日在上海举办，"
            "总决赛定于梅赛德斯-奔驰文化中心举行。",
            "ok",
        ),
    )

    enriched = event_discovery._verify_candidate_sources(candidate, config)

    assert "VALORANT Champions Shanghai" in captured["qwen"]
    assert captured["government"] == (
        "2026无畏契约全球冠军赛",
        5,
    )
    observation = enriched["source_observations"][0]
    assert observation["url"] == "https://www.shanghai.gov.cn/events/vct-current.html"
    assert observation["source_level"] == "authoritative"
    assert observation["validation"]["supported"]
    assert observation["validation"]["overall_dates"]


def test_proactive_queries_cover_the_complete_90_day_horizon_without_named_games() -> None:
    queries = event_discovery._proactive_queries(
        date(2026, 8, 10),
        90,
        {
            "window_days": 30,
            "max_full_queries": 12,
            "query_groups": [
                {"category": "competitive", "terms": "电竞赛事 体育赛事"},
                {"category": "entertainment", "terms": "演唱会 音乐节 演出"},
                {"category": "business_and_city", "terms": "展会 博览会 会议"},
            ],
        },
    )

    assert len(queries) == 9
    assert any(start <= date(2026, 8, 20) <= end and category == "competitive" for start, end, category, _ in queries)
    assert any(start <= date(2026, 10, 15) <= end for start, end, _, _ in queries)
    assert any(start <= date(2026, 11, 1) <= end for start, end, _, _ in queries)
    assert any("演唱会" in query for _, _, _, query in queries)
    assert all("DOTA2" not in query and "英雄联盟" not in query for _, _, _, query in queries)


def test_full_queries_add_generic_near_term_priority_searches_without_named_events() -> None:
    config = {
        "window_days": 30,
        "priority_window_days": 14,
        "max_full_queries": 20,
        "query_groups": [
            {"category": category, "terms": terms}
            for category, terms in (
                ("esports", "电竞赛事"),
                ("sports", "体育赛事"),
                ("performance", "演唱会"),
                ("acg", "动漫展"),
                ("exhibition", "博览会"),
            )
        ],
        "priority_query_groups": [
            {"category": "esports", "terms": "大型线下电竞职业联赛 总决赛 年度盛典 场馆 票务 官方公告"},
            {"category": "sports", "terms": "大型线下国际体育赛事 全国决赛 官方公告"},
            {"category": "performance", "terms": "大型线下演唱会 万人场 官方公告"},
            {"category": "acg", "terms": "大型线下动漫展 游戏展 官方公告"},
            {"category": "exhibition", "terms": "国家级展会 国际博览会 官方公告"},
        ],
    }

    queries = event_discovery._proactive_queries(
        date(2026, 9, 5), 90, config, include_priority=True
    )

    assert len(queries) == 20
    priority = queries[:5]
    assert all(start == date(2026, 9, 6) and end == date(2026, 9, 19) for start, end, _, _ in priority)
    assert {category for _, _, category, _ in priority} == {
        "esports", "sports", "performance", "acg", "exhibition"
    }
    assert "职业联赛" in priority[0][3]
    assert any(start <= date(2026, 11, 20) <= end for start, end, _, _ in queries[5:])
    assert all("LPL" not in query and "英雄联盟" not in query for _, _, _, query in queries)


def test_forward_scan_limits_trusted_page_verification(monkeypatch) -> None:
    config = {
        "enabled": True,
        "qwen": {
            "model": "qwen3.8-flash",
            "api_base": "https://dashscope.aliyuncs.com/api/v1",
            "min_sources_before_max": 0,
        },
        "results_per_query": 5,
        "max_trusted_page_verifications": 1,
        "window_days": 7,
        "max_full_queries": 2,
        "query_groups": [{"category": "competitive", "terms": "电竞"}],
        "trusted_source_domains": {"official.example": "official"},
    }
    monkeypatch.setattr(event_discovery, "_load_config", lambda _path: config)
    monkeypatch.setattr(event_discovery, "_search", lambda *_args, **_kwargs: [_result("https://official.example/a"), _result("https://official.example/b")])
    monkeypatch.setattr(event_discovery, "psycopg", object())
    calls: list[bool] = []

    def fake_candidate(_item, _stay_date, _config, **kwargs):
        calls.append(kwargs["verify_trusted_page"])
        return None

    monkeypatch.setattr(event_discovery, "_candidate_from_result", fake_candidate)
    event_discovery.run_forward_discovery("unused", anchor_date=date(2026, 8, 10), days=14, dry_run=True)

    assert calls == [True, False, False, False]


def test_forward_scan_raises_when_every_primary_query_fails(monkeypatch) -> None:
    config = {
        "enabled": True,
        "qwen": {
            "model": "qwen3.8-flash",
            "api_base": "https://dashscope.aliyuncs.com/api/v1",
            "min_sources_before_max": 0,
        },
        "results_per_query": 5,
        "max_trusted_page_verifications": 0,
        "window_days": 30,
        "max_full_queries": 1,
        "query_groups": [{"category": "sports", "terms": "体育赛事"}],
    }
    monkeypatch.setattr(event_discovery, "_load_config", lambda _path: config)
    monkeypatch.setattr(event_discovery, "psycopg", object())
    monkeypatch.setattr(
        event_discovery,
        "_search",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(event_discovery.EventDiscoveryError("API unavailable")),
    )

    with pytest.raises(event_discovery.EventDiscoveryError, match="全部查询失败"):
        event_discovery.run_forward_discovery(
            "unused",
            anchor_date=date(2026, 9, 5),
            days=30,
            dry_run=True,
        )


def test_full_scan_uses_max_and_incremental_scan_uses_turbo(monkeypatch) -> None:
    config = {
        "enabled": True,
        "qwen": {
            "model": "qwen3.8-flash",
            "api_base": "https://dashscope.aliyuncs.com/api/v1",
            "min_sources_before_max": 0,
            "full_search_strategy": "max",
            "incremental_search_strategy": "turbo",
        },
        "results_per_query": 5,
        "max_trusted_page_verifications": 0,
        "window_days": 30,
        "max_full_queries": 1,
        "query_groups": [{"category": "sports", "terms": "体育赛事"}],
    }
    monkeypatch.setattr(event_discovery, "_load_config", lambda _path: config)
    monkeypatch.setattr(event_discovery, "psycopg", object())
    strategies: list[str] = []

    def fake_search(*_args, **kwargs):
        strategies.append(kwargs["strategy"])
        return []

    monkeypatch.setattr(event_discovery, "_search", fake_search)
    event_discovery.run_forward_discovery(
        "unused", anchor_date=date(2026, 9, 5), days=30, dry_run=True, recent=False
    )
    event_discovery.run_forward_discovery(
        "unused", anchor_date=date(2026, 9, 5), days=30, dry_run=True, recent=True
    )

    assert strategies == ["max", "turbo"]


def test_trusted_page_requires_target_name_and_same_entry_fields(monkeypatch) -> None:
    page = """2026上海旅游节活动清单
上海旅游节花车巡游
活动时间：2026年9月13日-10月6日
活动地点：上海各区
2026最江南上海青浦文化旅游节
百联奥特莱斯品牌广场二期中心广场举办启动仪式。
活动时间：2026年9月28日-10月28日
活动地点：青浦区
“乡约古镇·乐游青溪”2026奉贤青村乡村文化旅游季
活动时间：2026年9月19日-10月18日
活动地点：奉贤青村镇
"""
    monkeypatch.setattr(event_discovery, "_fetch_trusted_page", lambda *_args: page)
    result = {"title": "“乡约古镇·乐游青溪”活动", "url": "https://official.example/list",
              "venue": "百联奥特莱斯品牌广场二期中心广场", "category": "other"}
    candidate = event_discovery._candidate_from_result(result, date(2026, 10, 1), _config(), category="performance")
    assert candidate is not None
    assert candidate["source_confidence"] == "discovery"
    assert candidate["venue_confidence"] <= 50
    assert candidate["category"] == "other"


def test_official_page_about_another_event_is_not_elevated(monkeypatch) -> None:
    monkeypatch.setattr(event_discovery, "_fetch_trusted_page",
                        lambda *_args: "上海旅游节于2026年9月10日至12日举办。")
    candidate = event_discovery._candidate_from_result(_result("https://official.example/list"), date(2026, 9, 10), _config())
    assert candidate["source_confidence"] == "discovery"


def test_correct_list_entry_overrides_only_its_own_fields(monkeypatch) -> None:
    page = """2026上海活动清单
其他演唱会
活动时间：2026年9月13日-10月6日
活动地点：上海音乐厅
上海动漫展
活动时间：2026年9月20日-9月21日
活动地点：上海体育馆
"""
    monkeypatch.setattr(event_discovery, "_fetch_trusted_page", lambda *_args: page)
    result = {**_result("https://official.example/list"), "venue": "上海体育馆"}
    candidate = event_discovery._candidate_from_result(result, date(2026, 9, 20), _config())
    assert candidate["source_confidence"] == "official"
    assert candidate["event_start_date"] == date(2026, 9, 20)
    assert candidate["event_end_date"] == date(2026, 9, 21)
    assert candidate["venue_confidence"] == 100


def test_actual_event_category_does_not_inherit_search_group() -> None:
    result = {**_result("https://untrusted.example/event"), "category": "acg"}
    candidate = event_discovery._candidate_from_result(result, date(2026, 9, 10), _config(), category="performance")
    assert candidate["category"] == "acg"


def test_shared_hotel_context_reads_all_hotels_and_deduplicates_venues() -> None:
    from unittest.mock import Mock
    cursor = Mock()
    cursor.fetchall.return_value = [
        {"hotel_key": f"own_{index}", "hotel_name": f"上海电竞酒店{index}", "city": "上海",
         "district": None, "venue": "上海新国际博览中心", "impact_level": "high"}
        for index in range(5)
    ]
    config = event_discovery._shared_search_config({"include_active_hotel_context": True, "qwen": {}}, cursor)
    assert cursor.execute.call_count == 1
    assert len(config["qwen"]["hotel_context"]) == 5
    assert config["shared_venues"] == ["上海新国际博览中心"]


def test_forward_queries_are_shared_by_five_hotels(monkeypatch) -> None:
    context = [{"hotel_key": f"own_{index}", "hotel_name": f"店{index}"} for index in range(5)]
    config = {"qwen": {"hotel_context": context, "min_sources_before_max": 0},
              "window_days": 30, "max_full_queries": 1,
              "query_groups": [{"category": "esports", "terms": "线下电竞赛事 官方赛程"}]}
    monkeypatch.setattr(event_discovery, "_load_config", lambda *_args: config)
    monkeypatch.setattr(event_discovery, "psycopg", object())
    calls = []
    def search(shared, *_args, **_kwargs):
        calls.append(shared["qwen"]["hotel_context"])
        return []
    monkeypatch.setattr(event_discovery, "_search", search)
    summary = event_discovery.run_forward_discovery("unused", anchor_date=date(2026, 10, 1), days=30, dry_run=True)
    assert summary["queries"] == 1
    assert calls == [context]


@pytest.mark.parametrize("query_budget", [3, 6])
def test_reverse_search_reuses_same_date_results_across_five_hotels(monkeypatch, query_budget) -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    cursor, connection = MagicMock(), MagicMock()
    connection.__enter__.return_value = connection
    connection.cursor.return_value.__enter__.return_value = cursor
    cursor.fetchone.return_value = {"count": 0}
    monkeypatch.setattr(event_discovery, "psycopg", SimpleNamespace(connect=lambda *_args, **_kwargs: connection,
                                                                  rows=SimpleNamespace(dict_row=None)))
    monkeypatch.setattr(event_discovery, "_load_database_url", lambda: "<DATABASE_URL>")
    monkeypatch.setattr(event_discovery, "_load_config", lambda *_args:
                        {"max_reverse_anomalies": 5, "max_reverse_queries": query_budget})
    monkeypatch.setattr(event_discovery, "_due_anomalies", lambda *_args, **_kwargs:
                        [{"id": index, "stay_date": date(2026, 10, 1)} for index in range(5)])
    monkeypatch.setattr(event_discovery, "_already_due", lambda *_args: True)
    calls = []
    monkeypatch.setattr(event_discovery, "_search", lambda *_args, **_kwargs: calls.append(_args[1]) or [])
    summary = event_discovery.run_reverse_discovery("unused", dry_run=True)
    assert summary["processed"] == 5
    assert summary["queries"] == 3
    assert len(calls) == 3
