from __future__ import annotations

from src import shanghai_government_search


class _Response:
    def __init__(self, body: str) -> None:
        self.body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit: int) -> bytes:
        return self.body


def _config() -> dict:
    return {
        "shanghai_government_search": {
            "enabled": True,
            "endpoint": "https://search.sh.gov.cn/searchResult",
            "page_size": 20,
            "timeout_seconds": 15,
        }
    }


def test_search_returns_actual_government_pages_not_search_redirects(monkeypatch) -> None:
    html = """
      <div class="result result-elm search-result-card">
        <a href="/detail?token=opaque" class="restitle wl-link" title="无畏契约金秋抵沪">无畏契约金秋抵沪</a>
        <span class="mobile-result-date">2026-08-20</span>
        <a href="http://www.shanghai.gov.cn/events/vct.html" class="url">www.shanghai.gov.cn</a>
      </div>
      <div class="result result-elm search-result-card">
        <a href="/detail?token=other" class="restitle wl-link" title="外部网页">外部网页</a>
        <a href="https://example.com/not-official" class="url">example.com</a>
      </div>
    """
    captured = {}

    def fake_open(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = request.data.decode("utf-8")
        captured["timeout"] = timeout
        return _Response(html)

    monkeypatch.setattr(shanghai_government_search, "urlopen", fake_open)
    results = shanghai_government_search.search(_config(), "无畏契约全球冠军赛", limit=5)

    assert captured["url"] == "https://search.sh.gov.cn/searchResult"
    assert "%E6%97%A0%E7%95%8F%E5%A5%91%E7%BA%A6" in captured["body"]
    assert results == [
        {
            "url": "https://www.shanghai.gov.cn/events/vct.html",
            "title": "无畏契约金秋抵沪",
            "summary": "",
            "published_at": "2026-08-20",
            "provider": "shanghai_government_search",
            "source_level": "authoritative",
            "discovered_via": "shanghai_government_search",
        }
    ]


def test_search_rejects_unapproved_endpoint() -> None:
    config = _config()
    config["shanghai_government_search"]["endpoint"] = "https://example.com/search"

    try:
        shanghai_government_search.search(config, "活动")
    except shanghai_government_search.ShanghaiGovernmentSearchError as exc:
        assert "允许列表" in str(exc)
    else:
        raise AssertionError("unapproved endpoint must fail")
