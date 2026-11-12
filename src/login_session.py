from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


REMINDER_AFTER = timedelta(days=25)
STATE_FILENAME = ".hotel_login_session.json"
AUTH_COOKIE_NAMES = ("cticket", "login_uid")
WEBKIT_EPOCH_OFFSET_SECONDS = 11_644_473_600


@dataclass(frozen=True)
class LoginReminder:
    cycle_id: str
    confirmed_at: datetime
    age_days: int


def record_login_confirmation(profile_dir: str | Path, *, confirmed_at: datetime | None = None) -> None:
    """Persist the successful manual-login time without storing any credential."""
    confirmed = _as_utc(confirmed_at or datetime.now(timezone.utc))
    _write_state(
        Path(profile_dir),
        {
            "schema_version": 1,
            "confirmed_at": _format_timestamp(confirmed),
            "reminder_sent_for": "",
        },
    )


def login_reminder_due(profile_dir: str | Path, *, now: datetime | None = None) -> LoginReminder | None:
    """Return one reminder per login cycle after the session has been used for 25 days."""
    profile_path = Path(profile_dir)
    current = _as_utc(now or datetime.now(timezone.utc))
    state = _read_state(profile_path)
    confirmed = _parse_timestamp(str(state.get("confirmed_at") or ""))
    if confirmed is None:
        # Existing profiles predate this state file. Bootstrap from Chrome's
        # auth-cookie creation time, while never reading cookie values.
        confirmed = _read_auth_cookie_created_at(profile_path)
    if confirmed is None or current < confirmed + REMINDER_AFTER:
        return None
    cycle_id = _format_timestamp(confirmed)
    if str(state.get("reminder_sent_for") or "") == cycle_id:
        return None
    return LoginReminder(
        cycle_id=cycle_id,
        confirmed_at=confirmed,
        age_days=max(0, int((current - confirmed).total_seconds() // 86_400)),
    )


def mark_login_reminder_sent(profile_dir: str | Path, reminder: LoginReminder) -> None:
    profile_path = Path(profile_dir)
    state = _read_state(profile_path)
    if _parse_timestamp(str(state.get("confirmed_at") or "")) is None:
        state["confirmed_at"] = reminder.cycle_id
    state["schema_version"] = 1
    state["reminder_sent_for"] = reminder.cycle_id
    _write_state(profile_path, state)


def _read_auth_cookie_created_at(profile_path: Path) -> datetime | None:
    cookie_path = profile_path / "Default" / "Network" / "Cookies"
    if not cookie_path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"{cookie_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            row = connection.execute(
                """
                SELECT creation_utc
                FROM cookies
                WHERE name IN (?, ?) AND host_key LIKE ?
                ORDER BY creation_utc DESC
                LIMIT 1
                """,
                (*AUTH_COOKIE_NAMES, "%ctrip.com"),
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    if not row or not isinstance(row[0], int) or row[0] <= 0:
        return None
    return datetime.fromtimestamp(row[0] / 1_000_000 - WEBKIT_EPOCH_OFFSET_SECONDS, tz=timezone.utc)


def _read_state(profile_path: Path) -> dict[str, object]:
    state_path = profile_path / STATE_FILENAME
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_state(profile_path: Path, state: dict[str, object]) -> None:
    profile_path.mkdir(parents=True, exist_ok=True)
    state_path = profile_path / STATE_FILENAME
    temporary_path = state_path.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary_path.replace(state_path)


def _parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return _as_utc(parsed)


def _format_timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
