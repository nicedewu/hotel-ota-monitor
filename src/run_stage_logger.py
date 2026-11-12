from __future__ import annotations

import json
import logging
from typing import Any

from src.db_writer import Jsonb, _load_database_url, psycopg


SECRET_TOKENS = ("secret", "token", "cookie", "webhook", "password", "database_url", "api_key", "env")
logger = logging.getLogger(__name__)


def table_exists(conn: Any, table_name: str) -> bool:
    with conn.cursor() as cursor:
        cursor.execute("SELECT to_regclass(%s) IS NOT NULL AS exists", (f"public.{table_name}",))
        row = cursor.fetchone()
        return bool(row and row["exists"])


def start_stage(run_id: str, own_hotel_key: str | None, stage_name: str, metadata: dict[str, Any] | None = None) -> int | None:
    with _connect() as conn:
        if not table_exists(conn, "agent_run_stages"):
            raise RuntimeError("agent_run_stages 表不存在，拒绝伪造阶段成功")
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO agent_run_stages (run_id, own_hotel_key, stage_name, stage_status, metadata)
                VALUES (%s, %s, %s, 'running', %s)
                RETURNING id
                """,
                (run_id, own_hotel_key, stage_name, Jsonb(_safe_metadata(metadata))),
            )
            row = cursor.fetchone()
        conn.commit()
        return int(row["id"]) if row else None


def finish_stage(stage_id: int | None, status: str = "success", message: str | None = None, metadata: dict[str, Any] | None = None) -> None:
    if stage_id is None:
        return
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE agent_run_stages
                SET stage_status = %s, finished_at = now(), message = %s,
                    metadata = COALESCE(metadata, '{}'::jsonb) || %s
                WHERE id = %s
                """,
                (status, message, Jsonb(_safe_metadata(metadata)), stage_id),
            )
        conn.commit()


def fail_stage(stage_id: int | None, error_message: str, metadata: dict[str, Any] | None = None) -> None:
    if stage_id is None:
        return
    try:
        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE agent_run_stages
                    SET stage_status = 'failed', finished_at = now(), error_message = %s,
                        metadata = COALESCE(metadata, '{}'::jsonb) || %s
                    WHERE id = %s
                    """,
                    (error_message, Jsonb(_safe_metadata(metadata)), stage_id),
                )
            conn.commit()
    except Exception:
        logger.exception("无法记录失败阶段 stage_id=%s", stage_id)


def _connect() -> Any:
    if psycopg is None or Jsonb is None:
        raise RuntimeError("psycopg unavailable")
    return psycopg.connect(_load_database_url(), row_factory=psycopg.rows.dict_row)


def _safe_metadata(value: Any) -> Any:
    try:
        json.dumps(value or {}, ensure_ascii=False, default=str)
    except TypeError:
        value = str(value)
    return _redact(value or {})


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            key_text = str(key)
            if any(token in key_text.lower() for token in SECRET_TOKENS):
                result[key_text] = "<redacted>"
            else:
                result[key_text] = _redact(item)
        return result
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value
