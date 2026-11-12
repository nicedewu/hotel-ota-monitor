from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from src.db_writer import DBWriterError, _load_database_url, psycopg
from src.event_intelligence import EventIntelligenceError, candidate_from_entry, parse_event_dates, upsert_event_candidate
from src.event_corroboration import event_passage, visible_event_text
from src.qwen_search import QwenSearchError, search as qwen_search
from src.shanghai_government_search import ShanghaiGovernmentSearchError, search as shanghai_government_search


class EventDiscoveryError(RuntimeError):
    """A safe operator-facing reverse-discovery failure."""


def _load_config(path: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EventDiscoveryError(f"反向搜索配置读取失败：{type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise EventDiscoveryError("反向搜索配置必须是对象")
    return value


def _queries(stay_date: date) -> list[str]:
    cn = f"{stay_date.year}年{stay_date.month}月{stay_date.day}日"
    return [
        f"上海 {cn} 线下电竞赛事 职业联赛 总决赛 体育赛事 官方赛程 场馆",
        f"上海 {cn} 大型演唱会 音乐节 动漫展 游戏展 官方票务 场馆",
        f"上海 {cn} 专业展会 国际博览会 行业大会 官方日程 场馆",
    ]


def _shared_search_config(config: dict[str, Any], cursor: Any = None) -> dict[str, Any]:
    """Read all own hotels once; every query in the batch shares this context."""
    if not config.get("include_active_hotel_context"):
        return config
    if cursor is None:
        try:
            with psycopg.connect(_load_database_url(), row_factory=psycopg.rows.dict_row,
                                 options="-c default_transaction_read_only=on -c statement_timeout=5000") as conn:
                with conn.cursor() as context_cursor:
                    return _shared_search_config(config, context_cursor)
        except EventDiscoveryError:
            raise
        except Exception as exc:
            raise EventDiscoveryError(f"共享门店搜索上下文读取失败：{type(exc).__name__}") from exc
    cursor.execute(
        """
        SELECT h.hotel_key, h.hotel_name, h.city, h.district, r.venue, r.impact_level
        FROM hotels h
        LEFT JOIN hotel_venue_impact_rules r ON r.own_hotel_key = h.hotel_key
        WHERE h.hotel_role = 'own' AND h.is_active = TRUE
        ORDER BY h.hotel_key, r.venue
        """
    )
    hotels: dict[str, dict[str, Any]] = {}
    venues: set[str] = set()
    for row in cursor.fetchall():
        key = str(row["hotel_key"])
        hotel = hotels.setdefault(key, {"hotel_key": key, "hotel_name": row["hotel_name"],
                                       "hotel_type": "电竞酒店", "city": row.get("city"),
                                       "district": row.get("district"), "venues": []})
        venue, level = str(row.get("venue") or "").strip(), str(row.get("impact_level") or "unknown")
        if venue:
            hotel["venues"].append({"venue": venue, "impact_level": level})
            if level in {"high", "medium"}:
                venues.update(part.strip() for part in re.split(r"[、/；;|]+", venue) if part.strip())
    if not hotels:
        raise EventDiscoveryError("共享门店搜索上下文为空，未执行活动搜索")
    return {**config, "qwen": {**config.get("qwen", {}), "hotel_context": list(hotels.values())},
            "shared_venues": sorted(venues)}


def _search(
    config: dict[str, Any],
    query: str,
    limit: int,
    *,
    recent: bool = False,
    strategy: str | None = None,
    date_window: tuple[date, date] | None = None,
) -> list[dict[str, Any]]:
    """Use native Qwen search for source-backed structured event discovery."""

    try:
        return qwen_search(config, query, limit, recent=recent, strategy=strategy, date_window=date_window)
    except QwenSearchError as exc:
        raise EventDiscoveryError(str(exc)) from exc


def _trusted_source_level(result_url: str, config: dict[str, Any]) -> str:
    parsed = urlsplit(result_url)
    host = (parsed.hostname or "").lower()
    domains = config.get("trusted_source_domains")
    if parsed.scheme != "https" or not isinstance(domains, dict):
        return ""
    for domain, level in domains.items():
        suffix = str(domain).lower().strip().lstrip(".")
        if host == suffix or host.endswith(f".{suffix}"):
            candidate_level = str(level).lower().strip()
            return candidate_level if candidate_level in {"official", "authoritative"} else ""
    return ""


def _fetch_trusted_page(result_url: str, config: dict[str, Any]) -> str:
    """Read a whitelisted public event page, bounded and without elevating redirects."""

    level = _trusted_source_level(result_url, config)
    if not level:
        return ""
    request = Request(result_url, headers={"User-Agent": "hotel-demand-intelligence/1.0", "Accept": "text/html,application/xhtml+xml"})
    try:
        with urlopen(request, timeout=15) as response:
            final_url = str(response.geturl() or result_url)
            if not _trusted_source_level(final_url, config):
                return ""
            payload = response.read(500_001)
            if len(payload) > 500_000:
                return ""
            headers = getattr(response, "headers", None)
            get_charset = getattr(headers, "get_content_charset", None)
            charset = str(get_charset() or "") if callable(get_charset) else ""
    except Exception:
        return ""
    for encoding in (charset, "utf-8", "gb18030"):
        if not encoding:
            continue
        try:
            return visible_event_text(payload.decode(encoding))
        except (LookupError, UnicodeDecodeError):
            continue
    return ""


def _candidate_from_result(
    result: dict[str, Any],
    stay_date: date,
    config: dict[str, Any],
    *,
    category: str = "other",
    window_end: date | None = None,
    verify_trusted_page: bool = True,
) -> dict[str, Any] | None:
    """Keep search hits as discovery clues; only verified trusted pages gain source weight."""

    raw_offline_mode = str(result.get("offline_mode") or "").strip().lower()
    if ("线下" in raw_offline_mode and "线上" in raw_offline_mode) or raw_offline_mode == "hybrid":
        offline_mode = "hybrid"
    elif "线下" in raw_offline_mode or raw_offline_mode == "offline":
        offline_mode = "offline"
    elif "线上" in raw_offline_mode or raw_offline_mode == "online":
        offline_mode = "online"
    else:
        offline_mode = "unknown"
    base_source = {
        "key": "qwen-native-discovery",
        "city": "上海",
        "category": (str(result.get("category") or "other").strip().lower()
                     if str(result.get("category") or "other").strip().lower()
                     in {"esports", "sports", "performance", "acg", "exhibition", "other"} else "other"),
        "offline_mode": offline_mode,
        "source_level": "discovery",
        "default_status": "discovered",
        "city_markers": ["上海", "shanghai"],
        "match_terms": [],
    }
    trusted_level = _trusted_source_level(result.get("url", ""), config)
    page_text = _fetch_trusted_page(result["url"], config) if trusted_level and verify_trusted_page else ""
    passage = event_passage({"event_name": result.get("title"), "aliases": result.get("aliases") or []}, page_text)
    verified_start, verified_end, _ = parse_event_dates(passage)
    verified_shanghai = "上海" in page_text or "shanghai" in page_text.lower()
    proposed_venue = str(result.get("venue") or "").strip()
    verified_venue = proposed_venue if proposed_venue and proposed_venue.lower() in passage.lower() else ""
    window_end = window_end or stay_date
    if (
        trusted_level
        and verified_start is not None
        and verified_end is not None
        and verified_start <= window_end
        and verified_end >= stay_date
        and verified_shanghai
        and verified_venue
    ):
        base_source.update(
            {
                "key": "qwen-native-trusted-page-verification",
                "source_level": trusted_level,
                "default_status": (
                    "cancelled"
                    if any(marker in passage for marker in ("取消", "停止举办", "延期至待定"))
                    else "probable"
                ),
                "offline_mode": "offline" if verified_venue else "unknown",
            }
        )
        verified_entry = {
            **result,
            "summary": passage[:12_000],
            "event_start_date": verified_start.isoformat(),
            "event_end_date": verified_end.isoformat(),
            "date_confidence": 100,
            "venue": verified_venue or None,
            "venue_confidence": 100 if verified_venue else 0,
            "raw": {"search_result": result, "trusted_page_verified": True,
                    "field_validation": "event_bound_fields_v3", "event_passage": passage[:12_000]},
        }
        candidate = candidate_from_entry(verified_entry, base_source)
        if candidate is not None:
            candidate["source_observations"] = result.get("source_evidence") or []
            candidate["corroboration_min_domains"] = int(config.get("corroboration_min_domains", 2))
        return candidate
    discovery_entry = {
        **result,
        "date_confidence": min(int(result.get("date_confidence") or 50), 50),
        "venue_confidence": min(int(result.get("venue_confidence") or 50), 50),
    }
    candidate = candidate_from_entry(discovery_entry, base_source)
    if candidate is not None:
        candidate["source_observations"] = result.get("source_evidence") or []
        candidate["corroboration_min_domains"] = int(config.get("corroboration_min_domains", 2))
    return candidate


def _proactive_queries(
    anchor_date: date,
    days: int,
    config: dict[str, Any],
    *,
    include_priority: bool = False,
) -> list[tuple[date, date, str, str]]:
    if not 1 <= days <= 90:
        raise EventDiscoveryError("主动活动搜索天数必须在 1 到 90 之间")
    groups = config.get("query_groups") or config.get("proactive_query_groups")
    if not isinstance(groups, list) or not groups:
        raise EventDiscoveryError("主动活动搜索缺少查询分组")
    try:
        window_days = int(config.get("window_days", 30))
        max_queries = int(config.get("max_full_queries", 12))
    except (TypeError, ValueError) as exc:
        raise EventDiscoveryError("主动活动搜索配置无效") from exc
    max_queries = max(1, min(max_queries, 100))
    if not 7 <= window_days <= 31:
        raise EventDiscoveryError("活动搜索窗口必须在 7 到 31 天之间")
    normalized_groups = [
        (str(item.get("category") or "other").strip(), str(item.get("terms") or "").strip())
        for item in groups
        if isinstance(item, dict)
    ]
    normalized_groups = [(category, terms) for category, terms in normalized_groups if terms]
    if not normalized_groups:
        raise EventDiscoveryError("主动活动搜索查询分组为空")
    venue_hint = ""
    if config.get("shared_venues"):
        venue_hint = " 优先关注场馆：" + "、".join(config["shared_venues"])

    priority_groups = config.get("priority_query_groups") if include_priority else None
    normalized_priority_groups = [
        (str(item.get("category") or "other").strip(), str(item.get("terms") or "").strip())
        for item in priority_groups
        if isinstance(item, dict)
    ] if isinstance(priority_groups, list) else []
    normalized_priority_groups = [
        (category, terms) for category, terms in normalized_priority_groups if terms
    ]
    try:
        priority_window_days = int(config.get("priority_window_days", 14))
    except (TypeError, ValueError) as exc:
        raise EventDiscoveryError("近期高影响活动搜索配置无效") from exc
    if not 7 <= priority_window_days <= 21:
        raise EventDiscoveryError("近期高影响活动搜索窗口必须在 7 到 21 天之间")

    windows: list[tuple[date, date]] = []
    cursor = 1
    while cursor <= days:
        start = anchor_date + timedelta(days=cursor)
        end = anchor_date + timedelta(days=min(days, cursor + window_days - 1))
        windows.append((start, end))
        cursor += window_days

    result: list[tuple[date, date, str, str]] = []
    if normalized_priority_groups:
        priority_start = anchor_date + timedelta(days=1)
        priority_end = anchor_date + timedelta(days=min(days, priority_window_days))
        priority_date_text = (
            f"{priority_start.year}年{priority_start.month}月{priority_start.day}日"
            if priority_start == priority_end
            else f"{priority_start.year}年{priority_start.month}月{priority_start.day}日至"
                 f"{priority_end.month}月{priority_end.day}日"
        )
        for category, terms in normalized_priority_groups:
            result.append((priority_start, priority_end, category, f"上海 {priority_date_text} {terms}{venue_hint}"))
            if len(result) >= max_queries:
                return result
    for start, end in windows:
        date_text = (
            f"{start.year}年{start.month}月{start.day}日"
            if start == end
            else f"{start.year}年{start.month}月{start.day}日至{end.month}月{end.day}日"
        )
        for category, terms in normalized_groups:
            result.append((start, end, category, f"上海 {date_text} {terms}{venue_hint}"))
            if len(result) >= max_queries:
                return result
    return result


def _verify_candidate_sources(candidate: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """One dynamic search per event; validate individual pages before counting."""
    from src.event_corroboration import PUBLISHERS, domain, fetch_page, same_event, validate_page

    candidate_aliases = [str(item).strip() for item in candidate.get("aliases", []) if str(item).strip()]
    alias_text = " ".join(dict.fromkeys(candidate_aliases))[:300]
    query = (f"{candidate['event_name']} {alias_text} {candidate['event_start_date']}至{candidate['event_end_date']} "
             f"上海 {candidate.get('venue') or ''} 举办日期 场馆 票务")
    results = _search(config, query, 5, recent=False, strategy="max",
                      date_window=(candidate["event_start_date"], candidate["event_end_date"]))
    entry = dict(candidate.get("raw", {}).get("entry") or {})
    evidence = list(candidate.get("source_observations") or entry.get("source_evidence") or [])
    evidence.extend({"url": url} for url in entry.get("source_urls", []) if isinstance(url, str))
    verification_failures: list[str] = []
    government_settings = config.get("shanghai_government_search")
    if isinstance(government_settings, dict) and bool(government_settings.get("enabled", False)):
        try:
            government_limit = max(1, min(int(government_settings.get("results_per_event", 5)), 10))
            evidence.extend(
                shanghai_government_search(
                    config,
                    str(candidate.get("event_name") or "").strip(),
                    limit=government_limit,
                )
            )
        except (ShanghaiGovernmentSearchError, TypeError, ValueError) as exc:
            verification_failures.append(str(exc))
    for result in results:
        if same_event(candidate, result):
            evidence.extend(result.get("source_evidence") or [])
    by_url = {}
    for item in evidence:
        if isinstance(item, dict) and item.get("url"):
            previous = by_url.get(item["url"], {})
            by_url[item["url"]] = {**previous, **{key: value for key, value in item.items() if value}}
    # Spread page reads over publishers rather than consuming all slots on one portal.
    ordered, pending = [], list(by_url.values())
    pending.sort(key=lambda item: (not bool(_trusted_source_level(item["url"], config)),
                                    domain(item["url"]) not in PUBLISHERS))
    seen_domains = set()
    for item in pending:
        if domain(item["url"]) not in seen_domains:
            ordered.append(item)
            seen_domains.add(domain(item["url"]))
    ordered.extend(item for item in pending if item not in ordered)
    max_pages = max(3, min(int(config.get("verification_pages_per_event", 8)), 10))
    for item in ordered[:max_pages]:
        item["source_level"] = (_trusted_source_level(item["url"], config)
                                or ("authoritative" if item.get("discovered_via") == "shanghai_government_search" else "")
                                or ("media" if domain(item["url"]) in PUBLISHERS else "discovery"))
        body, fetch_status = fetch_page(item["url"])
        if body:
            item["validation"] = validate_page(
                candidate, item, body, origin="page",
                aliases=config.get("verification_venue_aliases"),
            )
        elif item.get("summary"):
            item["validation"] = validate_page(
                candidate, item, item["summary"], origin="search_excerpt",
                aliases=config.get("verification_venue_aliases"),
            )
        else:
            item["validation"] = {"supported": False, "reason": fetch_status}
    candidate = {**candidate, "source_observations": ordered}
    candidate["raw"] = {
        **candidate.get("raw", {}),
        "entry": {**entry, "source_evidence": ordered, "source_urls": list(by_url)},
        "verification_failures": verification_failures,
    }
    return candidate


def run_forward_discovery(
    config_path: str,
    *,
    anchor_date: date,
    days: int | None,
    dry_run: bool = False,
    recent: bool = False,
) -> dict[str, Any]:
    config = _load_config(config_path)
    if not bool(config.get("enabled", True)):
        return {"enabled": False, "queries": 0, "results": 0, "candidates": 0}
    if psycopg is None:
        raise EventDiscoveryError("缺少 psycopg 依赖，无法写入活动候选库")
    config = _shared_search_config(config)
    try:
        horizon_days = int(days if days is not None else config.get("horizon_days", 90))
        if not 1 <= horizon_days <= 90:
            raise ValueError
        result_limit = max(1, min(int(config.get("results_per_query", 5)), 10))
        trusted_page_limit = max(0, min(int(config.get("max_trusted_page_verifications", 8)), 20))
        corroboration_min_domains = int(config.get("corroboration_min_domains", 2))
        if not 2 <= corroboration_min_domains <= 10:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise EventDiscoveryError("Qwen 活动发现配置无效") from exc
    summary = {"enabled": True, "provider": "qwen_native", "queries": 0, "successful_queries": 0, "results": 0, "candidates": 0, "max_retries": 0, "verification_queries": 0, "verified_pages": 0, "failures": []}
    verification_candidates = {}
    trusted_pages_checked = 0
    qwen = config.get("qwen") if isinstance(config.get("qwen"), dict) else {}
    fallback_strategy = str(qwen.get("fallback_search_strategy") or "max").lower()
    primary_strategy = str(
        qwen.get("incremental_search_strategy" if recent else "full_search_strategy")
        or qwen.get("search_strategy")
        or ("turbo" if recent else "max")
    ).lower()
    try:
        minimum_sources = max(0, min(int(qwen.get("min_sources_before_max", 3)), result_limit))
    except (TypeError, ValueError) as exc:
        raise EventDiscoveryError("Qwen 来源阈值配置无效") from exc
    primary_queries = 0
    for stay_date, window_end, category, query in _proactive_queries(
        anchor_date,
        horizon_days,
        config,
        include_priority=not recent,
    ):
        primary_queries += 1
        summary["queries"] += 1
        try:
            results = _search(config, query, result_limit, recent=recent, strategy=primary_strategy,
                              date_window=(stay_date, window_end))
        except EventDiscoveryError as exc:
            summary["failures"].append(f"{stay_date} {category}: {exc}")
            continue
        summary["successful_queries"] += 1
        if len(results) < minimum_sources and fallback_strategy == "max" and primary_strategy != "max":
            summary["queries"] += 1
            summary["max_retries"] += 1
            try:
                fallback_results = _search(config, query, result_limit, recent=recent, strategy="max",
                                           date_window=(stay_date, window_end))
            except EventDiscoveryError as exc:
                summary["failures"].append(f"{stay_date} {category} max: {exc}")
            else:
                result_by_url = {item["url"]: item for item in results}
                result_by_url.update({item["url"]: item for item in fallback_results})
                results = list(result_by_url.values())[:result_limit]
        summary["results"] += len(results)
        for item in results:
            verify_trusted_page = bool(_trusted_source_level(item.get("url", ""), config)) and trusted_pages_checked < trusted_page_limit
            if verify_trusted_page:
                trusted_pages_checked += 1
            candidate = _candidate_from_result(
                item,
                stay_date,
                config,
                category=category,
                window_end=window_end,
                verify_trusted_page=verify_trusted_page,
            )
            if candidate is None:
                continue
            summary["candidates"] += 1
            if (not recent and candidate.get("source_confidence") == "discovery"
                and candidate.get("venue") and candidate.get("offline_mode") in {"offline", "hybrid"}
                and candidate.get("event_start_date") and candidate.get("event_end_date")
                and anchor_date < candidate["event_start_date"] <= anchor_date + timedelta(days=30)):
                verification_candidates.setdefault(candidate["event_identity_key"], candidate)
            if not dry_run:
                upsert_event_candidate(candidate)
    limit = max(0, min(int(config.get("max_verification_queries", 5)), 5))
    ranked = sorted(verification_candidates.values(), key=lambda item: (
        str(item.get("raw", {}).get("entry", {}).get("impact_level", "")).lower() not in {"high", "高", "极高"},
        item["event_start_date"], item["event_name"],
    ))
    for candidate in ranked[:limit]:
        summary["queries"] += 1
        summary["verification_queries"] += 1
        try:
            enriched = _verify_candidate_sources(candidate, config)
        except EventDiscoveryError as exc:
            summary["failures"].append(f"核验 {candidate['event_name']}: {exc}")
            continue
        summary["successful_queries"] += 1
        summary["verified_pages"] += sum(bool(item.get("validation", {}).get("supported")) for item in enriched["source_observations"])
        if not dry_run:
            upsert_event_candidate(enriched)
    if primary_queries and summary["successful_queries"] == 0:
        raise EventDiscoveryError("Qwen 活动发现全部查询失败")
    return summary


def _due_anomalies(cursor: Any, *, max_attempts: int) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT d.id, d.anomaly_key, d.stay_date, d.score, d.signal_count, d.total_net_pickup
        FROM demand_anomalies d
        WHERE d.stay_date BETWEEN current_date + 1 AND current_date + 30
          AND d.score >= 70 AND d.signal_count >= 2
          AND NOT EXISTS (
              SELECT 1 FROM demand_event_attributions a
              WHERE a.anomaly_id = d.id
                AND a.attribution_score >= 80
                AND a.source_confidence IN ('official', 'authoritative')
          )
          AND (
              SELECT count(*) FROM event_discovery_runs r WHERE r.anomaly_id = d.id
          ) < %s
        ORDER BY d.score DESC, d.stay_date
        """,
        (max_attempts,),
    )
    return [dict(row) for row in cursor.fetchall()]


def _already_due(cursor: Any, anomaly_id: int) -> bool:
    cursor.execute(
        """
        SELECT finished_at, attempt_no FROM event_discovery_runs
        WHERE anomaly_id = %s ORDER BY finished_at DESC, id DESC LIMIT 1
        """,
        (anomaly_id,),
    )
    row = cursor.fetchone()
    if not row:
        return True
    finished = row["finished_at"]
    attempt = int(row["attempt_no"])
    age = datetime.now(finished.tzinfo) - finished
    wait_days = 1 if attempt == 1 else 2 if attempt == 2 else 4 if attempt == 3 else 7
    return age >= timedelta(days=wait_days)


def _record_run(cursor: Any, anomaly_id: int, attempt_no: int, query_count: int, result_count: int, candidate_count: int, status: str, error_summary: str = "") -> None:
    cursor.execute(
        """
        INSERT INTO event_discovery_runs (
            anomaly_id, provider, attempt_no, query_count, result_count,
            candidate_count, status, error_summary, finished_at
        ) VALUES (%s, 'qwen_native', %s, %s, %s, %s, %s, NULLIF(%s, ''), now())
        """,
        (anomaly_id, attempt_no, query_count, result_count, candidate_count, status, error_summary[:500]),
    )


def run_reverse_discovery(config_path: str, *, dry_run: bool = False) -> dict[str, Any]:
    config = _load_config(config_path)
    if not bool(config.get("enabled", True)):
        return {"enabled": False, "processed": 0, "queries": 0, "candidates": 0}
    if psycopg is None:
        raise EventDiscoveryError("缺少 psycopg 依赖，无法读取需求异常")
    max_attempts = max(1, min(int(config.get("max_attempts", 5)), 5))
    result_limit = max(1, min(int(config.get("results_per_query", 10)), 10))
    max_anomalies = max(1, min(int(config.get("max_reverse_anomalies", 3)), 10))
    max_queries = max(1, min(int(config.get("max_reverse_queries", 6)), 12))
    summary = {"enabled": True, "processed": 0, "queries": 0, "results": 0, "candidates": 0, "stay_dates": [], "failures": []}
    try:
        with psycopg.connect(_load_database_url(), row_factory=psycopg.rows.dict_row) as connection:
            with connection.cursor() as cursor:
                config = _shared_search_config(config, cursor)
                anomalies = _due_anomalies(cursor, max_attempts=max_attempts)[:max_anomalies]
                search_cache: dict[tuple[str, bool], list[dict[str, Any]]] = {}
                for anomaly in anomalies:
                    if not _already_due(cursor, int(anomaly["id"])):
                        continue
                    attempt_no = 1
                    cursor.execute("SELECT count(*) AS count FROM event_discovery_runs WHERE anomaly_id = %s", (anomaly["id"],))
                    attempt_no += int(cursor.fetchone()["count"])
                    query_list = _queries(anomaly["stay_date"])
                    if attempt_no > 1:
                        query_list = query_list[:2]
                    remaining_queries = max_queries - summary["queries"]
                    selected_queries = []
                    for query in query_list:
                        if (query, attempt_no > 1) in search_cache:
                            selected_queries.append(query)
                        elif remaining_queries > 0:
                            selected_queries.append(query)
                            remaining_queries -= 1
                    query_list = selected_queries
                    if not query_list:
                        continue
                    result_count = candidate_count = 0
                    executed_queries = 0
                    error_summary = ""
                    try:
                        for query in query_list:
                            cache_key = (query, attempt_no > 1)
                            if cache_key not in search_cache:
                                executed_queries += 1
                                search_cache[cache_key] = _search(
                                    config, query, result_limit, recent=attempt_no > 1,
                                    date_window=(anomaly["stay_date"], anomaly["stay_date"]))
                            results = search_cache[cache_key]
                            result_count += len(results)
                            for result in results:
                                candidate = _candidate_from_result(result, anomaly["stay_date"], config)
                                if candidate is None:
                                    continue
                                candidate_count += 1
                                if not dry_run:
                                    upsert_event_candidate(candidate)
                    except (EventDiscoveryError, EventIntelligenceError) as exc:
                        error_summary = str(exc)
                    status = "success" if not error_summary else "failed"
                    if not dry_run:
                        _record_run(cursor, int(anomaly["id"]), attempt_no, executed_queries, result_count, candidate_count, status, error_summary)
                    summary["processed"] += 1
                    summary["queries"] += executed_queries
                    summary["results"] += result_count
                    summary["candidates"] += candidate_count
                    summary["stay_dates"].append(anomaly["stay_date"].isoformat())
                    if error_summary:
                        summary["failures"].append(f"{anomaly['stay_date']}: {error_summary}")
                return summary
    except DBWriterError as exc:
        raise EventDiscoveryError(str(exc)) from exc
    except EventDiscoveryError:
        raise
    except Exception as exc:
        raise EventDiscoveryError(f"反向活动搜索失败：{type(exc).__name__}") from exc
