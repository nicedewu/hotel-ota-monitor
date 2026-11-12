import json
from datetime import date
from io import BytesIO
from urllib.error import HTTPError

import pytest

import src.qwen_search as qwen_search


def test_one_search_request_contains_all_five_hotels() -> None:
    context = [{"hotel_key": f"own_{index}", "hotel_name": f"上海电竞酒店{index}"} for index in range(5)]
    qwen = {**_config()["qwen"], "hotel_context": context}
    payload = json.loads(qwen_search._request_payload("上海2026年10月电竞赛事", qwen, recent=False, strategy="max"))
    instructions = payload["input"]["messages"][0]["content"][0]["text"]
    assert all(item["hotel_key"] in instructions for item in context)
    assert "不要逐店重复联网搜索" in instructions
    assert "普通旅游季" in instructions and "跨条目拼接" in instructions
    schema = payload["parameters"]["response_format"]["json_schema"]["schema"]
    assert "hotel_relevance" in schema["properties"]["events"]["items"]["required"]


def test_structured_search_filters_ended_future_missing_and_reversed_dates() -> None:
    sources = [{"source_index": 1, "url": "https://official.example/event"}]
    def event(name, start, end):
        return {"event_name": name, "source_indexes": [1], "event_start_date": start, "event_end_date": end}
    payload = {"events": [
        event("模型实测返回的已结束赛事", "2026-09-13", "2026-09-13"),
        event("查询之后的赛事", "2026-10-20", "2026-10-21"),
        event("日期缺失", None, None),
        event("日期颠倒", "2026-10-10", "2026-10-09"),
        event("有效赛事", "2026-10-03", "2026-10-04"),
        event("覆盖查询窗口的进行中赛事", "2026-09-24", "2026-10-18"),
    ]}
    results = qwen_search._structured_event_results(json.dumps(payload), sources, limit=8,
                                                   date_window=(date(2026, 10, 2), date(2026, 10, 15)))
    assert [item["title"] for item in results] == ["有效赛事", "覆盖查询窗口的进行中赛事"]


def test_hotel_relevance_keeps_cited_known_hotels_and_marks_missing_unknown() -> None:
    source = {"source_index": 1, "url": "https://official.example/event", "title": "上海电竞赛事"}
    event = {"event_name": "上海电竞赛事", "source_indexes": [1], "hotel_relevance": [
        {"hotel_key": "own_0", "impact_level": "high", "reason": "事实：相关场馆公布赛事；推断：跨城观赛可能住宿。", "source_indexes": [1]},
        {"hotel_key": "own_1", "impact_level": "high", "reason": "缺少支持的引用", "source_indexes": [99]},
        {"hotel_key": "invented", "impact_level": "high", "reason": "错误门店", "source_indexes": [1]},
    ]}
    results = qwen_search._structured_event_results(json.dumps({"events": [event]}), [source], limit=8,
                                                   hotel_keys={f"own_{index}" for index in range(5)})
    levels = {row["hotel_key"]: row["impact_level"] for row in results[0]["hotel_relevance"]}
    assert levels == {"own_0": "high", "own_1": "unknown", "own_2": "unknown", "own_3": "unknown", "own_4": "unknown"}


def _sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n".encode()


class _FakeResponse:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args) -> None:
        return None

    def __iter__(self):
        return iter(self._lines)


def _config() -> dict:
    return {
        "qwen": {
            "model": "qwen3.8-flash",
            "api_base": "https://dashscope.aliyuncs.com/api/v1",
            "api_key_env": "DASHSCOPE_API_KEY",
            "search_strategy": "turbo",
            "timeout_seconds": 45,
            "max_retries": 0,
        }
    }


