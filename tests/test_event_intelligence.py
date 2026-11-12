from datetime import date
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import pytest

import src.event_intelligence as event_intelligence
from src.event_intelligence import (
    _fetch_allcpp_event,
    _fetch_bilibili_world,
    _fetch_html_table_calendar,
    _fetch_json,
    _fetch_riot_schedule,
    _fetch_html_with_chrome,
    _candidate_is_ready_for_business,
    _event_identity_key,
    _incoming_can_replace,
    _merged_candidate_city,
    _merged_event_status,
    _occurrence_key,
    _registrable_domain,
    _source_url,
    candidate_from_entry,
    parse_event_dates,
)


def _source(**overrides: object) -> dict:
    source = {
        "key": "steam-dota2-news",
        "kind": "steam_news",
        "city": "上海",
        "category": "esports",
        "source_level": "official",
        "event_name": "DOTA2 国际邀请赛",
        "aliases": ["The International", "TI"],
        "city_markers": ["上海", "shanghai"],
        "match_terms": ["the international"],
    }
    source.update(overrides)
    return source


def test_parse_explicit_chinese_event_date_range() -> None:
    start, end, confidence = parse_event_dates("赛事将于2026年8月20日至23日在上海举办。")

    assert start == date(2026, 8, 20)
    assert end == date(2026, 8, 23)
    assert confidence == 100


def test_parse_english_event_date_range_uses_year_elsewhere_in_official_copy() -> None:
    start, end, confidence = parse_event_dates(
        "The International 2026 opens in Shanghai on August 20-23 at the Oriental Sports Center.",
        fallback_year=2026,
    )

    assert start == date(2026, 8, 20)
    assert end == date(2026, 8, 23)
    assert confidence == 100


def test_official_entry_becomes_a_traceable_candidate() -> None:
    candidate = candidate_from_entry(
        {
            "title": "The International 将在上海举办",
            "summary": "赛事将于2026年8月20日至23日举行。",
            "url": "https://www.dota2.com/newsentry/example",
            "published_at": "2026-02-12T00:00:00+00:00",
        },
        _source(),
    )

    assert candidate is not None
    assert candidate["event_name"] == "DOTA2 国际邀请赛"
    assert candidate["city"] == "上海"
    assert candidate["event_start_date"] == date(2026, 8, 20)
    assert candidate["source_confidence"] == "official"
    assert candidate["source_url"].startswith("https://")
    assert candidate["candidate_key"]
    assert candidate["event_identity_key"]


def test_discovery_retains_candidate_when_venue_confirms_shanghai_or_city_is_unknown() -> None:
    source = _source(source_level="discovery", event_name=None, match_terms=[])
    entry = {"title": "RED LAND 2026", "summary": "2026年10月2日至6日举办线下嘉年华。",
             "url": "https://example.com/event", "venue": "上海复兴岛船台PARK"}
    located = candidate_from_entry(entry, source)
    assert located is not None and located["city"] == "上海"
    unknown = candidate_from_entry({**entry, "venue": "船台PARK"}, source)
    assert unknown is not None and unknown["city"] == "待确认"


def test_confirmed_city_replaces_unknown_candidate_city() -> None:
    assert _merged_candidate_city("待确认", "上海") == "上海"
    assert _merged_candidate_city("上海", "待确认") == "上海"


def test_event_identity_survives_a_verified_venue_or_date_correction() -> None:
    base = {
        "event_name": "2026 上海大型赛事",
        "city": "上海",
        "category": "competitive",
        "event_start_date": date(2026, 8, 20),
        "venue": "东方体育中心",
    }
    corrected = {**base, "event_start_date": date(2026, 8, 27), "venue": "上海体育馆"}

    assert _event_identity_key(base) == _event_identity_key(corrected)


def test_discovery_observation_cannot_replace_an_official_observation() -> None:
    existing = {
        "source_confidence": "official",
        "event_start_date": date(2026, 8, 20),
        "event_end_date": date(2026, 8, 23),
        "venue": "东方体育中心",
    }
    incoming = {
        "source_confidence": "discovery",
        "event_start_date": date(2026, 8, 27),
        "event_end_date": date(2026, 8, 30),
        "venue": "上海体育馆",
    }

    assert _incoming_can_replace(existing, incoming) is False


def test_official_cancellation_overrides_probable_or_promoted_status() -> None:
    assert _merged_event_status("probable", "cancelled") == "cancelled"
    assert _merged_event_status("promoted", "cancelled") == "cancelled"


def test_dated_occurrences_do_not_share_the_business_identity() -> None:
    first = _occurrence_key("上海大型赛事", "上海", "东方体育中心", date(2026, 8, 20))
    later = _occurrence_key("上海大型赛事", "上海", "东方体育中心", date(2026, 10, 20))

    assert first != later


