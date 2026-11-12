from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from src.db_writer import DBWriterError, _load_database_url, psycopg


class EventSourceHealthError(RuntimeError):
    """A safe operator-facing source-health failure."""


def new_run_id() -> str:
    return str(uuid4())


def entries_fingerprint(entries: list[dict[str, Any]]) -> str:
    payload = json.dumps(entries, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record_source_run(
    *,
    run_id: str,
    source_key: str,
    status: str,
    fetched_entries: int,
    matched_candidates: int,
    written_candidates: int,
    entries_hash: str = "",
    error_summary: str = "",
) -> None:
    if psycopg is None:
        raise EventSourceHealthError("缺少 psycopg 依赖，无法记录来源健康状态")
    try:
        with psycopg.connect(_load_database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO event_source_runs (
                        run_id, source_key, status, fetched_entries, matched_candidates,
                        written_candidates, entries_hash, error_summary, finished_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, NULLIF(%s, ''), NULLIF(%s, ''), now())
                    """,
                    (
                        run_id,
                        source_key,
                        status,
                        fetched_entries,
                        matched_candidates,
                        written_candidates,
                        entries_hash,
                        error_summary[:500],
                    ),
                )
    except DBWriterError as exc:
        raise EventSourceHealthError(str(exc)) from exc
    except Exception as exc:
        raise EventSourceHealthError(f"来源健康记录失败：{type(exc).__name__}") from exc


def critical_source_failures(source_keys: list[str], *, threshold: int = 2) -> list[dict[str, Any]]:
    if not source_keys:
        return []
    if psycopg is None:
        raise EventSourceHealthError("缺少 psycopg 依赖，无法检查来源健康状态")
    failures: list[dict[str, Any]] = []
    try:
        with psycopg.connect(_load_database_url(), row_factory=psycopg.rows.dict_row) as connection:
            with connection.cursor() as cursor:
                for source_key in source_keys:
                    cursor.execute(
                        """
                        SELECT status, error_summary, finished_at
                        FROM event_source_runs
                        WHERE source_key = %s
                        ORDER BY finished_at DESC, id DESC
                        LIMIT %s
                        """,
                        (source_key, threshold),
                    )
                    rows = [dict(row) for row in cursor.fetchall()]
                    if len(rows) == threshold and all(row["status"] == "failed" for row in rows):
                        failures.append(
                            {
                                "source_key": source_key,
                                "consecutive_failures": threshold,
                                "last_error": str(rows[0].get("error_summary") or ""),
                                "last_finished_at": rows[0].get("finished_at"),
                            }
                        )
    except DBWriterError as exc:
        raise EventSourceHealthError(str(exc)) from exc
    except Exception as exc:
        raise EventSourceHealthError(f"来源健康检查失败：{type(exc).__name__}") from exc
    return failures
