import json
from datetime import date, datetime, timedelta, timezone

from src.event_corroboration import assess, same_event, validate_page, visible_event_text, _public_url
from src.event_intelligence import _candidate_source_rows
from src.future_event_alert import _merge_event_layers


def event():
    return {"event_name": "2026城市电竞总决赛", "event_start_date": date(2026, 9, 12),
            "event_end_date": date(2026, 9, 13), "venue": "东方体育中心"}


def test_list_page_cannot_support_fields_from_different_events():
    page = visible_event_text("""<h1>2026上海活动清单</h1>
        <h2>上海花车巡游</h2><p>活动时间：2026年9月13日-10月6日</p><p>活动地点：上海各区</p>
        <h2>青浦文旅节</h2><p>活动时间：2026年9月28日-10月28日</p><p>活动地点：百联奥特莱斯品牌广场二期中心广场</p>
        <h2>乡约古镇·乐游青溪</h2><p>活动时间：2026年9月19日-10月18日</p><p>活动地点：奉贤青村镇</p>""")
    candidate = {"event_name": "乡约古镇·乐游青溪", "event_start_date": date(2026, 9, 13),
                 "event_end_date": date(2026, 10, 6), "venue": "百联奥特莱斯品牌广场二期中心广场"}
    validation = validate_page(candidate, {"title": candidate["event_name"]}, page, origin="page")
    assert not validation["supported"]
    correct = {**candidate, "event_start_date": date(2026, 9, 19), "event_end_date": date(2026, 10, 18), "venue": "奉贤青村镇"}
    valid = validate_page(correct, {"title": correct["event_name"]}, page, origin="page")
    assert valid["overall_dates"] and valid["matched_venues"] == ["奉贤青村镇"]


def source(url, extra=""):
    text = "2026城市电竞总决赛于2026年9月12日至13日在上海东方体育中心举行。" + extra
    return {"source_url": url, "raw": {"source_validation": validate_page(event(),
            {"url": url, "title": event()["event_name"]}, text, origin="page")}}


def test_date_venue_evidence_must_be_on_actual_page():
    valid = source("https://donews.com/1")["raw"]["source_validation"]
    assert valid["supported"]
    wrong = validate_page(event(), {"title": event()["event_name"]},
                          "2026城市电竞总决赛于9月19日至20日在上海东方体育中心举行。", origin="page")
    assert not wrong["supported"]
    assert wrong["conflict"] and wrong["reason"] == "date_conflict"


def test_multi_venue_event_combines_field_evidence_across_publishers():
    candidate = {
        "event_name": "2026无畏契约全球冠军赛",
        "aliases": ["VALORANT Champions Shanghai", "Champions Shanghai"],
        "event_start_date": date(2026, 9, 24),
        "event_end_date": date(2026, 10, 18),
        "venue": "静安体育中心（小组赛及淘汰赛）、梅赛德斯-奔驰文化中心（总决赛）",
    }
    venue_aliases = {
        "静安体育中心": ["Jing'an Sports Center", "Jingan Sports Center"],
        "梅赛德斯-奔驰文化中心": ["Mercedes-Benz Arena", "Mercedes Benz Arena"],
    }
    pages = [
        (
            "https://thepaper.cn/vct",
            "无畏契约全球冠军赛将于2026年9月24日至10月18日在上海举行。"
            "小组赛及淘汰赛在静安体育中心，总决赛在梅赛德斯-奔驰文化中心。"
            "赛事聚集全球顶尖战队并公布观众入场安排。" * 8,
            "media",
        ),
        (
            "https://valorantesports.com/vct",
            "VALORANT Champions Shanghai 2026 starts at Jing'an Sports Center in Shanghai, "
            "with the final weekend at Mercedes-Benz Arena. Ticket and live audience details are official. " * 8,
            "official",
        ),
        (
            "https://donews.com/vct",
            "2026无畏契约全球冠军赛于9月24日至10月18日在上海举办，"
            "赛事阶段安排与静安体育中心周边观赛交通提示已经发布。" * 8,
            "media",
        ),
    ]
    rows = []
    for url, text, level in pages:
        evidence = {"url": url, "title": "无畏契约全球冠军赛", "source_level": level}
        validation = validate_page(candidate, evidence, text, origin="page", aliases=venue_aliases)
        rows.append({"source_url": url, "raw": {"source_validation": validation}})

    result = assess(candidate, rows)

    assert result["eligible"] and result["count"] == 3
    assert result["field_coverage"]["complete"]
    assert result["field_coverage"]["overall_dates"] == ["donews.com", "thepaper.cn"]
    assert set(result["field_coverage"]["venues"]) == {"静安体育中心", "梅赛德斯-奔驰文化中心"}