def test_discovery_candidate_cannot_enter_business_facts() -> None:
    candidate = {
        "city": "上海",
        "event_status": "discovered",
        "verification_state": "unverified",
        "offline_mode": "offline",
        "venue": "东方体育中心",
        "event_start_date": date(2026, 8, 20),
        "event_end_date": date(2026, 8, 23),
        "date_confidence": 100,
        "venue_confidence": 100,
    }

    assert _candidate_is_ready_for_business(candidate) is False


def test_verified_candidate_requires_explicit_venue_evidence() -> None:
    candidate = {
        "city": "上海",
        "event_status": "confirmed",
        "verification_state": "unverified",
        "offline_mode": "offline",
        "venue": "东方体育中心",
        "event_start_date": date(2026, 8, 20),
        "event_end_date": date(2026, 8, 23),
        "date_confidence": 100,
        "venue_confidence": 0,
    }

    assert _candidate_is_ready_for_business(candidate) is False


def test_conflicting_candidate_cannot_enter_business_facts() -> None:
    candidate = {
        "city": "上海",
        "event_status": "confirmed",
        "verification_state": "conflict",
        "offline_mode": "offline",
        "venue": "东方体育中心",
        "event_start_date": date(2026, 8, 20),
        "event_end_date": date(2026, 8, 23),
        "date_confidence": 100,
        "venue_confidence": 100,
    }

    assert _candidate_is_ready_for_business(candidate) is False


def test_corroborated_candidate_cannot_be_promoted_into_canonical_facts() -> None:
    candidate = {
        "city": "上海",
        "event_status": "probable",
        "verification_state": "corroborated",
        "source_confidence": "corroborated",
        "independent_source_count": 3,
        "offline_mode": "offline",
        "venue": "东方体育中心",
        "event_start_date": date(2026, 9, 12),
        "event_end_date": date(2026, 9, 13),
        "date_confidence": 50,
        "venue_confidence": 50,
    }

    assert _candidate_is_ready_for_business(candidate) is False
    candidate["independent_source_count"] = 2
    assert _candidate_is_ready_for_business(candidate) is False


def test_registrable_domain_deduplicates_mobile_and_www_subdomains() -> None:
    assert _registrable_domain("https://www.sohu.com/a/1") == "sohu.com"
    assert _registrable_domain("https://m.sohu.com/a/2") == "sohu.com"
    assert _registrable_domain("https://news.example.com.cn/a") == "example.com.cn"


def test_unmatched_entry_is_not_saved_as_a_candidate() -> None:
    candidate = candidate_from_entry(
        {
            "title": "其他游戏更新",
            "summary": "本次更新没有线下活动。",
            "url": "https://www.dota2.com/newsentry/other",
        },
        _source(),
    )

    assert candidate is None


def test_event_without_shanghai_marker_is_not_saved_as_a_candidate() -> None:
    candidate = candidate_from_entry(
        {
            "title": "The International 2026",
            "summary": "赛事将于2026年8月20日至23日举行。",
            "url": "https://www.dota2.com/newsentry/outside-shanghai",
            "published_at": "2026-02-12T00:00:00+00:00",
        },
        _source(),
    )

    assert candidate is None


def test_transient_source_error_retries_once() -> None:
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"ok": true}'
    with patch("src.event_intelligence.urlopen", side_effect=[URLError("temporary"), response]), patch(
        "src.event_intelligence.time.sleep"
    ) as sleep:
        assert _fetch_json("https://example.test/source", 5) == {"ok": True}

    sleep.assert_called_once_with(1)


def test_url_template_uses_current_year_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    class Today:
        @classmethod
        def today(cls) -> date:
            return date(2027, 1, 2)

    monkeypatch.setattr(event_intelligence, "date", Today)
    assert _source_url({"key": "bw", "url_template": "https://bw.example/{year}/"}) == "https://bw.example/2027/"


def test_chrome_dynamic_render_rejects_non_allowlisted_host() -> None:
    with pytest.raises(event_intelligence.EventIntelligenceError, match="允许域名"):
        _fetch_html_with_chrome(
            {"key": "riot", "dynamic_allowed_hosts": ["official.example"]},
            "https://evil.example/schedule",
            "",
        )


def test_all_event_source_failures_are_visible_to_the_scheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(event_intelligence, "_load_sources", lambda _path: [_source()])

    def fail_fetch(_source_value: dict, _timeout_seconds: int) -> list[dict]:
        raise event_intelligence.EventIntelligenceError("temporary failure")

    monkeypatch.setattr(event_intelligence, "_fetch_entries", fail_fetch)

    with pytest.raises(event_intelligence.EventIntelligenceError, match="所有活动来源均同步失败"):
        event_intelligence.sync_event_sources("unused.json", dry_run=True)


