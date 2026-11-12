from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from src.db_writer import DBWriterError, _load_database_url, psycopg


VALID_SOURCE_LEVELS = {"official", "authoritative", "corroborated", "media", "discovery"}
EVIDENCE_SOURCE_LEVELS = VALID_SOURCE_LEVELS - {"corroborated"}
VALID_CANDIDATE_STATUSES = {"discovered", "probable", "confirmed", "promoted", "cancelled"}
VALID_DEMAND_LEVELS = {"high", "medium", "normal"}
BUSINESS_SOURCE_LEVELS = {"official", "authoritative"}
BUSINESS_CANDIDATE_STATUSES = {"probable", "confirmed", "promoted"}
VALID_VERIFICATION_STATES = {"unverified", "corroborated", "verified", "conflict", "stale"}
SOURCE_RANKS = {
    "discovery": 0,
    "media": 1,
    "corroborated": 2,
    "authoritative": 3,
    "official": 4,
    "manual": 5,
}
_COMPOUND_PUBLIC_SUFFIXES = {"com.cn", "net.cn", "org.cn", "gov.cn"}
MONTH_NUMBERS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
SHORT_MONTH_NUMBERS = {name[:3]: number for name, number in MONTH_NUMBERS.items()}
MONTH_NAME_NUMBERS = {**MONTH_NUMBERS, **SHORT_MONTH_NUMBERS}
DEFAULT_HTML_MAX_BYTES = 1_000_000
MAX_HTML_MAX_BYTES = 3_000_000


