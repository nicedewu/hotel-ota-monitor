from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

try:
    from dotenv import dotenv_values
except ModuleNotFoundError:  # Allows payload tests to run before optional local dependencies are installed.
    dotenv_values = None

try:
    import psycopg
    from psycopg.types.json import Jsonb
except ModuleNotFoundError:  # Keeps payload validation usable before server dependencies are installed.
    psycopg = None
    Jsonb = None


DEFAULT_PAYLOAD_PATH = "output/latest/hermes_payload.json"
DB_ENV_PATH = (Path.home() / ".hotel_agent/db.env")


class DBWriterError(RuntimeError):
    """A user-facing failure while preparing or writing OTA price rows."""


def write_ota_room_prices_from_payload(
    payload_path: str = DEFAULT_PAYLOAD_PATH,
    *,
    run_id: str | None = None,
    collected_at: datetime | None = None,
) -> int:
    """Append Ctrip room prices from one Hermes payload, including explicit sold-out rows."""
    payload = _read_payload(Path(payload_path))
    rows = collect_ota_room_price_rows(payload)
    if not rows:
        return 0

    run_id = str(run_id or payload.get("run_id") or uuid4().hex)
    collected_at = collected_at or _payload_captured_at(payload)
    for row in rows:
        row["run_id"] = run_id
        row["collected_at"] = collected_at

    database_url = _load_database_url()
    _write_rows(database_url, rows)
    return len(rows)


