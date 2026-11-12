from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from datetime import date, timedelta
from typing import Any

from src.db_writer import _load_database_url, psycopg
from src.event_demand_context import load_future_event_demand_context
from src.pms_snapshot import _pms_agent_dir


class EventDemandWatchError(RuntimeError):
    pass


def _targets(cursor: Any, anchor_date: date) -> dict[str, list[date]]:
    cursor.execute("SELECT hotel_key FROM hotels WHERE hotel_role = 'own' AND is_active = TRUE ORDER BY hotel_key")
    targets: dict[str, list[date]] = {}
    for row in cursor.fetchall():
        hotel_key = str(row["hotel_key"])
        context = load_future_event_demand_context(cursor, hotel_key, anchor_date)
        dates = {
            day
            for event in context["events"] if event.get("priority") == "high"
            for day in _scan_days(event.get("event_start_date"), event.get("event_end_date"), anchor_date, 30)
        }
        if dates:
            targets[hotel_key] = sorted(dates)
    return targets


def _event_days(start: Any, end: Any) -> list[date]:
    if not isinstance(start, date) or not isinstance(end, date):
        return []
    return [start.fromordinal(value) for value in range(start.toordinal(), end.toordinal() + 1)]


def _scan_days(start: Any, end: Any, anchor_date: date, days: int) -> list[date]:
    """Keep explicit stay dates inside the downstream scanner's accepted horizon."""
    window_end = anchor_date + timedelta(days=days)
    return [day for day in _event_days(start, end) if anchor_date < day <= window_end]


def _claim(cursor: Any, hotel_key: str, advice: dict[str, Any]) -> bool:
    key = hashlib.sha256(f"{hotel_key}|{advice['event_reference']}|{advice['stay_date']}".encode()).hexdigest()
    cursor.execute(
        """INSERT INTO event_demand_snapshot_triggers (trigger_key, hotel_key, event_reference, stay_date, payload)
           VALUES (%s, %s, %s, %s, %s::jsonb) ON CONFLICT (trigger_key) DO NOTHING RETURNING id""",
        (key, hotel_key, advice["event_reference"], advice["stay_date"], __import__("json").dumps(advice, ensure_ascii=False, default=str)),
    )
    return cursor.fetchone() is not None


def run_event_demand_watch(*, anchor_date: date, dry_run: bool = False) -> dict[str, Any]:
    if psycopg is None:
        raise EventDemandWatchError("缺少 psycopg 依赖")
    with psycopg.connect(_load_database_url(), row_factory=psycopg.rows.dict_row) as connection:
        with connection.cursor() as cursor:
            targets = _targets(cursor, anchor_date)
        for hotel_key, stay_dates in targets.items():
            project_dir = _pms_agent_dir()
            command = [
                os.getenv("PMS_PYTHON", str(project_dir / ".venv/bin/python")),
                str(project_dir / "main.py"),
                "future-demand-scan", "--hotel-key", hotel_key, "--days", "30", "--force",
            ]
            for stay_date in stay_dates:
                command.extend(["--stay-date", stay_date.isoformat()])
            if not dry_run:
                subprocess.run(command, check=True, timeout=900)
        with connection.cursor() as cursor:
            selected: list[str] = []
            for hotel_key in targets:
                context = load_future_event_demand_context(cursor, hotel_key, anchor_date)
                for advice in context["price_control_advice"]:
                    if dry_run or _claim(cursor, hotel_key, advice):
                        selected.append(hotel_key)
        connection.commit()
    return {"targets": {key: [item.isoformat() for item in value] for key, value in targets.items()}, "triggered_hotels": sorted(set(selected))}