class EventIntelligenceError(RuntimeError):
    """A safe, operator-facing event-intelligence failure."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalise_name(value: Any) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", _text(value).lower())


def _parse_iso_date(value: Any) -> date | None:
    text = _text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _parse_published_at(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_event_dates(text: str, fallback_year: int | None = None) -> tuple[date | None, date | None, int]:
    """Parse only explicit date statements and return a confidence score.

    Missing or ambiguous dates intentionally remain empty candidates rather than
    being guessed from an article publication date.
    """

    value = _text(text)
    if not value:
        return None, None, 0

    iso = re.search(
        r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\s*(?:至|到|[-–—~]|to)\s*"
        r"(?:(20\d{2})[-/.])?(\d{1,2})[-/.](\d{1,2})",
        value,
        re.IGNORECASE,
    )
    if iso:
        start_year, start_month, start_day, end_year, end_month, end_day = iso.groups()
        start = _safe_date(int(start_year), int(start_month), int(start_day))
        end = _safe_date(int(end_year or start_year), int(end_month), int(end_day))
        if start and end and end >= start:
            return start, end, 100

    chinese = re.search(
        r"(?:(20\d{2})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日?\s*"
        r"(?:至|到|[-–—~])\s*(?:(\d{1,2})\s*月\s*)?(\d{1,2})\s*日?",
        value,
    )
    if chinese:
        explicit_year, start_month, start_day, end_month, end_day = chinese.groups()
        year_match = re.search(r"20\d{2}", value)
        year = int(explicit_year or (year_match.group(0) if year_match else fallback_year or 0))
        if year:
            start = _safe_date(year, int(start_month), int(start_day))
            end = _safe_date(year, int(end_month or start_month), int(end_day))
            if start and end and end >= start:
                return start, end, 100 if explicit_year or year_match else 70

    english = re.search(
        r"\b("
        + "|".join(MONTH_NUMBERS)
        + r")\s+(\d{1,2})\s*(?:[-–—]|to)\s*(\d{1,2})(?:,?\s*(20\d{2})\b)?",
        value,
        re.IGNORECASE,
    )
    if english:
        month_name, start_day, end_day, explicit_year = english.groups()
        year_match = re.search(r"20\d{2}", value)
        year = int(explicit_year or (year_match.group(0) if year_match else fallback_year or 0))
        start = _safe_date(year, MONTH_NUMBERS[month_name.lower()], int(start_day))
        end = _safe_date(year, MONTH_NUMBERS[month_name.lower()], int(end_day))
        if start and end and end >= start:
            return start, end, 100 if explicit_year or year_match else 70

    return None, None, 0


def _candidate_key(candidate: dict[str, Any]) -> str:
    start = candidate.get("event_start_date")
    year = start.year if isinstance(start, date) else ""
    parts = (
        _normalise_name(candidate["event_name"]),
        _normalise_name(candidate["city"]),
        _normalise_name(candidate.get("venue")),
        str(year),
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _merged_candidate_city(existing: object, incoming: object) -> str:
    current = _text(existing)
    observed = _text(incoming)
    return observed if current in {"", "待确认"} and observed not in {"", "待确认"} else current


def _event_identity_key(candidate: dict[str, Any]) -> str:
    """Return an identity that survives a verified date or venue correction.

    The legacy candidate key is retained for backwards compatibility and audit.
    This identity deliberately excludes date and venue, which are observations
    that may be corrected by a newer official notice.
    """

    start = candidate.get("event_start_date")
    year = start.year if isinstance(start, date) else ""
    parts = (
        _normalise_name(candidate["event_name"]),
        _normalise_name(candidate["city"]),
        _normalise_name(candidate.get("category") or "other"),
        str(year),
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _source_rank(source_level: Any) -> int:
    return SOURCE_RANKS.get(_text(source_level).lower(), -1)


def _registrable_domain(url: Any) -> str:
    host = (urlsplit(_text(url)).hostname or "").lower().strip(".")
    if not host:
        return ""
    labels = [part for part in host.split(".") if part]
    if len(labels) <= 2:
        return host
    suffix = ".".join(labels[-2:])
    if suffix in _COMPOUND_PUBLIC_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return suffix


def _fields_conflict(existing: dict[str, Any], incoming: dict[str, Any]) -> list[str]:
    conflicts: list[str] = []
    for field in ("event_start_date", "event_end_date", "venue"):
        before = existing.get(field)
        after = incoming.get(field)
        if before is not None and after is not None and _text(before) != _text(after):
            conflicts.append(field)
    return conflicts


def _incoming_can_replace(existing: dict[str, Any], incoming: dict[str, Any]) -> bool:
    """Allow only a strictly more authoritative source to replace known fields.

    Equal-rank disagreements must enter manual verification rather than being
    resolved by write order.  Missing incumbent fields may always be filled.
    """

    if not any(existing.get(field) is not None for field in ("event_start_date", "event_end_date", "venue")):
        return True
    return _source_rank(incoming.get("source_confidence")) > _source_rank(existing.get("source_confidence"))


def _verification_state(candidate: dict[str, Any], *, conflict: bool) -> str:
    if conflict:
        return "conflict"
    if (
        _source_rank(candidate.get("source_confidence")) >= SOURCE_RANKS["authoritative"]
        and _text(candidate.get("event_status")) in BUSINESS_CANDIDATE_STATUSES
        and int(candidate.get("date_confidence") or 0) >= 90
        and int(candidate.get("venue_confidence") or 0) >= 90
        and bool(_text(candidate.get("venue")))
        and isinstance(candidate.get("event_start_date"), date)
        and isinstance(candidate.get("event_end_date"), date)
    ):
        return "verified"
    return "unverified"


def _merged_event_status(existing: Any, incoming: Any) -> str:
    before = _text(existing)
    after = _text(incoming)
    if after == "cancelled":
        return "cancelled"
    if before == "promoted":
        return "promoted"
    if after in {"promoted", "confirmed"}:
        return after
    if before == "cancelled":
        return "cancelled"
    if before == "confirmed":
        return before
    if after == "probable" or before == "probable":
        return "probable"
    return "discovered"


def _merged_aliases(existing: Any, incoming: list[str]) -> list[str]:
    before = existing if isinstance(existing, list) else []
    result: list[str] = []
    seen: set[str] = set()
    for value in [*before, *incoming]:
        text = _text(value)
        key = _normalise_name(text)
        if text and key and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _candidate_observation(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_name": candidate.get("event_name"),
        "event_start_date": candidate.get("event_start_date"),
        "event_end_date": candidate.get("event_end_date"),
        "venue": candidate.get("venue"),
        "event_status": candidate.get("event_status"),
        "source_level": candidate.get("source_confidence"),
        "source_url": candidate.get("source_url"),
        "published_at": candidate.get("published_at"),
    }


def _occurrence_key(event_name: str, city: str, venue: str, event_start_date: date) -> str:
    """Return the stable business identity for one dated, in-person occurrence.

    Candidate keys intentionally remain a staging identity.  A recurring event
    can reuse a candidate while each dated occurrence remains distinct here.
    """

    parts = (
        _normalise_name(event_name),
        _normalise_name(city),
        _normalise_name(venue),
        event_start_date.isoformat(),
    )
    if not all(parts):
        raise EventIntelligenceError("正式活动缺少名称、城市、场馆或开始日期")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _candidate_aliases(event_name: str, source: dict[str, Any], entry: dict[str, Any]) -> list[str]:
    entry_aliases = entry.get("aliases")
    candidates = [
        event_name,
        *source.get("aliases", []),
        *(entry_aliases if isinstance(entry_aliases, list) else []),
    ]
    seen: set[str] = set()
    result: list[str] = []
    for item in candidates:
        item = _text(item)
        key = _normalise_name(item)
        if item and key and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def candidate_from_entry(entry: dict[str, Any], source: dict[str, Any]) -> dict[str, Any] | None:
    title = _text(entry.get("title"))
    summary = _text(entry.get("summary"))
    body = "\n".join(part for part in (title, summary) if part)
    if not body:
        return None
    source_level = _text(source.get("source_level")) or "discovery"
    if source_level not in VALID_SOURCE_LEVELS:
        raise EventIntelligenceError(f"来源 {source.get('key') or '<unknown>'} 的 source_level 无效")
    city = _text(entry.get("city")) or _text(source.get("city")) or "上海"
    city_provenance = _text(source.get("city_provenance")) or "entry"
    if city_provenance not in {"entry", "source"}:
        raise EventIntelligenceError(f"来源 {source.get('key') or '<unknown>'} 的 city_provenance 无效")
    if city_provenance == "source" and (
        source_level not in {"official", "authoritative"} or not _text(source.get("city"))
    ):
        raise EventIntelligenceError(
            f"来源 {source.get('key') or '<unknown>'} 只有 official/authoritative 的固定城市来源可跳过正文城市校验"
        )
    city_markers = [_text(marker) for marker in source.get("city_markers", []) if _text(marker)]
    event_terms = [_text(term) for term in source.get("match_terms", []) if _text(term)]
    body_key = body.lower()
    if city_provenance != "source" and city_markers and not any(
        marker.lower() in body_key or marker.lower() in _text(entry.get("venue")).lower()
        for marker in city_markers
    ):
        if source_level != "discovery":
            return None
        city = "待确认"
    if event_terms and not any(term.lower() in body_key for term in event_terms):
        return None

    published_at = _parse_published_at(entry.get("published_at"))
    start_date = _parse_iso_date(entry.get("event_start_date"))
    end_date = _parse_iso_date(entry.get("event_end_date"))
    if start_date and end_date and end_date >= start_date:
        try:
            date_confidence = int(entry.get("date_confidence", 100))
        except (TypeError, ValueError):
            date_confidence = 100
        date_confidence = max(0, min(100, date_confidence))
    else:
        start_date, end_date, date_confidence = parse_event_dates(
            body,
            fallback_year=published_at.year if published_at else None,
        )
    event_name = _text(source.get("event_name")) or title
    status = _text(source.get("default_status")) or "discovered"
    if status not in VALID_CANDIDATE_STATUSES:
        raise EventIntelligenceError(f"来源 {source.get('key') or '<unknown>'} 的 default_status 无效")
    source_url = _text(entry.get("url"))
    if not source_url.startswith(("https://", "http://")):
        return None
    venue = _text(entry.get("venue")) or _text(source.get("venue"))
    raw_venue_confidence = entry.get("venue_confidence", source.get("venue_confidence"))
    if raw_venue_confidence is None:
        venue_confidence = 100 if venue else 0
    else:
        try:
            venue_confidence = int(raw_venue_confidence)
        except (TypeError, ValueError) as exc:
            raise EventIntelligenceError(
                f"来源 {source.get('key') or '<unknown>'} 的 venue_confidence 无效"
            ) from exc
        venue_confidence = max(0, min(100, venue_confidence))
    candidate = {
        "candidate_key": "",
        "event_name": event_name,
        "aliases": _candidate_aliases(event_name, source, entry),
        "city": city,
        "venue": venue or None,
        "event_start_date": start_date,
        "event_end_date": end_date,
        "category": _text(source.get("category")) or "other",
        "event_status": status,
        "offline_mode": _text(source.get("offline_mode")) or "unknown",
        "source_confidence": source_level,
        "date_confidence": date_confidence,
        "venue_confidence": venue_confidence,
        "source_key": _text(source.get("key")),
        "source_url": source_url,
        "source_title": title,
        "published_at": published_at,
        "raw": {
            "entry": entry,
            "source": {key: value for key, value in source.items() if key != "url"},
            "date_parser": "explicit_only_v2",
        },
    }
    candidate["candidate_key"] = _candidate_key(candidate)
    candidate["event_identity_key"] = _event_identity_key(candidate)
    return candidate


def _fetch_json(url: str, timeout_seconds: int) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": "hotel-demand-intelligence/1.0"})
    for attempt in range(2):
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                payload = response.read().decode("utf-8")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            retryable = not isinstance(exc, HTTPError) or exc.code == 429 or 500 <= exc.code < 600
            if attempt == 0 and retryable:
                time.sleep(1)
                continue
            raise EventIntelligenceError(f"活动来源请求失败：{type(exc).__name__}") from exc
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise EventIntelligenceError("活动来源返回的 JSON 无法解析") from exc
    if not isinstance(data, dict):
        raise EventIntelligenceError("活动来源 JSON 顶层必须是对象")
    return data


def _fetch_steam_news(source: dict[str, Any], timeout_seconds: int) -> list[dict[str, Any]]:
    appid = int(source.get("appid") or 0)
    if appid <= 0:
        raise EventIntelligenceError(f"来源 {source.get('key') or '<unknown>'} 缺少 appid")
    query = urlencode(
        {
            "appid": appid,
            "count": int(source.get("count") or 100),
            "maxlength": int(source.get("maxlength") or 10000),
            "format": "json",
        }
    )
    payload = _fetch_json(
        f"https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/?{query}",
        timeout_seconds,
    )
    appnews = payload.get("appnews") if isinstance(payload.get("appnews"), dict) else {}
    items = appnews.get("newsitems") if isinstance(appnews.get("newsitems"), list) else []
    entries: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        timestamp = item.get("date")
        published_at = (
            datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
            if isinstance(timestamp, int)
            else None
        )
        entries.append(
            {
                "title": _text(item.get("title")),
                "summary": _text(item.get("contents")),
                "url": _text(item.get("url")),
                "published_at": published_at.isoformat() if published_at else "",
                "raw": item,
            }
        )
    return entries


def _rss_text(item: ElementTree.Element, name: str) -> str:
    element = item.find(name)
    return _text(element.text if element is not None else "")


def _fetch_rss(source: dict[str, Any], timeout_seconds: int) -> list[dict[str, Any]]:
    url = _text(source.get("url"))
    if not url.startswith(("https://", "http://")):
        raise EventIntelligenceError(f"来源 {source.get('key') or '<unknown>'} 缺少合法 RSS URL")
    request = Request(url, headers={"User-Agent": "hotel-demand-intelligence/1.0"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            root = ElementTree.fromstring(response.read())
    except (HTTPError, URLError, TimeoutError, OSError, ElementTree.ParseError) as exc:
        raise EventIntelligenceError(f"RSS 来源读取失败：{type(exc).__name__}") from exc
    entries: list[dict[str, Any]] = []
    for item in root.findall(".//item"):
        entries.append(
            {
                "title": _rss_text(item, "title"),
                "summary": _rss_text(item, "description"),
                "url": _rss_text(item, "link"),
                "published_at": _rss_text(item, "pubDate"),
                "raw": {
                    "title": _rss_text(item, "title"),
                    "link": _rss_text(item, "link"),
                },
            }
        )
    return entries


def _configured_html_max_bytes(source: dict[str, Any]) -> int:
    try:
        limit = int(source.get("max_bytes") or DEFAULT_HTML_MAX_BYTES)
    except (TypeError, ValueError) as exc:
        raise EventIntelligenceError(f"来源 {source.get('key') or '<unknown>'} 的 max_bytes 无效") from exc
    if not 1024 <= limit <= MAX_HTML_MAX_BYTES:
        raise EventIntelligenceError(
            f"来源 {source.get('key') or '<unknown>'} 的 max_bytes 必须在 1024 到 {MAX_HTML_MAX_BYTES} 之间"
        )
    return limit


def _source_url(source: dict[str, Any], key: str = "url") -> str:
    value = _text(source.get(key))
    if not value and key == "url":
        template = _text(source.get("url_template"))
        if template:
            try:
                value = template.format(year=date.today().year)
            except (KeyError, ValueError) as exc:
                raise EventIntelligenceError(
                    f"来源 {source.get('key') or '<unknown>'} 的 url_template 无效"
                ) from exc
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise EventIntelligenceError(f"来源 {source.get('key') or '<unknown>'} 缺少合法 HTTPS {key}")
    return value


def _fetch_html(source: dict[str, Any], timeout_seconds: int, *, url: str | None = None) -> str:
    source_url = url or _source_url(source)
    request = Request(source_url, headers={"User-Agent": "hotel-demand-intelligence/1.0"})
    limit = _configured_html_max_bytes(source)
    response_headers: Any = None
    failure: EventIntelligenceError | None = None
    for attempt in range(2):
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                payload = response.read(limit + 1)
                response_headers = getattr(response, "headers", None)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            retryable = not isinstance(exc, HTTPError) or exc.code == 429 or 500 <= exc.code < 600
            if attempt == 0 and retryable:
                time.sleep(1)
                continue
            failure = EventIntelligenceError(f"网页活动来源读取失败：{type(exc).__name__}")
            break
    if failure is None:
        if len(payload) > limit:
            failure = EventIntelligenceError("网页活动来源内容超过安全上限")
        else:
            charset = ""
            if response_headers is not None:
                get_charset = getattr(response_headers, "get_content_charset", None)
                if callable(get_charset):
                    charset = _text(get_charset())
            for encoding in (charset, "utf-8", "gb18030"):
                if not encoding:
                    continue
                try:
                    static_html = payload.decode(encoding)
                    return static_html
                except (LookupError, UnicodeDecodeError):
                    continue
            failure = EventIntelligenceError("网页活动来源文本编码无法识别")
    if source.get("dynamic_render"):
        try:
            return _fetch_html_with_chrome(source, source_url, "")
        except EventIntelligenceError as chrome_error:
            raise EventIntelligenceError(f"{failure}；Chrome 动态回退失败：{chrome_error}") from chrome_error
    raise failure


def _fetch_html_with_chrome(source: dict[str, Any], source_url: str, static_html: str) -> str:
    """Render only configured first-party dynamic sources with the system Chrome."""

    host = (urlsplit(source_url).hostname or "").lower()
    allowed_hosts = {
        _text(item).lower()
        for item in source.get("dynamic_allowed_hosts", [])
        if _text(item)
    }
    if not allowed_hosts or host not in allowed_hosts:
        raise EventIntelligenceError("Chrome 动态回退 URL 不在来源允许域名列表中")
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise EventIntelligenceError("缺少 Playwright，无法使用 Chrome 动态回退") from exc
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            try:
                page = browser.new_page()
                page.goto(source_url, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(1_500)
                rendered_html = page.content()
            finally:
                browser.close()
    except PlaywrightTimeoutError as exc:
        raise EventIntelligenceError("Chrome 动态回退超时") from exc
    except Exception as exc:
        raise EventIntelligenceError(f"Chrome 动态回退失败：{type(exc).__name__}") from exc
    if not _text(rendered_html):
        raise EventIntelligenceError("Chrome 动态回退未返回页面内容")
    return rendered_html


def _entry_url(source_url: str, identity: str) -> str:
    parsed = urlsplit(source_url)
    digest = hashlib.sha256(_normalise_name(identity).encode("utf-8")).hexdigest()[:16]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, f"event-{digest}"))


def _calendar_entry(
    *,
    source: dict[str, Any],
    source_url: str,
    title: str,
    date_text: str,
    venue: str = "",
    detail: str = "",
    raw: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    title = _text(title)
    date_text = _text(date_text)
    if not title or not date_text:
        return None
    start, end, confidence = parse_event_dates(date_text)
    if not start or not end:
        return None
    summary_parts = [f"时间：{date_text}"]
    if venue:
        summary_parts.append(f"场馆：{venue}")
    if detail:
        summary_parts.append(detail)
    return {
        "title": title,
        "summary": "；".join(summary_parts),
        "url": _entry_url(source_url, f"{title}|{date_text}|{venue}"),
        "event_start_date": start.isoformat(),
        "event_end_date": end.isoformat(),
        "date_confidence": confidence,
        "venue": venue,
        "venue_confidence": 100 if venue else 0,
        "raw": raw or {},
    }


class _TableRowsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._in_row = False
        self._in_cell = False
        self._row: list[str] = []
        self._cell_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr" and not self._in_row:
            self._in_row = True
            self._row = []
        elif self._in_row and tag in {"td", "th"} and not self._in_cell:
            self._in_cell = True
            self._cell_parts = []
        elif self._in_cell and tag == "br":
            self._cell_parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._in_cell and tag in {"td", "th"}:
            value = re.sub(r"\s+", " ", " ".join(self._cell_parts)).strip()
            self._row.append(value)
            self._in_cell = False
        elif self._in_row and tag == "tr":
            if self._row:
                self.rows.append(self._row)
            self._in_row = False


def _row_value(row: list[str], index: Any, *, source_key: str) -> str:
    if index is None or index == "":
        return ""
    try:
        value_index = int(index)
    except (TypeError, ValueError) as exc:
        raise EventIntelligenceError(f"来源 {source_key} 的表格列索引无效") from exc
    return _text(row[value_index]) if 0 <= value_index < len(row) else ""


def _fetch_html_table_calendar(source: dict[str, Any], timeout_seconds: int) -> list[dict[str, Any]]:
    source_url = _source_url(source)
    html = _fetch_html(source, timeout_seconds, url=source_url)
    parser = _TableRowsParser()
    parser.feed(html)
    source_key = _text(source.get("key")) or "<unknown>"
    entries: list[dict[str, Any]] = []
    for row in parser.rows:
        title = _row_value(row, source.get("title_index", 0), source_key=source_key)
        date_text = _row_value(row, source.get("date_index", 1), source_key=source_key)
        venue = _row_value(row, source.get("venue_index"), source_key=source_key)
        detail = _row_value(row, source.get("detail_index"), source_key=source_key)
        entry = _calendar_entry(
            source=source,
            source_url=source_url,
            title=title,
            date_text=date_text,
            venue=venue or _text(source.get("venue")),
            detail=detail,
            raw={"parser": "html_table_calendar_v1", "row": row},
        )
        if entry is not None:
            entries.append(entry)
    return entries


class _SniecCardsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: list[tuple[str, str]] = []
        self._in_card = False
        self._card_depth = 0
        self._active_tag = ""
        self._parts: list[str] = []
        self._title_parts: list[str] = []
        self._date_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "div" and not self._in_card and "p" in _text(attributes.get("class")).split():
            self._in_card = True
            self._card_depth = 1
            self._title_parts = []
            self._date_parts = []
            return
        if not self._in_card:
            return
        if tag == "div":
            self._card_depth += 1
        if tag in {"h1", "span"}:
            self._active_tag = tag
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._in_card and self._active_tag:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._in_card and self._active_tag == tag:
            value = re.sub(r"\s+", " ", " ".join(self._parts)).strip()
            if tag == "h1":
                self._title_parts.append(value)
            elif tag == "span" and "date" in value.lower():
                self._date_parts.append(value)
            self._active_tag = ""
        if self._in_card and tag == "div":
            self._card_depth -= 1
            if self._card_depth == 0:
                title = re.sub(r"\s+", " ", " ".join(self._title_parts)).strip()
                date_text = " ".join(self._date_parts)
                if title and date_text:
                    self.cards.append((title, date_text))
                self._in_card = False


def _month_urls(source: dict[str, Any]) -> list[str]:
    template = _text(source.get("url_template"))
    if not template.startswith("https://"):
        raise EventIntelligenceError(f"来源 {source.get('key') or '<unknown>'} 缺少合法 HTTPS url_template")
    try:
        months_ahead = int(source.get("months_ahead") or 1)
    except (TypeError, ValueError) as exc:
        raise EventIntelligenceError(f"来源 {source.get('key') or '<unknown>'} 的 months_ahead 无效") from exc
    if not 1 <= months_ahead <= 12:
        raise EventIntelligenceError(f"来源 {source.get('key') or '<unknown>'} 的 months_ahead 必须在 1 到 12 之间")
    today = date.today()
    urls: list[str] = []
    for offset in range(months_ahead):
        month_number = today.month + offset
        year = today.year + (month_number - 1) // 12
        month = (month_number - 1) % 12 + 1
        url = template.format(year=year, month=f"{month:02d}")
        if not url.startswith("https://"):
            raise EventIntelligenceError(f"来源 {source.get('key') or '<unknown>'} 生成了非 HTTPS 日历地址")
        urls.append(url)
    return urls


def _fetch_sniec_month_calendar(source: dict[str, Any], timeout_seconds: int) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for source_url in _month_urls(source):
        parser = _SniecCardsParser()
        parser.feed(_fetch_html(source, timeout_seconds, url=source_url))
        for title, date_text in parser.cards:
            entry = _calendar_entry(
                source=source,
                source_url=source_url,
                title=title,
                date_text=date_text,
                venue=_text(source.get("venue")),
                raw={"parser": "sniec_month_calendar_v1", "date_text": date_text},
            )
            if entry is not None:
                entries.append(entry)
    return entries


class _HeadingDateListParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[tuple[str, str]] = []
        self._in_item = False
        self._item_depth = 0
        self._active_tag = ""
        self._parts: list[str] = []
        self._title = ""
        self._published = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "li" and not self._in_item:
            self._in_item = True
            self._item_depth = 1
            self._title = ""
            self._published = ""
            return
        if not self._in_item:
            return
        if tag == "li":
            self._item_depth += 1
        if tag in {"h4", "span"}:
            self._active_tag = tag
            self._parts = []
            if tag == "h4" and _text(attributes.get("title")):
                self._title = _text(attributes.get("title"))

    def handle_data(self, data: str) -> None:
        if self._in_item and self._active_tag:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._in_item and self._active_tag == tag:
            value = re.sub(r"\s+", " ", " ".join(self._parts)).strip()
            if tag == "h4" and not self._title:
                self._title = value
            elif tag == "span" and re.search(r"20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}", value):
                self._published = value
            self._active_tag = ""
        if self._in_item and tag == "li":
            self._item_depth -= 1
            if self._item_depth == 0:
                if self._title and self._published:
                    self.items.append((self._title, self._published))
                self._in_item = False


def _fetch_perfect_world_news(source: dict[str, Any], timeout_seconds: int) -> list[dict[str, Any]]:
    source_url = _source_url(source)
    parser = _HeadingDateListParser()
    parser.feed(_fetch_html(source, timeout_seconds, url=source_url))
    return [
        {
            "title": title,
            "summary": f"官方发布日期：{published_at}",
            "url": _entry_url(source_url, f"{title}|{published_at}"),
            "published_at": published_at,
            "raw": {"parser": "perfect_world_news_v1", "published_at": published_at},
        }
        for title, published_at in parser.items
    ]


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._suppressed_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._suppressed_depth += 1
        elif not self._suppressed_depth and tag in {"div", "p", "li", "section", "h1", "h2", "h3", "h4", "br"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._suppressed_depth:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._suppressed_depth:
            self._suppressed_depth -= 1


def _parse_month_day_range(value: str, year: int) -> tuple[date, date] | None:
    names = "|".join(sorted(MONTH_NAME_NUMBERS, key=len, reverse=True))
    match = re.search(
        rf"\b({names})\s+(\d{{1,2}})\s*(?:[-–—]|to)\s*(?:({names})\s+)?(\d{{1,2}})\b",
        value,
        re.IGNORECASE,
    )
    if not match:
        return None
    start_month_name, start_day, end_month_name, end_day = match.groups()
    start_month = MONTH_NAME_NUMBERS[start_month_name.lower()]
    end_month = MONTH_NAME_NUMBERS[(end_month_name or start_month_name).lower()]
    start = _safe_date(year, start_month, int(start_day))
    end = _safe_date(year, end_month, int(end_day))
    if start and end and end >= start:
        return start, end
    return None


def _fetch_riot_schedule(source: dict[str, Any], timeout_seconds: int) -> list[dict[str, Any]]:
    source_url = _source_url(source)
    html = _fetch_html(source, timeout_seconds, url=source_url)
    entries = _riot_schedule_entries(source, source_url, html)
    if not entries and source.get("dynamic_render"):
        # The normal HTTP request is intentionally first. Chrome is restricted
        # to the first-party allowlist and is used only when the static schedule
        # contains no parseable dates.
        entries = _riot_schedule_entries(
            source,
            source_url,
            _fetch_html_with_chrome(source, source_url, html),
        )
    return entries


def _riot_schedule_entries(source: dict[str, Any], source_url: str, html: str) -> list[dict[str, Any]]:
    parser = _VisibleTextParser()
    parser.feed(html)
    visible_text = re.sub(r"\s+", " ", " ".join(parser.parts))
    try:
        calendar_year_value = source.get("calendar_year")
        calendar_year = date.today().year if _text(calendar_year_value).lower() in {"", "auto"} else int(calendar_year_value)
    except (TypeError, ValueError) as exc:
        raise EventIntelligenceError(f"来源 {source.get('key') or '<unknown>'} 的 calendar_year 无效") from exc
    event_names = source.get("event_names") or ["First Stand", "MSI", "Worlds"]
    if not isinstance(event_names, list):
        raise EventIntelligenceError(f"来源 {source.get('key') or '<unknown>'} 的 event_names 必须为数组")
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, date, date]] = set()
    for raw_name in event_names:
        name = _text(raw_name)
        if not name:
            continue
        matches = re.finditer(
            rf"\b{re.escape(name)}\b\s+([A-Za-z]{{3,9}}\s+\d{{1,2}}\s*(?:[-–—]|to)\s*(?:[A-Za-z]{{3,9}}\s+)?\d{{1,2}})",
            visible_text,
            re.IGNORECASE,
        )
        for match in matches:
            dates = _parse_month_day_range(match.group(1), calendar_year)
            if dates is None:
                continue
            start, end = dates
            key = (name.lower(), start, end)
            if key in seen:
                continue
            seen.add(key)
            entries.append(
                {
                    "title": f"{name} {calendar_year}",
                    "summary": f"Riot 官方全球赛程：{name} {match.group(1)}",
                    "url": _entry_url(source_url, f"{name}|{start.isoformat()}|{end.isoformat()}"),
                    "event_start_date": start.isoformat(),
                    "event_end_date": end.isoformat(),
                    "date_confidence": 100,
                    "raw": {"parser": "riot_schedule_v1", "schedule_text": match.group(0)},
                }
            )
    return entries


def _javascript_assignment(html: str, field: str) -> str:
    match = re.search(
        rf"{re.escape(field)}\s*=\s*\"((?:\\.|[^\"\\])*)\"",
        html,
        re.DOTALL,
    )
    if not match:
        return ""
    return match.group(1).replace(r"\"", '"').replace(r"\\", "\\")


def _fetch_allcpp_event(source: dict[str, Any], timeout_seconds: int) -> list[dict[str, Any]]:
    source_url = _source_url(source)
    html = _fetch_html(source, timeout_seconds, url=source_url)
    name = _javascript_assignment(html, "eventParam.eventName")
    start = _parse_iso_date(_javascript_assignment(html, "eventParam.sDate"))
    end = _parse_iso_date(_javascript_assignment(html, "eventParam.eDate"))
    venue = _javascript_assignment(html, "eventParam.enterAddress")
    tags = _javascript_assignment(html, "eventParam.eventTag")
    if not name or not start or not end or end < start:
        raise EventIntelligenceError("Comicup 官方活动页缺少完整名称或日期")
    return [
        {
            "title": name,
            "summary": f"Comicup 官方活动页：{tags}",
            "url": source_url,
            "event_start_date": start.isoformat(),
            "event_end_date": end.isoformat(),
            "date_confidence": 100,
            "venue": venue,
            "venue_confidence": 100 if venue else 0,
            "raw": {
                "parser": "allcpp_event_v1",
                "event_tag": tags,
                "event_city": _javascript_assignment(html, "eventParam.eventCity"),
            },
        }
    ]


def _fetch_bilibili_world(source: dict[str, Any], timeout_seconds: int) -> list[dict[str, Any]]:
    public_url = _source_url(source)
    page_html = _fetch_html(source, timeout_seconds, url=public_url)
    script_paths = re.findall(r'<script[^>]+src=["\']([^"\']*/js/app\.[^"\']+\.js)["\']', page_html, re.I)
    if not script_paths:
        raise EventIntelligenceError("BilibiliWorld 官方活动页未找到主前端资源")
    script_url = urljoin(public_url, script_paths[0])
    allowed_hosts = {_text(host).lower() for host in source.get("allowed_script_hosts", []) if _text(host)}
    if not allowed_hosts or (urlsplit(script_url).hostname or "").lower() not in allowed_hosts:
        raise EventIntelligenceError("BilibiliWorld 前端资源不在受信任域名列表中")
    html = _fetch_html(source, timeout_seconds, url=script_url)
    try:
        event_year_value = source.get("event_year")
        event_year = date.today().year if _text(event_year_value).lower() in {"", "auto"} else int(event_year_value)
        event_month = int(source.get("event_month"))
    except (TypeError, ValueError) as exc:
        raise EventIntelligenceError(f"来源 {source.get('key') or '<unknown>'} 缺少有效的 event_year/event_month") from exc
    dates = {
        _safe_date(event_year, int(month), int(day))
        for month, day in re.findall(r'date:"(\d{1,2})\.(\d{1,2})"', html)
        if int(month) == event_month and _safe_date(event_year, int(month), int(day)) is not None
    }
    if len(dates) < 2:
        raise EventIntelligenceError("BilibiliWorld 官方活动页未找到完整多日日期")
    ordered_dates = sorted(dates)
    return [
        {
            "title": _text(source.get("source_title")) or f"BW{event_year}",
            "summary": f"BilibiliWorld 官方前端日程：{ordered_dates[0].isoformat()} 至 {ordered_dates[-1].isoformat()}",
            "url": public_url,
            "event_start_date": ordered_dates[0].isoformat(),
            "event_end_date": ordered_dates[-1].isoformat(),
            "date_confidence": 100,
            "raw": {
                "parser": "bilibili_world_js_v1",
                "script_url": script_url,
                "days": [item.isoformat() for item in ordered_dates],
            },
        }
    ]


def _load_sources(path: str) -> list[dict[str, Any]]:
    config_path = Path(path)
    if not config_path.is_file():
        raise EventIntelligenceError(f"活动来源配置不存在：{config_path}")
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EventIntelligenceError("活动来源配置不是有效 JSON") from exc
    sources = payload.get("sources") if isinstance(payload, dict) else None
    if not isinstance(sources, list):
        raise EventIntelligenceError("活动来源配置必须包含 sources 数组")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in sources:
        if not isinstance(source, dict) or source.get("enabled") is False:
            continue
        key = _text(source.get("key"))
        kind = _text(source.get("kind"))
        if not key or not kind:
            raise EventIntelligenceError("每个启用活动来源必须提供 key 和 kind")
        if key in seen:
            raise EventIntelligenceError(f"活动来源 key 重复：{key}")
        seen.add(key)
        result.append(source)
    return result


def _fetch_entries(source: dict[str, Any], timeout_seconds: int) -> list[dict[str, Any]]:
    kind = _text(source.get("kind"))
    if kind == "steam_news":
        return _fetch_steam_news(source, timeout_seconds)
    if kind == "rss":
        return _fetch_rss(source, timeout_seconds)
    if kind == "html_table_calendar":
        return _fetch_html_table_calendar(source, timeout_seconds)
    if kind == "sniec_month_calendar":
        return _fetch_sniec_month_calendar(source, timeout_seconds)
    if kind == "perfect_world_news":
        return _fetch_perfect_world_news(source, timeout_seconds)
    if kind == "riot_schedule":
        return _fetch_riot_schedule(source, timeout_seconds)
    if kind == "allcpp_event":
        return _fetch_allcpp_event(source, timeout_seconds)
    if kind == "bilibili_world":
        return _fetch_bilibili_world(source, timeout_seconds)
    raise EventIntelligenceError(f"不支持的活动来源类型：{kind}")


def _require_database() -> None:
    if psycopg is None:
        raise EventIntelligenceError("缺少 psycopg 依赖，无法写入活动候选库")


def _db_row_value(row: Any, name: str, index: int) -> Any:
    return row.get(name) if isinstance(row, dict) else row[index]


def _candidate_source_rows(candidate: dict[str, Any], raw: dict[str, Any]) -> list[dict[str, Any]]:
    primary_url = _text(candidate.get("source_url"))
    primary_level = _text(candidate.get("source_confidence")) or "discovery"
    if primary_level == "corroborated":
        primary_level = "discovery"
    evidence = candidate.get("source_observations")
    if not isinstance(evidence, list):
        entry = raw.get("entry") if isinstance(raw.get("entry"), dict) else {}
        evidence = entry.get("source_evidence")
        if not isinstance(evidence, list):
            urls = entry.get("source_urls") if isinstance(entry.get("source_urls"), list) else []
            evidence = [{"url": url} for url in urls]
    observations = [
        *[item for item in evidence if isinstance(item, dict)],
        {
            "url": primary_url,
            "title": _text(candidate.get("source_title")),
            "published_at": candidate.get("published_at"),
        },
    ]
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for observation in observations:
        source_url = _text(observation.get("url"))
        if not source_url.startswith(("https://", "http://")) or source_url in seen:
            continue
        seen.add(source_url)
        observed_level = _text(observation.get("source_level"))
        source_level = primary_level if source_url == primary_url else (
            observed_level if observed_level in EVIDENCE_SOURCE_LEVELS else "discovery"
        )
        observed = _candidate_observation({**candidate, "source_url": source_url, "source_confidence": source_level})
        source_raw = {**raw, "observed": observed, "source_evidence": observation}
        source_raw["source_validation"] = observation.get("validation") or {}
        rows.append(
            {
                "source_key": _text(candidate.get("source_key")) or "qwen-native-discovery",
                "source_url": source_url,
                "source_title": _text(observation.get("title")) or _text(candidate.get("event_name")),
                "source_level": source_level,
                "published_at": _parse_published_at(observation.get("published_at")),
                "raw": json.dumps(source_raw, ensure_ascii=False, default=str),
            }
        )
    return rows


def _insert_candidate_sources(
    cursor: Any,
    candidate_id: int,
    candidate: dict[str, Any],
    raw: dict[str, Any],
) -> int:
    rows = _candidate_source_rows(candidate, raw)
    for row in rows:
        cursor.execute(
            """
            INSERT INTO event_candidate_sources (
                candidate_id, source_key, source_url, source_title,
                source_level, published_at, fetched_at, raw
            ) VALUES (
                %(candidate_id)s, %(source_key)s, %(source_url)s, %(source_title)s,
                %(source_level)s, %(published_at)s, now(), %(raw)s::jsonb
            )
            ON CONFLICT (candidate_id, source_url) DO UPDATE SET
                source_title = EXCLUDED.source_title,
                source_level = CASE
                    WHEN event_candidate_sources.source_level IN ('official', 'authoritative')
                        THEN event_candidate_sources.source_level
                    ELSE EXCLUDED.source_level
                END,
                published_at = COALESCE(EXCLUDED.published_at, event_candidate_sources.published_at),
                fetched_at = now(),
                raw = CASE WHEN EXCLUDED.raw->'source_validation'->>'rule' IS NOT NULL
                    THEN EXCLUDED.raw
                    ELSE EXCLUDED.raw || jsonb_build_object('source_validation', COALESCE(event_candidate_sources.raw->'source_validation', '{}'::jsonb)) END
            """,
            {**row, "candidate_id": candidate_id},
        )
    return len(rows)


def _refresh_candidate_corroboration(cursor: Any, candidate_id: int, *, min_domains: int) -> int:
    from src.event_corroboration import assess

    cursor.execute("SELECT * FROM event_candidates WHERE id = %s FOR UPDATE", (candidate_id,))
    row = cursor.fetchone()
    if row is None:
        raise EventIntelligenceError(f"候选活动不存在：{candidate_id}")
    candidate = dict(row)
    cursor.execute("SELECT source_url, raw FROM event_candidate_sources WHERE candidate_id = %s", (candidate_id,))
    assessment = assess(candidate, [dict(item) for item in cursor.fetchall()], min_domains=min_domains)
    state = candidate["verification_state"]
    level = candidate["source_confidence"]
    status = candidate["event_status"]
    # Aggregate evidence never overrides official, manual, cancelled or existing conflicts.
    if level not in {"official", "authoritative"} and status not in {"promoted", "cancelled"} and state != "conflict":
        if assessment["conflict"]:
            state = "conflict"
        elif assessment["eligible"] and candidate["city"] == "上海" and candidate["offline_mode"] in {"offline", "hybrid"}:
            state, level, status = "corroborated", "corroborated", "probable"
        elif state == "corroborated" or level == "corroborated":
            state, level, status = "unverified", "discovery", "discovered"
    cursor.execute(
        """
        UPDATE event_candidates
        SET independent_source_count = %s, verification_state = %s, source_confidence = %s,
            event_status = %s, corroborated_at = CASE WHEN %s = 'corroborated' THEN %s::timestamptz ELSE NULL END,
            raw = jsonb_set(raw, '{corroboration}', %s::jsonb), updated_at = now()
        WHERE id = %s
        """,
        (assessment["count"], state, level, status, state, assessment.get("evidence_as_of"), json.dumps(assessment), candidate_id),
    )
    return assessment["count"]


def _best_business_source(cursor: Any, candidate_id: int) -> tuple[str, str] | None:
    cursor.execute(
        """
        SELECT source_level, source_url FROM event_candidate_sources
        WHERE candidate_id = %s AND source_level IN ('official', 'authoritative')
        ORDER BY CASE source_level WHEN 'official' THEN 2 ELSE 1 END DESC,
                 fetched_at DESC, id DESC LIMIT 1
        """,
        (candidate_id,),
    )
    row = cursor.fetchone()
    return (str(_db_row_value(row, "source_level", 0)), str(_db_row_value(row, "source_url", 1))) if row else None


def _candidate_is_ready_for_business(candidate: dict[str, Any]) -> bool:
    """Only official/authoritative verification permits canonical promotion."""
    return (
        candidate.get("verification_state") == "verified"
        and candidate.get("source_confidence") in {"official", "authoritative"}
        and candidate.get("event_status") in BUSINESS_CANDIDATE_STATUSES
        and candidate.get("city") == "上海"
        and candidate.get("offline_mode") in {"offline", "hybrid"}
        and bool(_text(candidate.get("venue")))
        and isinstance(candidate.get("event_start_date"), date)
        and isinstance(candidate.get("event_end_date"), date)
        and candidate["event_end_date"] >= candidate["event_start_date"]
        and int(candidate.get("date_confidence") or 0) >= 90
        and int(candidate.get("venue_confidence") or 0) >= 90
    )


def _canonicalize_event_candidate(
    cursor: Any,
    candidate_id: int,
    *,
    manual_overrides: dict[str, Any] | None = None,
) -> int | None:
    """Promote official evidence only; corroborated candidates stay separate."""

    cursor.execute(
        """
        SELECT id, event_name, city, venue, event_start_date, event_end_date,
               category, event_status, offline_mode, date_confidence,
               venue_confidence, source_confidence, verification_state,
               independent_source_count, canonical_event_id
        FROM event_candidates
        WHERE id = %s
        """,
        (candidate_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise EventIntelligenceError(f"候选活动不存在：{candidate_id}")
    candidate = dict(row) if isinstance(row, dict) else {
        "id": row[0], "event_name": row[1], "city": row[2], "venue": row[3],
        "event_start_date": row[4], "event_end_date": row[5], "category": row[6],
        "event_status": row[7], "offline_mode": row[8], "date_confidence": row[9],
        "venue_confidence": row[10], "source_confidence": row[11], "verification_state": row[12],
        "independent_source_count": row[13], "canonical_event_id": row[14],
    }
    event_start = candidate.get("event_start_date")
    event_end = candidate.get("event_end_date")

    existing_occurrence_id = candidate.get("canonical_event_id")
    if isinstance(existing_occurrence_id, int) and (
        candidate.get("event_status") == "cancelled"
        or candidate.get("verification_state") in {"conflict", "stale"}
    ):
        inactive_status = "cancelled" if candidate.get("event_status") == "cancelled" else "conflict"
        cursor.execute(
            """
            UPDATE event_occurrences
            SET is_business_active = FALSE, canonical_status = %s, last_verified_at = now()
            WHERE id = %s AND source_kind <> 'manual'
            """,
            (inactive_status, existing_occurrence_id),
        )
        return None

    if manual_overrides is None:
        if not _candidate_is_ready_for_business(candidate):
            return None
        best_source = _best_business_source(cursor, candidate_id)
        if best_source is None:
            return None
        source_confidence, primary_source_url = best_source
        venue = _text(candidate["venue"])
        stay_start = event_start
        stay_end = event_end
        demand_level = "normal"
        source_kind = "official_candidate"
        canonical_status = "confirmed"
    else:
        if str(candidate.get("city") or "").strip() != "上海":
            return None
        if not isinstance(event_start, date) or not isinstance(event_end, date) or event_end < event_start:
            return None
        venue = _text(manual_overrides.get("venue"))
        if not venue:
            raise EventIntelligenceError("正式活动缺少场馆")
        stay_start = manual_overrides["stay_start_date"]
        stay_end = manual_overrides["stay_end_date"]
        demand_level = _text(manual_overrides["demand_level"])
        source_kind = "manual"
        canonical_status = "promoted"
        source_confidence = "manual"
        primary_source_url = ""

    occurrence_key = _occurrence_key(str(candidate["event_name"]), "上海", venue, event_start)
    if manual_overrides is None and isinstance(existing_occurrence_id, int):
        cursor.execute(
            """
            UPDATE event_occurrences
            SET occurrence_key = %s, venue = %s, event_name = %s,
                event_start_date = %s, event_end_date = %s,
                stay_start_date = %s, stay_end_date = %s, duration_days = %s,
                category = %s, source_file = %s, source_sheet = %s, source_row = %s,
                date_fill_method = 'official_candidate', source_kind = %s,
                source_confidence = %s, primary_source_url = NULLIF(%s, ''),
                last_verified_at = now(), canonical_status = %s,
                is_business_active = TRUE, imported_at = now()
            WHERE id = %s AND source_kind <> 'manual'
            RETURNING id
            """,
            (
                occurrence_key, venue, candidate["event_name"], event_start, event_end,
                stay_start, stay_end, (event_end - event_start).days + 1,
                candidate.get("category") or "other", f"event_candidate:{candidate_id}",
                "event_intelligence", candidate_id, source_kind, source_confidence,
                primary_source_url, canonical_status, existing_occurrence_id,
            ),
        )
        occurrence_row = cursor.fetchone()
        if occurrence_row is not None:
            return int(_db_row_value(occurrence_row, "id", 0))
    cursor.execute(
        """
        INSERT INTO event_occurrences (
            occurrence_key, venue, event_name, event_start_date, event_end_date,
            stay_start_date, stay_end_date, duration_days, category, demand_level,
            source_file, source_sheet, source_row, date_fill_method, city,
            source_kind, source_confidence, primary_source_url, last_verified_at,
            canonical_status, is_business_active
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, '上海', %s, %s, NULLIF(%s, ''), now(), %s, TRUE
        )
        ON CONFLICT (occurrence_key) DO UPDATE SET
            event_name = EXCLUDED.event_name,
            event_end_date = EXCLUDED.event_end_date,
            category = EXCLUDED.category,
            stay_start_date = CASE WHEN event_occurrences.source_kind = 'manual'
                AND EXCLUDED.source_kind <> 'manual' THEN event_occurrences.stay_start_date
                ELSE EXCLUDED.stay_start_date END,
            stay_end_date = CASE WHEN event_occurrences.source_kind = 'manual'
                AND EXCLUDED.source_kind <> 'manual' THEN event_occurrences.stay_end_date
                ELSE EXCLUDED.stay_end_date END,
            duration_days = CASE WHEN event_occurrences.source_kind = 'manual'
                AND EXCLUDED.source_kind <> 'manual' THEN event_occurrences.duration_days
                ELSE EXCLUDED.duration_days END,
            demand_level = CASE WHEN event_occurrences.source_kind = 'manual'
                AND EXCLUDED.source_kind <> 'manual' THEN event_occurrences.demand_level
                ELSE EXCLUDED.demand_level END,
            source_file = EXCLUDED.source_file,
            source_sheet = EXCLUDED.source_sheet,
            source_row = EXCLUDED.source_row,
            date_fill_method = EXCLUDED.date_fill_method,
            source_kind = CASE WHEN event_occurrences.source_kind = 'manual'
                AND EXCLUDED.source_kind <> 'manual' THEN event_occurrences.source_kind
                ELSE EXCLUDED.source_kind END,
            source_confidence = CASE WHEN event_occurrences.source_kind = 'manual'
                AND EXCLUDED.source_kind <> 'manual' THEN event_occurrences.source_confidence
                ELSE EXCLUDED.source_confidence END,
            primary_source_url = CASE WHEN event_occurrences.source_kind = 'manual'
                AND EXCLUDED.source_kind <> 'manual' THEN event_occurrences.primary_source_url
                ELSE EXCLUDED.primary_source_url END,
            last_verified_at = now(),
            canonical_status = CASE WHEN event_occurrences.source_kind = 'manual'
                AND EXCLUDED.source_kind <> 'manual' THEN event_occurrences.canonical_status
                ELSE EXCLUDED.canonical_status END,
            is_business_active = TRUE,
            imported_at = now()
        RETURNING id
        """,
        (
            occurrence_key, venue, candidate["event_name"], event_start, event_end,
            stay_start, stay_end, (event_end - event_start).days + 1,
            candidate.get("category") or "other", demand_level,
            f"event_candidate:{candidate_id}", "event_intelligence", candidate_id,
            "manual_override" if manual_overrides is not None else "official_candidate",
            source_kind, source_confidence, primary_source_url, canonical_status,
        ),
    )
    occurrence_row = cursor.fetchone()
    if occurrence_row is None:
        raise EventIntelligenceError("正式活动写入未返回 ID")
    occurrence_id = int(_db_row_value(occurrence_row, "id", 0))
    cursor.execute(
        "UPDATE event_candidates SET canonical_event_id = %s, updated_at = now() WHERE id = %s",
        (occurrence_id, candidate_id),
    )
    return occurrence_id


def upsert_event_candidate(candidate: dict[str, Any]) -> int:
    _require_database()
    if not candidate.get("candidate_key") or not candidate.get("event_identity_key") or not candidate.get("event_name") or not candidate.get("city"):
        raise EventIntelligenceError("候选活动缺少名称、城市或活动身份键")
    try:
        min_domains = max(2, min(int(candidate.get("corroboration_min_domains", 2)), 10))
    except (TypeError, ValueError) as exc:
        raise EventIntelligenceError("多源交叉证实域名门槛无效") from exc
    raw = dict(candidate.get("raw") or {})
    raw["observed"] = _candidate_observation(candidate)
    try:
        with psycopg.connect(_load_database_url(), row_factory=psycopg.rows.dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, candidate_key, event_name, aliases, city, venue,
                           event_start_date, event_end_date, category, event_status,
                           offline_mode, source_confidence, date_confidence,
                           venue_confidence, primary_source_url, verification_state,
                           conflict_details, independent_source_count, canonical_event_id
                    FROM event_candidates
                    WHERE event_identity_key = %s OR candidate_key = %s
                    ORDER BY CASE WHEN event_identity_key = %s THEN 0 ELSE 1 END,
                             CASE source_confidence
                                 WHEN 'official' THEN 4
                                 WHEN 'authoritative' THEN 3
                                 WHEN 'corroborated' THEN 2
                                 WHEN 'media' THEN 1
                                 ELSE 0
                             END DESC,
                             CASE event_status
                                 WHEN 'promoted' THEN 4
                                 WHEN 'confirmed' THEN 3
                                 WHEN 'probable' THEN 2
                                 ELSE 1
                             END DESC,
                             date_confidence DESC,
                             venue_confidence DESC,
                             id DESC
                    LIMIT 1
                    FOR UPDATE
                    """,
                    (candidate["event_identity_key"], candidate["candidate_key"], candidate["event_identity_key"]),
                )
                existing_row = cursor.fetchone()
                if existing_row is None:
                    initial = {**candidate, "raw": raw}
                    initial["verification_state"] = _verification_state(initial, conflict=False)
                    cursor.execute(
                        """
                        INSERT INTO event_candidates (
                            candidate_key, event_identity_key, event_name, aliases, city, venue,
                            event_start_date, event_end_date, category, event_status, offline_mode,
                            source_confidence, date_confidence, venue_confidence, primary_source_url,
                            verification_state, conflict_details, resolved_sources, resolved_at,
                            first_seen_at, last_verified_at, raw
                        ) VALUES (
                            %(candidate_key)s, %(event_identity_key)s, %(event_name)s, %(aliases)s::jsonb,
                            %(city)s, %(venue)s, %(event_start_date)s, %(event_end_date)s, %(category)s,
                            %(event_status)s, %(offline_mode)s, %(source_confidence)s,
                            %(date_confidence)s, %(venue_confidence)s, %(source_url)s,
                            %(verification_state)s, '[]'::jsonb,
                            jsonb_build_object(
                                'event_start_date', CAST(%(source_url)s AS text),
                                'event_end_date', CAST(%(source_url)s AS text),
                                'venue', CAST(%(source_url)s AS text)
                            ),
                            now(), now(), CASE WHEN %(source_confidence)s IN ('official', 'authoritative') THEN now() ELSE now() END,
                            %(raw)s::jsonb
                        ) RETURNING id
                        """,
                        {
                            **initial,
                            "aliases": json.dumps(initial["aliases"], ensure_ascii=False),
                            "raw": json.dumps(initial["raw"], ensure_ascii=False, default=str),
                        },
                    )
                    created = cursor.fetchone()
                    if created is None:
                        raise EventIntelligenceError("候选活动写入未返回 ID")
                    candidate_id = int(created["id"])
                else:
                    existing = dict(existing_row)
                    incoming_rank = _source_rank(candidate.get("source_confidence"))
                    existing_rank = _source_rank(existing.get("source_confidence"))
                    conflicts = _fields_conflict(existing, candidate)
                    has_business_conflict = bool(
                        conflicts
                        and incoming_rank == existing_rank
                        and incoming_rank >= SOURCE_RANKS["authoritative"]
                    )
                    replace_fields = _incoming_can_replace(existing, candidate) and not has_business_conflict
                    merged = {**existing}
                    merged["city"] = _merged_candidate_city(existing.get("city"), candidate.get("city"))
                    merged["aliases"] = _merged_aliases(existing.get("aliases"), candidate["aliases"])
                    merged["event_status"] = _merged_event_status(existing.get("event_status"), candidate.get("event_status")) if incoming_rank >= existing_rank else existing.get("event_status")
                    merged["source_confidence"] = (
                        candidate["source_confidence"] if incoming_rank > existing_rank else existing["source_confidence"]
                    )
                    merged["offline_mode"] = (
                        candidate["offline_mode"] if _text(existing.get("offline_mode")) == "unknown" else existing["offline_mode"]
                    )
                    merged["category"] = candidate.get("category") or existing.get("category")
                    if replace_fields:
                        for field in ("venue", "event_start_date", "event_end_date"):
                            merged[field] = candidate.get(field) or existing.get(field)
                        merged["date_confidence"] = max(int(existing.get("date_confidence") or 0), int(candidate.get("date_confidence") or 0))
                        merged["venue_confidence"] = max(int(existing.get("venue_confidence") or 0), int(candidate.get("venue_confidence") or 0))
                        merged["primary_source_url"] = candidate["source_url"]
                    conflict_details = existing.get("conflict_details") if isinstance(existing.get("conflict_details"), list) else []
                    if has_business_conflict:
                        conflict_details = [
                            *conflict_details,
                            {
                                "fields": conflicts,
                                "existing_source": existing.get("primary_source_url"),
                                "incoming_source": candidate.get("source_url"),
                                "incoming_level": candidate.get("source_confidence"),
                            },
                        ][-20:]
                    unresolved_conflict = existing.get("verification_state") == "conflict" and not (
                        incoming_rank > existing_rank and incoming_rank >= SOURCE_RANKS["authoritative"]
                    )
                    merged["verification_state"] = _verification_state(merged, conflict=has_business_conflict or unresolved_conflict)
                    resolved_sources = {
                        "event_start_date": merged.get("primary_source_url") or "",
                        "event_end_date": merged.get("primary_source_url") or "",
                        "venue": merged.get("primary_source_url") or "",
                    }
                    cursor.execute(
                        """
                        UPDATE event_candidates
                        SET event_identity_key = %s, aliases = %s::jsonb, city = %s, venue = %s,
                            event_start_date = %s, event_end_date = %s, category = %s,
                            event_status = %s, offline_mode = %s, source_confidence = %s,
                            date_confidence = %s, venue_confidence = %s, primary_source_url = %s,
                            verification_state = %s, conflict_details = %s::jsonb,
                            resolved_sources = %s::jsonb, resolved_at = now(),
                            last_verified_at = CASE WHEN %s IN ('official', 'authoritative') THEN now() ELSE last_verified_at END,
                            raw = %s::jsonb, updated_at = now()
                        WHERE id = %s
                        """,
                        (
                            candidate["event_identity_key"], json.dumps(merged["aliases"], ensure_ascii=False), merged.get("city"), merged.get("venue"),
                            merged.get("event_start_date"), merged.get("event_end_date"), merged.get("category"),
                            merged.get("event_status"), merged.get("offline_mode"), merged.get("source_confidence"),
                            merged.get("date_confidence"), merged.get("venue_confidence"), merged.get("primary_source_url"),
                            merged.get("verification_state"), json.dumps(conflict_details, ensure_ascii=False, default=str),
                            json.dumps(resolved_sources, ensure_ascii=False), candidate.get("source_confidence"),
                            json.dumps(raw, ensure_ascii=False, default=str), existing["id"],
                        ),
                    )
                    candidate_id = int(existing["id"])
                _insert_candidate_sources(cursor, candidate_id, candidate, raw)
                _refresh_candidate_corroboration(cursor, candidate_id, min_domains=min_domains)
                _canonicalize_event_candidate(cursor, candidate_id)
                return candidate_id
    except DBWriterError as exc:
        raise EventIntelligenceError(str(exc)) from exc
    except EventIntelligenceError:
        raise
    except Exception as exc:
        raise EventIntelligenceError(f"活动候选库写入失败：{type(exc).__name__}") from exc


