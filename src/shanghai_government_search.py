"""Bounded adapter for Shanghai's public government-site search."""
from __future__ import annotations

from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


class ShanghaiGovernmentSearchError(RuntimeError):
    """A safe error raised when the official discovery endpoint is unavailable."""


class _ResultParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._anchor = ""
        self._title = ""
        self._title_parts: list[str] = []
        self._published_at = ""
        self._capture_date = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        classes = set(attributes.get("class", "").split())
        if tag == "a" and "restitle" in classes:
            self._anchor = "title"
            self._title = attributes.get("title", "").strip()
            self._title_parts = []
        elif tag == "a" and "url" in classes:
            self._anchor = "url"
            target = _official_https_url(attributes.get("href", ""))
            if target and self._title:
                self.results.append(
                    {
                        "url": target,
                        "title": self._title,
                        "summary": "",
                        "published_at": self._published_at,
                        "provider": "shanghai_government_search",
                        "source_level": "authoritative",
                        "discovered_via": "shanghai_government_search",
                    }
                )
                self._title = ""
                self._published_at = ""
        elif tag == "span" and "mobile-result-date" in classes:
            self._capture_date = True

    def handle_data(self, data: str) -> None:
        if self._anchor == "title":
            self._title_parts.append(data)
        elif self._capture_date:
            value = data.strip()
            if value:
                self._published_at = value

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._anchor == "title":
            if not self._title:
                self._title = "".join(self._title_parts).strip()
            self._anchor = ""
        elif tag == "a" and self._anchor == "url":
            self._anchor = ""
        elif tag == "span" and self._capture_date:
            self._capture_date = False


def _official_https_url(value: str) -> str:
    try:
        parsed = urlsplit(str(value).strip())
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower().strip(".")
    if parsed.scheme not in {"http", "https"} or not host or not host.endswith(".gov.cn"):
        return ""
    if parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
        return ""
    return urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))


def search(config: dict[str, Any], query: str, *, limit: int = 5) -> list[dict[str, str]]:
    settings = config.get("shanghai_government_search")
    if not isinstance(settings, dict) or not bool(settings.get("enabled", False)):
        return []
    endpoint = str(settings.get("endpoint") or "").strip()
    if endpoint != "https://search.sh.gov.cn/searchResult":
        raise ShanghaiGovernmentSearchError("上海政府搜索地址不在允许列表")
    try:
        timeout = max(3, min(int(settings.get("timeout_seconds", 15)), 30))
        page_size = max(5, min(int(settings.get("page_size", 20)), 20))
        limit = max(1, min(int(limit), 10))
    except (TypeError, ValueError) as exc:
        raise ShanghaiGovernmentSearchError("上海政府搜索配置无效") from exc
    body = urlencode(
        {
            "text": str(query).strip()[:160],
            "pageNo": 1,
            "newsPageNo": 1,
            "pageSize": page_size,
            "category7": "",
            "stealthy": 0,
            "showItemAgency": "false",
        }
    ).encode("utf-8")
    request = Request(
        endpoint,
        data=body,
        headers={
            "User-Agent": "hotel-demand-intelligence/1.0",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "Accept": "text/html,application/xhtml+xml",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read(1_000_001)
    except Exception as exc:
        raise ShanghaiGovernmentSearchError(f"上海政府搜索失败：{type(exc).__name__}") from exc
    if len(payload) > 1_000_000:
        raise ShanghaiGovernmentSearchError("上海政府搜索响应过大")
    try:
        html = payload.decode("utf-8")
    except UnicodeDecodeError:
        html = payload.decode("gb18030", errors="replace")
    parser = _ResultParser()
    parser.feed(html)
    seen: set[str] = set()
    results: list[dict[str, str]] = []
    for item in parser.results:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        results.append(item)
        if len(results) >= limit:
            break
    return results