def test_official_venue_calendar_can_supply_city_and_venue_without_title_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(
        key="necc-shanghai-exhibition-calendar",
        kind="html_table_calendar",
        url="https://example.test/calendar",
        event_name="",
        city_provenance="source",
        venue="国家会展中心（上海）",
        category="exhibition",
        default_status="probable",
        city_markers=[],
        match_terms=[],
        title_index=0,
        date_index=1,
        detail_index=2,
    )
    html = """
    <table><tr><th>展览名称</th><th>展览日期</th><th>展馆号</th></tr>
    <tr><td>2026鹰角嘉年华</td><td>2026-07-30 - 2026-08-02</td><td>2.1H、3H</td></tr></table>
    """
    monkeypatch.setattr(event_intelligence, "_fetch_html", lambda *_args, **_kwargs: html)

    entries = _fetch_html_table_calendar(source, 5)

    assert len(entries) == 1
    candidate = candidate_from_entry(entries[0], source)
    assert candidate is not None
    assert candidate["city"] == "上海"
    assert candidate["venue"] == "国家会展中心（上海）"
    assert candidate["event_start_date"] == date(2026, 7, 30)
    assert candidate["event_end_date"] == date(2026, 8, 2)
    assert candidate["event_status"] == "probable"


def test_bilibili_world_source_extracts_date_range_but_preserves_cross_source_venue_confidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(
        key="bilibili-world-2026",
        kind="bilibili_world",
        url="https://bw.example.test/en/2026/",
        allowed_script_hosts=["assets.example.test"],
        max_bytes=300000,
        event_year=2026,
        event_month=7,
        source_title="BW2026",
        event_name="2026哔哩哔哩上海国际数字娱乐动漫文化博览会",
        city_provenance="source",
        venue="国家会展中心（上海）",
        venue_confidence=0,
        city_markers=[],
        match_terms=[],
    )
    page_html = '<script src="https://assets.example.test/event/js/app.123.js"></script>'
    script_html = 'const dates=[{date:"7.10"},{date:"7.11"},{date:"7.12"}];'

    def fake_fetch(_source: dict, _timeout_seconds: int, *, url: str | None = None) -> str:
        return page_html if url == source["url"] else script_html

    monkeypatch.setattr(event_intelligence, "_fetch_html", fake_fetch)

    entries = _fetch_bilibili_world(source, 5)

    assert entries[0]["event_start_date"] == "2026-07-10"
    assert entries[0]["event_end_date"] == "2026-07-12"
    candidate = candidate_from_entry(entries[0], source)
    assert candidate is not None
    assert candidate["venue"] == "国家会展中心（上海）"
    assert candidate["venue_confidence"] == 0


def test_riot_global_schedule_stays_outside_shanghai_until_location_is_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(
        key="riot-global-esports-schedule",
        kind="riot_schedule",
        url="https://riot.example.test/schedule",
        max_bytes=3000000,
        calendar_year=2026,
        event_names=["MSI", "Worlds"],
        city="待确认",
        event_name="",
        city_markers=[],
        match_terms=[],
    )
    html = "<div>INTERNATIONAL MSI Jun 28 - Jul 12</div><div>INTERNATIONAL Worlds Oct 15 - Nov 14</div>"
    monkeypatch.setattr(event_intelligence, "_fetch_html", lambda *_args, **_kwargs: html)

    entries = _fetch_riot_schedule(source, 5)

    assert {(item["title"], item["event_start_date"], item["event_end_date"]) for item in entries} == {
        ("MSI 2026", "2026-06-28", "2026-07-12"),
        ("Worlds 2026", "2026-10-15", "2026-11-14"),
    }
    candidate = candidate_from_entry(entries[0], source)
    assert candidate is not None
    assert candidate["city"] == "待确认"


def test_comicup_official_event_page_extracts_explicit_dates_and_venue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(
        key="comicup-official-event-page",
        kind="allcpp_event",
        url="https://www.allcpp.cn/allcpp/event/event.do?event=7073",
        city="待确认",
        event_name="",
        city_markers=[],
        match_terms=[],
        category="acg",
    )
    html = """
    eventParam.eventName="COMICUPG08";
    eventParam.enterAddress="潭洲国际会展中心";
    eventParam.sDate="2026-08-22";
    eventParam.eDate="2026-08-23";
    eventParam.eventTag="CPG08|COMICUPG08|COMICUP";
    """
    monkeypatch.setattr(event_intelligence, "_fetch_html", lambda *_args, **_kwargs: html)

    entries = _fetch_allcpp_event(source, 5)

    assert entries[0]["title"] == "COMICUPG08"
    assert entries[0]["venue"] == "潭洲国际会展中心"
    candidate = candidate_from_entry(entries[0], source)
    assert candidate is not None
    assert candidate["event_start_date"] == date(2026, 8, 22)
    assert candidate["city"] == "待确认"
