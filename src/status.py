from __future__ import annotations

from dataclasses import dataclass

from .utils import count_keywords, text_contains_any


LOGIN_KEYWORDS = [
    "登录",
    "请登录",
    "手机号登录",
    "账号登录",
    "立即登录",
    "会员登录",
    "短信登录",
]

CAPTCHA_KEYWORDS = [
    "验证码",
    "安全验证",
    "滑块",
    "拖动滑块",
    "人机验证",
    "验证身份",
    "访问过于频繁",
    "风险",
    "异常访问",
    "短信验证",
    "请输入验证码",
]

PRICE_ROOM_KEYWORDS = [
    "房型",
    "大床",
    "双床",
    "入住",
    "离店",
    "早餐",
    "取消",
    "￥",
    "¥",
    "CNY",
    "RMB",
]

SESSION_EXPIRED_KEYWORDS = [
    "重新登录",
    "登录态失效",
    "会话已过期",
    "登录已过期",
    "请重新登录",
]


@dataclass(frozen=True)
class PageStatus:
    status: str
    login_keyword_count: int
    has_price_or_room: bool
    has_captcha: bool
    has_session_expired: bool


def detect_page_status(text: str = "", html: str = "") -> PageStatus:
    content = f"{text}\n{html}"
    has_captcha = text_contains_any(content, CAPTCHA_KEYWORDS)
    has_session_expired = text_contains_any(content, SESSION_EXPIRED_KEYWORDS)
    has_price_or_room = text_contains_any(content, PRICE_ROOM_KEYWORDS)
    login_keyword_count = count_keywords(content, LOGIN_KEYWORDS)

    if has_captcha:
        status = "captcha_required"
    elif has_session_expired:
        status = "session_expired"
    elif login_keyword_count >= 3 and not has_price_or_room:
        status = "login_required"
    elif has_price_or_room:
        status = "success"
    elif content.strip():
        status = "no_price"
    else:
        status = "page_error"

    return PageStatus(
        status=status,
        login_keyword_count=login_keyword_count,
        has_price_or_room=has_price_or_room,
        has_captcha=has_captcha,
        has_session_expired=has_session_expired,
    )


def profile_status(profile_dir: str) -> str:
    from pathlib import Path

    path = Path(profile_dir)
    if not path.exists():
        return f"profile_missing: {path}"
    files = [item for item in path.rglob("*") if item.is_file()]
    if files:
        return f"profile_exists: {path}，发现 {len(files)} 个文件，可能已保存登录态"
    return f"profile_empty: {path} 存在，但暂未发现登录态文件"