def reconcile_qwen_corroboration(*, min_domains: int = 2) -> dict[str, int]:
    """Backfill Qwen evidence URLs and re-evaluate aggregate corroboration."""

    _require_database()
    if not 3 <= min_domains <= 10:
        raise EventIntelligenceError("多源交叉证实域名门槛必须在 3 到 10 之间")
    summary = {"scanned": 0, "source_observations": 0, "corroborated": 0, "occurrences": 0}
    try:
        with psycopg.connect(_load_database_url(), row_factory=psycopg.rows.dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, event_name, city, venue, event_start_date, event_end_date,
                           category, event_status, offline_mode, source_confidence,
                           date_confidence, venue_confidence, primary_source_url, raw
                    FROM event_candidates
                    WHERE raw->'entry'->>'provider' = 'qwen_native'
                    ORDER BY id
                    FOR UPDATE
                    """
                )
                candidates = [dict(row) for row in cursor.fetchall()]
                for candidate in candidates:
                    summary["scanned"] += 1
                    candidate.update(
                        {
                            "source_key": "qwen-native-discovery",
                            "source_url": candidate.get("primary_source_url"),
                            "source_title": candidate.get("event_name"),
                            "published_at": None,
                        }
                    )
                    raw = dict(candidate.get("raw") or {})
                    mode = str(raw.get("entry", {}).get("offline_mode") or "").lower()
                    if candidate["offline_mode"] == "unknown" and ("线下" in mode or mode in {"offline", "hybrid"}):
                        candidate["offline_mode"] = "hybrid" if mode == "hybrid" or "线上" in mode else "offline"
                        cursor.execute("UPDATE event_candidates SET offline_mode = %s WHERE id = %s", (candidate["offline_mode"], candidate["id"]))
                    summary["source_observations"] += _insert_candidate_sources(
                        cursor, int(candidate["id"]), candidate, raw
                    )
                    _refresh_candidate_corroboration(
                        cursor, int(candidate["id"]), min_domains=min_domains
                    )
                    cursor.execute(
                        "SELECT verification_state FROM event_candidates WHERE id = %s",
                        (candidate["id"],),
                    )
                    state_row = cursor.fetchone()
                    if state_row and _db_row_value(state_row, "verification_state", 0) == "corroborated":
                        summary["corroborated"] += 1
        return summary
    except DBWriterError as exc:
        raise EventIntelligenceError(str(exc)) from exc
    except EventIntelligenceError:
        raise
    except Exception as exc:
        raise EventIntelligenceError(f"多源候选重评失败：{type(exc).__name__}") from exc


def sync_event_sources(
    config_path: str,
    *,
    dry_run: bool = False,
    timeout_seconds: int = 15,
) -> dict[str, Any]:
    if timeout_seconds < 1 or timeout_seconds > 60:
        raise EventIntelligenceError("--timeout-seconds 必须在 1 到 60 之间")
    summary: dict[str, Any] = {
        "sources": 0,
        "fetched_sources": 0,
        "fetched_entries": 0,
        "matched_candidates": 0,
        "written_candidates": 0,
        "write_failures": 0,
        "failures": [],
    }
    sources = _load_sources(config_path)
    if not sources:
        raise EventIntelligenceError("没有启用的活动来源")
    health_run_id = ""
    if not dry_run:
        from src.event_source_health import new_run_id

        health_run_id = new_run_id()
    for source in sources:
        summary["sources"] += 1
        source_matched = source_written = 0
        try:
            entries = _fetch_entries(source, timeout_seconds)
        except EventIntelligenceError as exc:
            summary["failures"].append(f"{source['key']}: {exc}")
            if not dry_run:
                _record_source_health(health_run_id, source["key"], "failed", 0, 0, 0, [], str(exc))
            continue
        summary["fetched_sources"] += 1
        summary["fetched_entries"] += len(entries)
        for entry in entries:
            candidate = candidate_from_entry(entry, source)
            if candidate is None:
                continue
            summary["matched_candidates"] += 1
            source_matched += 1
            if dry_run:
                continue
            try:
                upsert_event_candidate(candidate)
            except EventIntelligenceError as exc:
                summary["write_failures"] += 1
                summary["failures"].append(f"{source['key']}: {exc}")
                continue
            summary["written_candidates"] += 1
            source_written += 1
        if not dry_run:
            status = "partial" if source_written < source_matched and source_matched else "success"
            _record_source_health(
                health_run_id,
                source["key"],
                status,
                len(entries),
                source_matched,
                source_written,
                entries,
            )
    if summary["fetched_sources"] == 0:
        raise EventIntelligenceError("所有活动来源均同步失败")
    if (
        not dry_run
        and summary["matched_candidates"] > 0
        and summary["written_candidates"] == 0
        and summary["write_failures"] > 0
    ):
        raise EventIntelligenceError("活动候选全部写入失败")
    return summary


def _record_source_health(
    run_id: str,
    source_key: str,
    status: str,
    fetched_entries: int,
    matched_candidates: int,
    written_candidates: int,
    entries: list[dict[str, Any]],
    error_summary: str = "",
) -> None:
    try:
        from src.event_source_health import entries_fingerprint, record_source_run

        record_source_run(
            run_id=run_id,
            source_key=source_key,
            status=status,
            fetched_entries=fetched_entries,
            matched_candidates=matched_candidates,
            written_candidates=written_candidates,
            entries_hash=entries_fingerprint(entries) if entries else "",
            error_summary=error_summary,
        )
    except Exception as exc:
        raise EventIntelligenceError(f"活动来源健康记录失败：{type(exc).__name__}") from exc


def list_event_candidates(
    status: str = "all",
    limit: int = 50,
    *,
    date_from: str = "",
    date_to: str = "",
) -> list[dict[str, Any]]:
    _require_database()
    if status != "all" and status not in VALID_CANDIDATE_STATUSES:
        raise EventIntelligenceError("--status 无效")
    if not 1 <= limit <= 500:
        raise EventIntelligenceError("--limit 必须在 1 到 500 之间")
    start = _parse_iso_date(date_from) if date_from else None
    end = _parse_iso_date(date_to) if date_to else None
    if date_from and start is None:
        raise EventIntelligenceError("--from 必须是 YYYY-MM-DD")
    if date_to and end is None:
        raise EventIntelligenceError("--to 必须是 YYYY-MM-DD")
    if start and end and end < start:
        raise EventIntelligenceError("--to 不能早于 --from")
    clauses: list[str] = []
    params: list[Any] = []
    if status != "all":
        clauses.append("event_status = %s")
        params.append(status)
    if start:
        clauses.append("event_end_date >= %s")
        params.append(start)
    if end:
        clauses.append("event_start_date <= %s")
        params.append(end)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        with psycopg.connect(_load_database_url(), row_factory=psycopg.rows.dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT id, event_name, aliases, city, venue, event_start_date, event_end_date,
                           category, event_status, offline_mode, source_confidence,
                           date_confidence, venue_confidence, primary_source_url,
                           first_seen_at, last_verified_at
                    FROM event_candidates
                    {where}
                    ORDER BY last_verified_at DESC, id DESC
                    LIMIT %s
                    """,
                    (*params, limit),
                )
                return [dict(row) for row in cursor.fetchall()]
    except DBWriterError as exc:
        raise EventIntelligenceError(str(exc)) from exc
    except Exception as exc:
        raise EventIntelligenceError(f"候选活动查询失败：{type(exc).__name__}") from exc