def collect_ota_room_price_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return rows from Hermes rooms arrays, keeping explicit sold-out records as available=False."""
    rows: list[dict[str, Any]] = []
    for role, field in (("own", "own_hotel"), ("own", "own_hotels"), ("competitor", "competitor_hotels")):
        for hotel, fallback_key in _hotel_objects(payload.get(field), field):
            for room in _scraped_rooms(hotel):
                row = _room_to_row(payload, hotel, room, role, fallback_key)
                if row is not None:
                    rows.append(row)
    return rows


def _read_payload(payload_path: Path) -> dict[str, Any]:
    if not payload_path.is_file():
        raise DBWriterError(f"payload 文件不存在: {payload_path}")
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DBWriterError(f"payload JSON 格式错误: {exc}") from exc
    if not isinstance(payload, dict):
        raise DBWriterError("payload JSON 顶层必须是对象")
    return payload


def _hotel_objects(value: Any, field: str) -> Iterator[tuple[dict[str, Any], str]]:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                yield item, field
        return
    if not isinstance(value, dict):
        return
    if "available_rooms" in value or any(key in value for key in ("hotel_key", "key", "id", "hotel_name", "name", "title")):
        yield value, field
        return
    for key, item in value.items():
        if isinstance(item, dict):
            yield item, str(key)


def _scraped_rooms(hotel: dict[str, Any]) -> Iterator[dict[str, Any]]:
    rooms = hotel.get("rooms")
    if not isinstance(rooms, list):
        rooms = hotel.get("available_rooms")
    if not isinstance(rooms, list):
        return
    for room in rooms:
        if isinstance(room, dict):
            yield room


def _room_to_row(
    payload: dict[str, Any], hotel: dict[str, Any], room: dict[str, Any], role: str, fallback_key: str
) -> dict[str, Any] | None:
    state = _availability_state(room)
    room_name = _first_text(room, "room_name", "name", "title")
    price = _positive_number(room)
    if not room_name:
        return None
    if state is True and price is None:
        return None

    hotel_key = _first_text(hotel, "hotel_key", "key", "id") or fallback_key
    hotel_name = _first_text(hotel, "hotel_name", "name", "title")
    stay_date = _first_text(room, "stay_date", "checkin_date", "date") or _first_text(
        hotel, "stay_date", "checkin_date", "date"
    ) or _first_text(payload, "stay_date", "checkin_date", "date") or date.today().isoformat()
    source_url = _first_text(room, "source_url", "url") or _first_text(hotel, "source_url", "url")
    raw = {
        "hotel_key": hotel_key,
        "hotel_name": hotel_name,
        "hotel_role": role,
        "hotel_source_url": _first_text(hotel, "source_url", "url"),
        "room": room,
        "payload_captured_at": payload.get("captured_at"),
        "payload_run_id": payload.get("run_id"),
    }
    return {
        "run_id": "",
        "platform": "ctrip",
        "hotel_key": hotel_key,
        "source_hotel_key": hotel_key,
        "hotel_name": hotel_name,
        "stay_date": stay_date,
        "room_name": room_name,
        "room_category": _first_text(room, "room_category", "category", "type") or _classify_room_name(room_name),
        "normalized_room_type": _first_text(room, "normalized_room_type", "room_category", "category", "type")
        or _classify_room_name(room_name),
        "price": price,
        "available": state,
        "breakfast": _first_text(room, "breakfast", "breakfast_desc"),
        "cancel_policy": _first_text(room, "cancel_policy", "cancel_desc"),
        "source_url": source_url,
        "raw": raw,
        "raw_payload": raw,
        "collected_at": None,
    }


def _first_text(data: dict[str, Any], *fields: str) -> str:
    for field in fields:
        value = data.get(field)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _positive_number(room: dict[str, Any]) -> float | None:
    for field in ("price", "final_price", "sale_price", "amount"):
        value = room.get(field)
        if isinstance(value, bool) or value in (None, ""):
            continue
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        if price > 0:
            return price
    return None


def _availability_state(room: dict[str, Any]) -> bool | None:
    """True=可订，False=明确不可订（满房/售罄），None=无法判定（不参与满房判断）。"""
    status = str(room.get("status") or "").strip().lower()
    if any(token in status for token in ("soldout", "sold_out", "unavailable", "售罄", "不可订", "满房", "无房")):
        return False
    if status != "success":
        return None
    if "available" not in room:
        return None
    value = room.get("available")
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "available", "bookable"}:
        return True
    if any(token in text for token in ("可订", "在售", "仅剩", "预订", "有房")):
        return True
    if text in {"0", "no"} or text.startswith("false"):
        return False
    if any(token in text for token in ("售罄", "不可订", "满房", "无房")):
        return False
    return None


def _payload_captured_at(payload: dict[str, Any]) -> datetime:
    value = str(payload.get("captured_at") or "").strip()
    if not value:
        raise DBWriterError("payload 缺少 captured_at，拒绝把未知时间的价格写为最新数据")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DBWriterError(f"payload captured_at 格式错误: {value}") from exc
    if parsed.tzinfo is None:
        raise DBWriterError("payload captured_at 必须包含时区")
    return parsed


def _classify_room_name(room_name: str) -> str:
    if "大床" in room_name:
        return "大床"
    if "双床" in room_name:
        return "双床"
    if "电竞" in room_name:
        return "电竞"
    if "家庭" in room_name:
        return "家庭"
    return "其他"


def _load_database_url() -> str:
    if not DB_ENV_PATH.is_file():
        raise DBWriterError(f"数据库配置文件不存在: {DB_ENV_PATH}")
    if dotenv_values is not None:
        database_url = dotenv_values(DB_ENV_PATH).get("DATABASE_URL")
    else:
        database_url = _read_database_url_without_dotenv(DB_ENV_PATH)
    if not isinstance(database_url, str) or not database_url.strip():
        raise DBWriterError("数据库配置文件中的 DATABASE_URL 为空")
    return database_url.strip()


def _read_database_url_without_dotenv(env_path: Path) -> str | None:
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped.removeprefix("export ").lstrip()
        key, separator, value = stripped.partition("=")
        if key.strip() != "DATABASE_URL" or not separator:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value
    return None


def _write_rows(database_url: str, rows: list[dict[str, Any]]) -> None:
    if psycopg is None or Jsonb is None:
        raise DBWriterError("缺少 psycopg 依赖，请执行 pip install -r requirements.txt")
    try:
        connection = psycopg.connect(database_url)
    except Exception as exc:
        raise DBWriterError(f"数据库连接失败: {exc}") from exc

    statement = """
        INSERT INTO ota_room_prices
            (run_id, collected_at, platform, hotel_key, source_hotel_key, hotel_name,
             stay_date, room_name, room_category, normalized_room_type, price, available,
             breakfast, cancel_policy, source_url, raw, raw_payload)
        VALUES
            (%(run_id)s, %(collected_at)s, %(platform)s, %(hotel_key)s,
             %(source_hotel_key)s, %(hotel_name)s, %(stay_date)s, %(room_name)s,
             %(room_category)s, %(normalized_room_type)s, %(price)s, %(available)s,
             %(breakfast)s, %(cancel_policy)s, %(source_url)s, %(raw)s, %(raw_payload)s)
    """
    try:
        with connection:
            with connection.cursor() as cursor:
                for row in rows:
                    cursor.execute(
                        statement,
                        {**row, "raw": Jsonb(row["raw"]), "raw_payload": Jsonb(row["raw_payload"])},
                    )
    except Exception as exc:
        raise DBWriterError(f"数据库写入失败: {exc}") from exc
