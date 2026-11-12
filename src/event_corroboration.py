"""Per-page evidence for the non-canonical business reference layer."""
from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from html import unescape
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

RULE_VERSION = "event_bound_fields_v3"
LEGACY_RULE_VERSION = "independent_pages_v1"
PUBLISHERS = {"donews.com", "17173.com", "ithome.com", "jfdaily.com", "shobserver.com", "thepaper.cn", "people.com.cn", "xinhuanet.com", "ifeng.com", "sina.com.cn", "sina.cn", "hket.com"}
EXCLUDED = {"baidu.com", "hupu.com", "zhihu.com", "weibo.com", "bilibili.com"}
AUTHORIZED_TICKETING_HOSTS = {"show.bilibili.com"}


def normal(value) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", str(value or "").lower())


def visible_event_text(html: str) -> str:
    """Preserve HTML block boundaries so event-list fields cannot be mixed."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1\s*>", " ", html, flags=re.I | re.S)
    text = re.sub(r"<br\s*/?>|</(?:p|div|li|tr|h[1-6]|section|article)\s*>", "\n", text, flags=re.I)
    text = unescape(re.sub(r"<[^>]+>", " ", text))
    return "\n".join(line for block in text.splitlines() if (line := re.sub(r"\s+", " ", block).strip()))


def event_passage(candidate: dict, text: str, *, identity_aliases: dict | None = None) -> str:
    """Select the named entry of a schedule list; ambiguous entries fail closed."""
    variants = _identity_variants(candidate, identity_aliases)
    variants.update(value[:-2] for value in list(variants) if value.endswith("活动") and len(value) >= 8)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    matches = [index for index, line in enumerate(lines)
               if any(name in re.sub(r"20\d{2}", "", normal(line)) for name in variants)]
    if not matches:
        return ""
    time_label = re.compile(r"^(?:活动)?(?:时间|日期)[：:]")
    venue_label = re.compile(r"^(?:活动)?(?:地点(?:/平台)?|场馆)[：:]")
    if sum(bool(time_label.match(line)) for line in lines) < 2:
        if len(lines) == 1 and len(_explicit_dates(text, date.today().year)[0]) > 1:
            return ""
        if not any(time_label.match(line) for line in lines):
            return "\n".join(lines[index] for index in matches)
    passages: list[str] = []
    for start in matches:
        part: list[str] = []
        time_seen = False
        for line in lines[start:]:
            if time_label.match(line):
                if time_seen:
                    break
                time_seen = True
            if time_seen and re.match(r"^(?:参与方式|票价)[：:]", line):
                break
            part.append(line)
            if time_seen and venue_label.match(line):
                break
        passage = "\n".join(part)
        if time_seen and any(venue_label.match(line) for line in part):
            passages.append(passage)
    unique: dict[tuple[str, ...], str] = {}
    for passage in passages:
        fields = tuple(line for line in passage.splitlines() if time_label.match(line) or venue_label.match(line))
        if len(passage) > len(unique.get(fields, "")):
            unique[fields] = passage
    return next(iter(unique.values())) if len(unique) == 1 else ""


def domain(url) -> str:
    host = (urlsplit(str(url)).hostname or "").lower().strip(".")
    labels = host.split(".")
    suffix = ".".join(labels[-2:])
    return ".".join(labels[-3:]) if suffix in {"com.cn", "net.cn", "org.cn", "gov.cn", "co.uk", "com.au", "co.jp"} else suffix


def _public_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
            return False
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        return bool(addresses) and all(ipaddress.ip_address(item[4][0]).is_global for item in addresses)
    except (ValueError, OSError):
        return False


class _PublicRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _public_url(newurl):
            raise ValueError("non-public redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_page(url: str) -> tuple[str, str]:
    """Bounded unauthenticated public HTTPS read; failures are unusable evidence."""
    if not _public_url(url):
        return "", "unsafe_url"
    try:
        opener = build_opener(_PublicRedirect())
        with opener.open(Request(url, headers={"User-Agent": "Mozilla/5.0 hotel-event-evidence/1.0"}), timeout=12) as response:
            if domain(response.geturl()) != domain(url):
                return "", "cross_publisher_redirect"
            data = response.read(1_000_001)
            if len(data) > 1_000_000:
                return "", "page_too_large"
            charset = response.headers.get_content_charset() or "utf-8"
        try:
            html = data.decode(charset)
        except (UnicodeError, LookupError):
            html = data.decode("gb18030", errors="replace")
        article = re.search(r"<article\b[^>]*>(.*?)</article>", html, re.S | re.I)
        html = article.group(1) if article else html
        html = re.sub(r"<(script|style|nav)[^>]*>.*?</\1\s*>", " ", html, flags=re.S | re.I)
        text = visible_event_text(html)
        return text[:100_000], "ok"
    except Exception as exc:
        return "", type(exc).__name__


def same_event(first: dict, second: dict) -> bool:
    a = re.sub(r"20\d{2}", "", normal(first.get("event_name") or first.get("title")))
    b = re.sub(r"20\d{2}", "", normal(second.get("event_name") or second.get("title")))
    if min(len(a), len(b)) < 4:
        return a == b and bool(a)
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    shared = sum(block.size for block in matcher.get_matching_blocks())
    shorter_coverage = shared / min(len(a), len(b))
    return a in b or b in a or matcher.ratio() >= 0.72 or (shared >= 6 and shorter_coverage >= 0.68)


def _identity_variants(candidate: dict, aliases: dict | None) -> set[str]:
    names = [candidate.get("event_name"), *(candidate.get("aliases") or [])]
    variants = {re.sub(r"20\d{2}", "", normal(name)) for name in names if normal(name)}
    for source, replacements in (aliases or {}).items():
        source_key = normal(source)
        values = replacements if isinstance(replacements, list) else [replacements]
        for current in list(variants):
            if source_key and source_key in current:
                variants.update(current.replace(source_key, normal(value)) for value in values if normal(value))
    return {item for item in variants if len(item) >= 4}


def _venue_requirements(venue: object, aliases: dict | None = None) -> list[tuple[str, set[str]]]:
    result: list[tuple[str, set[str]]] = []
    for part in re.split(r"[、,/；;|]+", str(venue or "")):
        label = re.sub(r"[（(].*?[）)]", "", part).strip()
        base = normal(label).removeprefix("上海市").removeprefix("上海")
        if len(base) < 4 or any(word in label for word in ("通常", "待定", "各大", "具体", "未知")):
            continue
        terms = {base}
        for source, replacements in (aliases or {}).items():
            source_key = normal(source).removeprefix("上海市").removeprefix("上海")
            if source_key and (source_key in base or base in source_key):
                values = replacements if isinstance(replacements, list) else [replacements]
                terms.update(normal(value).removeprefix("上海市").removeprefix("上海") for value in values)
        result.append((label, {term for term in terms if len(term) >= 4}))
    return result


def _explicit_dates(text: str, fallback_year: int) -> tuple[set[tuple[date, date]], set[date]]:
    ranges: set[tuple[date, date]] = set()
    dates: set[date] = set()
    for year, month, day, end_month, end_day in re.findall(
        r"(?:(20\d{2})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日?\s*(?:至|到|[-–—~、])\s*(?:(\d{1,2})\s*月\s*)?(\d{1,2})\s*日", text
    ):
        try:
            pair = (date(int(year or fallback_year), int(month), int(day)),
                    date(int(year or fallback_year), int(end_month or month), int(end_day)))
            ranges.add(pair)
            dates.update(pair)
        except ValueError:
            pass
    for y, m, d, ey, em, ed in re.findall(
        r"(20\d{2})-(\d{1,2})-(\d{1,2})\s*(?:至|到|[~–—])\s*(20\d{2})-(\d{1,2})-(\d{1,2})", text
    ):
        try:
            pair = (date(int(y), int(m), int(d)), date(int(ey), int(em), int(ed)))
            ranges.add(pair)
            dates.update(pair)
        except ValueError:
            pass
    for year, month, day in re.findall(r"(?:(20\d{2})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日", text):
        try:
            dates.add(date(int(year or fallback_year), int(month), int(day)))
        except ValueError:
            pass
    for year, month, day in re.findall(r"(20\d{2})-(\d{1,2})-(\d{1,2})", text):
        try:
            dates.add(date(int(year), int(month), int(day)))
        except ValueError:
            pass
    return ranges, dates


def _single_venue_schedule_conflict(
    text: str,
    *,
    start: date,
    end: date,
    requirement: tuple[str, set[str]],
) -> bool:
    """Treat a different explicit held-at schedule as a conflict, not partial support."""
    _label, venue_terms = requirement
    for sentence in re.split(r"[。！？!?；;\n]+", text):
        clean = normal(sentence)
        if not any(term in clean for term in venue_terms) or not any(word in sentence for word in ("举办", "举行", "开赛", "上演")):
            continue
        ranges, dates = _explicit_dates(sentence, start.year)
        if ranges and (start, end) not in ranges and not (start in dates and end in dates):
            return True
    return False


def validate_page(candidate: dict, evidence: dict, text: str, *, origin: str,
                  aliases: dict | None = None, identity_aliases: dict | None = None) -> dict:
    """Validate individual critical fields on the actual page, never on a model synthesis."""
    checked = datetime.now(timezone.utc).isoformat()
    result = {"rule": RULE_VERSION, "checked_at": checked, "supported": False, "conflict": False, "origin": origin}
    try:
        start = date.fromisoformat(str(candidate.get("event_start_date"))[:10])
        end = date.fromisoformat(str(candidate.get("event_end_date"))[:10])
    except ValueError:
        return {**result, "reason": "missing_dates"}
    name = str(candidate.get("event_name") or candidate.get("title") or "")
    requirements = _venue_requirements(candidate.get("venue"), aliases)
    if not requirements:
        return {**result, "reason": "ambiguous_venue"}
    clean = normal(text)
    title_ok = same_event({"event_name": name}, {"event_name": evidence.get("title") or ""})
    identity_variants = _identity_variants(candidate, identity_aliases)
    body_identity = any(identity in clean for identity in identity_variants)
    if not title_ok and not body_identity:
        return {**result, "reason": "different_event"}
    if str(start.year) not in clean or ("上海" not in text and "shanghai" not in clean):
        return {**result, "reason": "missing_city_or_year"}
    passage = event_passage(candidate, text, identity_aliases=identity_aliases)[:12_000]
    if not passage:
        return {**result, "reason": "ambiguous_event_passage"}
    if any(word in passage for word in ("取消举办", "赛事取消", "演出取消", "延期举办", "延期至", "改期至", "变更场馆")):
        return {**result, "conflict": True, "reason": "cancellation_or_change", "excerpt": passage[:1700]}
    ranges, dates = _explicit_dates(passage, start.year)
    date_supported = (start, end) in ranges or (start in dates and end in dates)
    passage_key = normal(passage)
    matched_venues = [label for label, terms in requirements if any(term in passage_key for term in terms)]
    if (
        not date_supported
        and len(requirements) == 1
        and _single_venue_schedule_conflict(
            passage, start=start, end=end, requirement=requirements[0]
        )
    ):
        return {**result, "conflict": True, "reason": "date_conflict", "excerpt": passage[:1700]}
    if not date_supported and not matched_venues:
        return {**result, "reason": "no_critical_fields", "excerpt": passage[:1700]}
    evidence_url = str(evidence.get("url") or "")
    publisher = domain(evidence_url)
    host = (urlsplit(evidence_url).hostname or "").lower()
    authorized_ticketing = host in AUTHORIZED_TICKETING_HOSTS
    body = normal(passage)
    source_level = str(evidence.get("source_level") or "discovery")
    return {**result, "supported": True, "event_name": name, "publisher": publisher,
            "source_level": source_level, "overall_dates": date_supported,
            "event_start_date": start.isoformat() if date_supported else None,
            "event_end_date": end.isoformat() if date_supported else None,
            "required_venues": [label for label, _terms in requirements], "matched_venues": matched_venues,
            "professional": source_level in {"official", "authoritative", "media"}
                            or publisher in PUBLISHERS or publisher.endswith(".gov.cn") or authorized_ticketing,
            "authorized_ticketing": authorized_ticketing,
            "excluded": publisher in EXCLUDED and not authorized_ticketing,
            "fingerprint": hashlib.sha256(body.encode()).hexdigest(), "excerpt": passage[:1700]}


def assess(candidate: dict, source_rows: list[dict], *, min_domains: int = 3) -> dict:
    accepted: list[dict] = []
    publishers: set[str] = set()
    conflict = False
    now = datetime.now(timezone.utc)
    for row in sorted(source_rows, key=lambda item: not (item.get("raw", {}).get("source_validation", {}).get("professional"))):
        raw = row.get("raw") or {}
        validation = raw.get("source_validation") or {}
        if validation.get("rule") not in {RULE_VERSION, LEGACY_RULE_VERSION}:
            continue
        try:
            checked = datetime.fromisoformat(validation["checked_at"])
            if not now - timedelta(days=14) <= checked <= now + timedelta(minutes=5):
                continue
        except (KeyError, ValueError, TypeError):
            continue
        conflict |= bool(validation.get("conflict"))
        if (not validation.get("supported") or validation.get("excluded")
                or (validation.get("rule") == RULE_VERSION and validation.get("origin") != "page")
                or not validation.get("fingerprint") or not validation.get("excerpt")):
            continue
        if validation.get("rule") == LEGACY_RULE_VERSION:
            if any(str(validation.get(field) or "") != str(candidate.get(field) or "") for field in ("event_start_date", "event_end_date", "venue")):
                continue
            validation = {**validation, "overall_dates": True,
                          "required_venues": [label for label, _terms in _venue_requirements(candidate.get("venue"))],
                          "matched_venues": [label for label, _terms in _venue_requirements(candidate.get("venue"))]}
        publisher = domain(row.get("source_url", ""))
        if not publisher or publisher in publishers:
            continue
        passage = normal(validation.get("excerpt"))
        if any(validation.get("fingerprint") == old.get("fingerprint") or SequenceMatcher(None, passage, normal(old.get("excerpt")), autojunk=False).ratio() >= 0.90 for old in accepted):
            continue
        accepted.append(validation)
        publishers.add(publisher)
    professional = any(item.get("professional") for item in accepted)
    required_venues = [label for label, _terms in _venue_requirements(candidate.get("venue"))]
    date_domains = sorted(item["publisher"] for item in accepted if item.get("overall_dates"))
    venue_domains = {venue: sorted(item["publisher"] for item in accepted if venue in item.get("matched_venues", []))
                     for venue in required_venues}
    field_coverage = len(date_domains) >= 2 and bool(required_venues) and all(venue_domains[venue] for venue in required_venues)
    official_complete = any(item.get("source_level") == "official" and item.get("overall_dates")
                            and all(venue in item.get("matched_venues", []) for venue in required_venues)
                            for item in accepted)
    return {"domains": sorted(publishers), "count": len(publishers), "professional": professional,
            "evidence_as_of": min((item["checked_at"] for item in accepted), default=None),
            "field_coverage": {"overall_dates": date_domains, "venues": venue_domains, "complete": field_coverage},
            "official_complete": official_complete, "conflict": conflict,
            "eligible": len(publishers) >= max(2, min_domains) and professional and field_coverage and not conflict}
