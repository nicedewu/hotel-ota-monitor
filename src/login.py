from __future__ import annotations

import asyncio
import time
from pathlib import Path

from playwright.async_api import async_playwright

from .status import PageStatus, detect_page_status
from .login_session import record_login_confirmation
from .utils import ensure_dir, launch_options

POLL_INTERVAL_SECONDS = 15
LOGIN_TIMEOUT_MINUTES = 20
CONFIRM_POLLS = 2

EXIT_LOGIN_NOT_CONFIRMED = 2


def login_confirmed(page_status: PageStatus) -> bool:
    """明确已登录：页面 success，且无任何登录、验证码、会话失效信号。

    detect_page_status 的 success 只要求出现价格/房型关键词；登录弹窗覆盖在
    酒店页上时仍可能是 success，因此必须同时要求 login_keyword_count == 0。
    传入的 page_status 必须基于可见文本（detect_page_status(text, "")）：
    已登录页的 HTML 仍含隐藏登录组件（实测登录词底噪 3），混入 HTML 会导致
    永远无法确认。
    """
    return (
        page_status.status == "success"
        and page_status.login_keyword_count == 0
        and not page_status.has_captcha
        and not page_status.has_session_expired
    )


async def run_login(url: str, channel: str, profile_dir: str) -> str:
    profile_path = ensure_dir(Path(profile_dir))
    debug_dir = ensure_dir(Path("output/debug"))

    confirmed: PageStatus | None = None
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_path),
            **launch_options(channel, headless=False),
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)

            print("请在打开的浏览器中手动登录携程；遇到验证码、短信或安全验证请手动处理。")
            print("程序不会自动处理验证码或绕过验证。")
            print(
                f"程序每 {POLL_INTERVAL_SECONDS} 秒自动检测登录状态，"
                f"连续 {CONFIRM_POLLS} 次确认后自动收尾；"
                f"{LOGIN_TIMEOUT_MINUTES} 分钟未确认将以退出码 {EXIT_LOGIN_NOT_CONFIRMED} 结束。"
                "登录完成后请刷新酒店页面。"
            )

            confirmed = await _poll_until_confirmed(
                context,
                page,
                debug_dir,
                timeout_seconds=LOGIN_TIMEOUT_MINUTES * 60,
                interval_seconds=POLL_INTERVAL_SECONDS,
                confirm_polls=CONFIRM_POLLS,
            )
        finally:
            try:
                await context.close()
            except Exception:
                pass

    print(f"profile 目录: {profile_path.resolve()}")
    if confirmed is None:
        print("登录未确认：超时或浏览器被关闭。可查看 output/debug/login_check_text.txt 判断页面状态后重试。")
        raise SystemExit(EXIT_LOGIN_NOT_CONFIRMED)
    record_login_confirmation(profile_path)
    print("登录已确认，登录态已保存，会话将自动清理。")
    return confirmed.status


async def _poll_until_confirmed(
    context,
    page,
    debug_dir: Path,
    *,
    timeout_seconds: float,
    interval_seconds: float,
    confirm_polls: int,
) -> PageStatus | None:
    deadline = time.monotonic() + timeout_seconds
    streak = 0
    while time.monotonic() < deadline:
        await asyncio.sleep(interval_seconds)
        if page.is_closed():
            open_pages = [item for item in context.pages if not item.is_closed()]
            if not open_pages:
                print("浏览器页面已全部关闭，停止检测。")
                return None
            page = open_pages[0]
        text = await _safe_text(page)
        html = await _safe_html(page)
        # 登录确认只依据可见文本：HTML 中的隐藏登录组件是固定底噪，会阻断确认。
        page_status = detect_page_status(text, "")
        (debug_dir / "login_check_text.txt").write_text(text, encoding="utf-8")
        (debug_dir / "login_check_page.html").write_text(html, encoding="utf-8")
        stamp = time.strftime("%H:%M:%S")
        if login_confirmed(page_status):
            streak += 1
            print(f"[{stamp}] 状态 {page_status.status}，已确认 {streak}/{confirm_polls} 次")
            if streak >= confirm_polls:
                return page_status
        else:
            streak = 0
            print(
                f"[{stamp}] 状态 {page_status.status}"
                f"（登录词 {page_status.login_keyword_count}，验证码 {page_status.has_captcha}，"
                f"会话失效 {page_status.has_session_expired}），继续等待人工登录……"
            )
    print("等待登录确认超时。")
    return None


async def _safe_text(page) -> str:
    try:
        return await page.locator("body").inner_text(timeout=10000)
    except Exception:
        return ""


async def _safe_html(page) -> str:
    try:
        return await page.content()
    except Exception:
        return ""