def test_same_event_accepts_abbreviated_media_title_but_rejects_another_final():
    lpl = {"event_name": "2026 LPL总决赛暨英雄联盟15周年盛典"}
    assert same_event(lpl, {"title": "2026 LPL总决赛暨15周年盛典将于上海举行"})
    assert not same_event(lpl, {"title": "2026王者荣耀全国总决赛在上海举行"})


def test_model_aggregate_cannot_be_counted_as_independent_evidence():
    rows = [{"source_url": "https://" + host + "/1", "raw": {"observed": event()}}
            for host in ["donews.com", "17173.com", "sohu.com"]]
    assert assess(event(), rows)["count"] == 0


def test_copied_articles_and_same_domain_count_once():
    rows = [source("https://" + host + "/1") for host in ["donews.com", "17173.com", "www.sohu.com", "m.sohu.com"]]
    assert assess(event(), rows)["count"] == 1


def test_three_distinct_supported_publishers_can_qualify_but_conflict_blocks():
    rows = [source("https://donews.com/1", "主办方发布观众入场规则，现场将设互动体验区。" * 12),
            source("https://17173.com/2", "参赛阵容已揭晓，队伍将争夺本届冠军。" * 12),
            source("https://sohu.com/3", "交通提示建议观众乘地铁到场，票务平台提供实名购票。" * 12)]
    result = assess(event(), rows)
    assert result["eligible"] and result["count"] == 3
    rows[0]["raw"]["source_validation"]["conflict"] = True
    assert not assess(event(), rows)["eligible"]


def test_two_independent_pages_require_both_dates_and_one_professional_source():
    rows = [source("https://donews.com/1", "主办方发布观众入场规则。" * 12),
            source("https://17173.com/2", "赛事公布参赛阵容和现场安排。" * 12)]
    assert assess(event(), rows, min_domains=2)["eligible"]
    rows[1]["raw"]["source_validation"]["overall_dates"] = False
    assert not assess(event(), rows, min_domains=2)["eligible"]


def test_search_excerpt_does_not_qualify_as_media_page():
    rows = [source("https://donews.com/1", "观众入场规则。" * 12),
            source("https://17173.com/2", "赛事参赛阵容。" * 12)]
    rows[1]["raw"]["source_validation"]["origin"] = "search_excerpt"
    assert not assess(event(), rows, min_domains=2)["eligible"]


def test_expired_evidence_and_forum_do_not_qualify():
    row = source("https://donews.com/1")
    row["raw"]["source_validation"]["checked_at"] = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
    assert assess(event(), [row])["count"] == 0
    assert assess(event(), [source("https://hupu.com/1")])["count"] == 0


def test_bilibili_ticket_page_is_allowed_but_general_content_remains_excluded():
    ticket = source("https://show.bilibili.com/platform/detail.html?id=123")
    general = source("https://www.bilibili.com/read/cv123")
    assert ticket["raw"]["source_validation"]["authorized_ticketing"]
    assert not ticket["raw"]["source_validation"]["excluded"]
    assert general["raw"]["source_validation"]["excluded"]


def test_source_provenance_preserves_secondary_official_validation():
    ev = {**event(), "source_url": "https://donews.com/1", "source_confidence": "discovery",
          "source_observations": [{"url": "https://official.example/2", "title": "真实页面标题",
                                   "source_level": "authoritative",
                                   "validation": {"supported": True, "rule": "test"}}]}
    rows = _candidate_source_rows(ev, {})
    assert len(rows) == 2
    official = next(row for row in rows if row["source_url"] == "https://official.example/2")
    assert official["source_level"] == "authoritative"
    assert json.loads(official["raw"])["source_validation"]["rule"] == "test"


def test_official_cancellation_suppresses_reference_and_duplicate_names_merge():
    candidate = {**event(), "source_confidence": "corroborated"}
    cancelled = {**event(), "canonical_status": "cancelled"}
    assert _merge_event_layers([], [candidate], [cancelled]) == []
    duplicate = {**candidate, "event_name": "2026 城市电竞总决赛"}
    assert len(_merge_event_layers([], [candidate, duplicate], [])) == 1


def test_private_and_malformed_urls_rejected():
    assert not _public_url("https://127.0.0.1/a")
    assert not _public_url("https://example.com:bad/a")
    assert not _public_url("file:///etc/passwd")
