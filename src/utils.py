from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


DEFAULT_PROFILE_DIR = Path("profiles/ctrip_manual_profile")
DEFAULT_OUTPUT_DIR = Path("output")
DEFAULT_CONFIG = Path("hotels.json")
DEFAULT_LOGIN_URL = "https://www.ctrip.com/"

JSON_LIKE_RE = re.compile(r"^\s*(\{.*\}|\[.*\])\s*$", re.DOTALL)


def ensure_dir(path: Path | str) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def today_str() -> str:
    return date.today().isoformat()


def resolve_stay_dates(start_date: str | None = None, days: int = 1) -> list[str]:
    if days < 1:
        raise ValueError("--days 必须大于等于 1")
    base = date.fromisoformat(start_date) if start_date else date.today()
    return [(base + timedelta(days=offset)).isoformat() for offset in range(days)]


def next_date_str(value: str) -> str:
    return (date.fromisoformat(value) + timedelta(days=1)).isoformat()


def apply_stay_date_to_url(url: str, stay_date: str) -> str:
    checkout = next_date_str(stay_date)
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(
        {
            "date": stay_date,
            "stayDate": stay_date,
            "checkin": stay_date,
            "checkout": checkout,
            "checkIn": stay_date,
            "checkOut": checkout,
        }
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def safe_filename(value: str, max_length: int = 90) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
    cleaned = cleaned.strip("._")
    return (cleaned or "response")[:max_length]


def looks_like_json_text(value: str) -> bool:
    if not value:
        return False
    if not JSON_LIKE_RE.match(value):
        return False
    try:
        json.loads(value)
        return True
    except json.JSONDecodeError:
        return False


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def text_contains_any(text: str, keywords: list[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def count_keywords(text: str, keywords: list[str]) -> int:
    return sum(text.count(keyword) for keyword in keywords)


def launch_options(channel: str, headless: bool) -> dict[str, Any]:
    options: dict[str, Any] = {
        "headless": headless,
        "locale": "zh-CN",
        "timezone_id": "Asia/Shanghai",
        "viewport": {"width": 1365, "height": 900},
    }
    if channel == "chrome":
        options["channel"] = "chrome"
    return options