def promote_event_candidate(
    candidate_id: int,
    *,
    venue: str,
    demand_level: str,
    stay_start: str = "",
    stay_end: str = "",
    dry_run: bool = False,
) -> dict[str, Any]:
    _require_database()
    venue = _text(venue)
    if not venue:
        raise EventIntelligenceError("--venue 不能为空")
    if demand_level not in VALID_DEMAND_LEVELS:
        raise EventIntelligenceError("--demand-level 必须为 high、medium 或 normal")
    try:
        with psycopg.connect(_load_database_url(), row_factory=psycopg.rows.dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, event_name, city, event_start_date, event_end_date, category, event_status
                    FROM event_candidates
                    WHERE id = %s
                    """,
                    (candidate_id,),
                )
                candidate = cursor.fetchone()
                if candidate is None:
                    raise EventIntelligenceError(f"候选活动不存在：{candidate_id}")
                candidate = dict(candidate)
                if candidate["event_status"] == "cancelled":
                    raise EventIntelligenceError("已取消候选活动不能提升为正式活动")
                if str(candidate.get("city") or "").strip() != "上海":
                    raise EventIntelligenceError("当前仅支持将上海活动提升为正式活动")
                event_start = candidate.get("event_start_date")
                event_end = candidate.get("event_end_date")
                if not event_start or not event_end:
                    raise EventIntelligenceError("候选活动缺少已确认日期，不能提升为正式活动")
                resolved_stay_start = _parse_iso_date(stay_start) or event_start
                resolved_stay_end = _parse_iso_date(stay_end) or event_end
                if resolved_stay_end < resolved_stay_start:
                    raise EventIntelligenceError("入住覆盖结束日期不能早于开始日期")
                result = {
                    "candidate_id": candidate_id,
                    "event_name": candidate["event_name"],
                    "venue": venue,
                    "event_start_date": event_start,
                    "event_end_date": event_end,
                    "stay_start_date": resolved_stay_start,
                    "stay_end_date": resolved_stay_end,
                    "demand_level": demand_level,
                    "dry_run": dry_run,
                }
                if dry_run:
                    return result
                occurrence_id = _canonicalize_event_candidate(
                    cursor,
                    candidate_id,
                    manual_overrides={
                        "venue": venue,
                        "demand_level": demand_level,
                        "stay_start_date": resolved_stay_start,
                        "stay_end_date": resolved_stay_end,
                    },
                )
                if occurrence_id is None:
                    raise EventIntelligenceError("候选活动未满足正式活动条件")
                cursor.execute(
                    """
                    UPDATE event_candidates
                    SET event_status = 'promoted', promoted_at = now(), updated_at = now()
                    WHERE id = %s
                    """,
                    (candidate_id,),
                )
                result["event_id"] = occurrence_id
                return result
    except DBWriterError as exc:
        raise EventIntelligenceError(str(exc)) from exc
    except EventIntelligenceError:
        raise
    except Exception as exc:
        raise EventIntelligenceError(f"候选活动提升失败：{type(exc).__name__}") from exc