def test_sse_parser_keeps_first_source_packet_when_later_packets_are_empty() -> None:
    source_packet = {
        "output": {
            "search_info": {
                "search_results": [
                    {
                        "title": "上海活动官方公告",
                        "url": "https://official.example/event#sources",
                        "snippet": "2026年9月10日至12日举办",
                    }
                ]
            }
        }
    }
    later_packet = {"output": {"search_info": {"search_results": []}, "choices": []}}

    results = qwen_search._search_results_from_sse_lines(
        [
            f"data: {json.dumps(source_packet, ensure_ascii=False)}\n".encode(),
            f"data: {json.dumps(later_packet)}\n".encode(),
            b"data: [DONE]\n",
        ],
        limit=5,
    )

    assert results == [
        {
            "title": "上海活动官方公告",
            "summary": "2026年9月10日至12日举办",
            "url": "https://official.example/event",
            "published_at": "",
            "provider": "qwen_native",
        }
    ]


def test_request_forces_search_and_requests_structured_cited_output() -> None:
    payload = json.loads(qwen_search._request_payload("上海未来活动", _config()["qwen"], recent=True, strategy="turbo"))

    assert payload["model"] == "qwen3.8-flash"
    assert payload["parameters"]["enable_search"] is True
    assert payload["parameters"]["enable_thinking"] is False
    assert payload["parameters"]["search_options"] == {
        "forced_search": True,
        "search_strategy": "turbo",
        "enable_source": True,
        "enable_citation": True,
        "prepend_search_result": True,
    }
    assert payload["parameters"]["max_tokens"] == 4096
    assert payload["parameters"]["response_format"]["type"] == "json_schema"
    assert payload["input"]["messages"][1]["content"][0]["text"] == "上海未来活动"
    instructions = payload["input"]["messages"][0]["content"][0]["text"]
    assert "改期" in instructions
    assert "日期是活动举办日期" in instructions
    assert "aliases" in instructions
    assert "检索上海未来活动" not in instructions


def test_sse_parser_rejects_business_error_event() -> None:
    with pytest.raises(qwen_search.QwenSearchError, match="InvalidApiKey"):
        qwen_search._search_results_from_sse_lines(
            [b'data: {"code":"InvalidApiKey","message":"bad"}\n'],
            limit=5,
        )


def test_structured_events_require_and_preserve_real_source_references() -> None:
    sources = [
        {
            "title": "官方活动页",
            "summary": "",
            "url": "https://official.example/event",
            "published_at": "",
            "provider": "qwen_native",
            "source_index": 2,
        }
    ]
    answer = json.dumps(
        {
            "events": [
                {
                    "event_name": "上海电竞赛事",
                    "aliases": ["Shanghai Esports Event", "SEE"],
                    "event_start_date": "2026-09-20",
                    "event_end_date": "2026-09-21",
                    "venue": "上海体育馆",
                    "category": "esports",
                    "event_status": "confirmed",
                    "offline_mode": "offline",
                    "summary": "线下总决赛",
                    "impact_level": "high",
                    "impact_reason": "年轻玩家客流集中",
                    "source_indexes": [2],
                }
            ],
            "overview": "",
            "key_periods": [],
            "operating_advice": [],
        },
        ensure_ascii=False,
    )

    results = qwen_search._structured_event_results(answer, sources, limit=5)

    assert results[0]["title"] == "上海电竞赛事"
    assert results[0]["aliases"] == ["Shanghai Esports Event", "SEE"]
    assert results[0]["event_start_date"] == "2026-09-20"
    assert results[0]["venue"] == "上海体育馆"
    assert results[0]["source_urls"] == ["https://official.example/event"]
    assert results[0]["source_evidence"] == [
        {
            "url": "https://official.example/event",
            "title": "官方活动页",
            "summary": "",
            "published_at": "",
        }
    ]


def test_structured_event_without_returned_source_is_dropped() -> None:
    answer = json.dumps(
        {
            "events": [{"event_name": "无来源活动", "source_indexes": [99]}],
            "overview": "",
            "key_periods": [],
            "operating_advice": [],
        },
        ensure_ascii=False,
    )

    assert qwen_search._structured_event_results(answer, [], limit=5) == []


