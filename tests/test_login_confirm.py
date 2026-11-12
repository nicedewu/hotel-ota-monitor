from __future__ import annotations

import asyncio
from pathlib import Path

from src.login import _poll_until_confirmed, login_confirmed
from src.status import PageStatus, detect_page_status


def _status(**overrides) -> PageStatus:
    values = {
        "status": "success",
        "login_keyword_count": 0,
        "has_price_or_room": True,
        "has_captcha": False,
        "has_session_expired": False,
    }
    values.update(overrides)
    return PageStatus(**values)


def test_login_confirmed_clean_success():
    assert login_confirmed(_status())


def test_login_overlay_not_confirmed():
    # 登录弹窗覆盖在酒店页上：价格关键词仍在，status 为 success，但存在登录信号。
    page_status = detect_page_status("请登录 手机号登录 房型 大床 ¥399")
    assert page_status.status == "success"
    assert not login_confirmed(page_status)


def test_captcha_not_confirmed():
    assert not login_confirmed(_status(status="captcha_required", has_captcha=True))
    assert not login_confirmed(_status(has_captcha=True))


def test_session_expired_not_confirmed():
    assert not login_confirmed(_status(has_session_expired=True))


def test_non_success_not_confirmed():
    assert not login_confirmed(_status(status="no_price", has_price_or_room=False))


class _StubPage:
    def __init__(self, texts: list[str], html: str = ""):
        self._texts = list(texts)
        self._html = html
        self._index = 0

    def is_closed(self) -> bool:
        return False

    def locator(self, selector: str):
        return self

    async def inner_text(self, timeout=None) -> str:
        text = self._texts[min(self._index, len(self._texts) - 1)]
        self._index += 1
        return text

    async def content(self) -> str:
        return self._html


class _StubContext:
    pages: list = []


def test_poll_requires_consecutive_confirmations(tmp_path: Path):
    # 确认 1 次 → 出现登录信号重置 → 再连续确认 2 次才返回。
    page = _StubPage(
        [
            "房型 大床 ¥399",
            "请登录 房型 大床 ¥399",
            "房型 大床 ¥399",
            "房型 大床 ¥399",
        ]
    )
    result = asyncio.run(
        _poll_until_confirmed(
            _StubContext(),
            page,
            tmp_path,
            timeout_seconds=5,
            interval_seconds=0,
            confirm_polls=2,
        )
    )
    assert result is not None and login_confirmed(result)
    assert page._index == 4
    assert (tmp_path / "login_check_text.txt").exists()


def test_poll_ignores_hidden_login_markup_in_html(tmp_path: Path):
    # 已登录页的 HTML 仍含隐藏登录组件（实测底噪 3 个登录词）；确认只看可见文本。
    page = _StubPage(
        ["房型 大床 ¥399", "房型 大床 ¥399"],
        html="<div hidden>请登录 手机号登录 立即登录</div>",
    )
    result = asyncio.run(
        _poll_until_confirmed(
            _StubContext(),
            page,
            tmp_path,
            timeout_seconds=5,
            interval_seconds=0,
            confirm_polls=2,
        )
    )
    assert result is not None and login_confirmed(result)


def test_poll_times_out_without_login(tmp_path: Path):
    page = _StubPage(["请登录 手机号登录 立即登录"])
    result = asyncio.run(
        _poll_until_confirmed(
            _StubContext(),
            page,
            tmp_path,
            timeout_seconds=0.2,
            interval_seconds=0.05,
            confirm_polls=2,
        )
    )
    assert result is None