def test_structured_online_or_explicitly_non_shanghai_events_are_dropped() -> None:
    sources = [{"source_index": 1, "url": "https://example.test/event"}]
    base = {
        "event_start_date": "2026-09-20",
        "event_end_date": "2026-09-20",
        "category": "esports",
        "event_status": "confirmed",
        "summary": "",
        "impact_level": "low",
        "impact_reason": "",
        "source_indexes": [1],
    }
    answer = json.dumps(
        {
            "events": [
                {**base, "event_name": "线上赛事", "venue": "线上", "offline_mode": "线上"},
                {**base, "event_name": "外地赛事", "venue": "非上海场馆", "offline_mode": "线下"},
            ],
            "overview": "",
            "key_periods": [],
            "operating_advice": [],
        },
        ensure_ascii=False,
    )

    assert qwen_search._structured_event_results(answer, sources, limit=5) == []


def test_valid_empty_structured_answer_does_not_fall_back_to_raw_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    answer = json.dumps(
        {"events": [], "overview": "未发现", "key_periods": [], "operating_advice": []},
        ensure_ascii=False,
    )
    chunks = [
        _sse(
            {
                "output": {
                    "choices": [{"message": {"content": [{"text": answer}]}}],
                    "search_info": {
                        "search_results": [
                            {"index": 1, "title": "无关网页", "url": "https://example.test/noise"}
                        ]
                    },
                }
            }
        )
    ]
    monkeypatch.setattr(qwen_search, "urlopen", lambda *_args, **_kwargs: _FakeResponse(chunks))

    assert qwen_search.search(_config(), "上海未来活动", 5) == []


def test_unparseable_answer_is_rejected_instead_of_falling_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    chunks = [
        _sse(
            {
                "output": {
                    "choices": [{"message": {"content": [{"text": "not-json"}]}}],
                    "search_info": {
                        "search_results": [
                            {"index": 1, "title": "来源线索", "url": "https://example.test/clue"}
                        ]
                    },
                }
            }
        )
    ]
    monkeypatch.setattr(qwen_search, "urlopen", lambda *_args, **_kwargs: _FakeResponse(chunks))

    with pytest.raises(qwen_search.QwenSearchError, match="无法解析的结构化响应"):
        qwen_search.search(_config(), "上海未来活动", 5)


def test_unparseable_answer_is_retried_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    config = _config()
    config["qwen"]["max_retries"] = 1
    good_answer = json.dumps(
        {"events": [], "overview": "未发现", "key_periods": [], "operating_advice": []},
        ensure_ascii=False,
    )
    responses = iter(
        [
            _FakeResponse([_sse({"output": {"choices": [{"message": {"content": [{"text": "bad"}]}}]}})]),
            _FakeResponse([_sse({"output": {"choices": [{"message": {"content": [{"text": good_answer}]}}]}})]),
        ]
    )
    monkeypatch.setattr(qwen_search, "urlopen", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(qwen_search.time, "sleep", lambda *_args: None)

    assert qwen_search.search(config, "上海活动", 5) == []


def test_api_base_rejects_non_beijing_or_non_api_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QWEN_SEARCH_API_BASE", "https://example.test/api/v1")
    with pytest.raises(qwen_search.QwenSearchError, match="北京地域"):
        qwen_search._api_base(_config())


def test_authentication_failure_never_includes_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "secret-value-must-not-leak")

    def fail(*_args, **_kwargs):
        raise HTTPError("https://dashscope.aliyuncs.com", 401, "unauthorized", {}, BytesIO())

    monkeypatch.setattr(qwen_search, "urlopen", fail)
    with pytest.raises(qwen_search.QwenSearchError) as raised:
        qwen_search.search(_config(), "上海活动", 5)

    assert str(raised.value) == "Qwen 原生联网搜索认证失败"
    assert "secret-value" not in str(raised.value)
