from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


COLUMNS = [
    "platform",
    "hotel_name",
    "stay_date",
    "checkin_date",
    "date",
    "source_type",
    "source",
    "status",
    "error_reason",
    "room_name",
    "room_type",
    "price",
    "currency",
    "breakfast",
    "cancel_policy",
    "available",
    "source_url",
    "response_url",
    "json_path",
    "captured_at",
    "note",
]


def normalize_records(records: Iterable[dict]) -> list[dict]:
    return [{column: record.get(column, "") for column in COLUMNS} for record in records]


def status_record(hotel_name: str, status: str, source_url: str, error_reason: str) -> dict:
    return {
        "platform": "ctrip",
        "hotel_name": hotel_name,
        "stay_date": "",
        "checkin_date": "",
        "date": "",
        "source_type": "status_record",
        "source": "",
        "status": status,
        "error_reason": error_reason,
        "room_name": "",
        "room_type": "",
        "price": "",
        "currency": "",
        "breakfast": "",
        "cancel_policy": "",
        "available": "",
        "source_url": source_url,
        "response_url": "",
        "json_path": "",
        "captured_at": "",
        "note": "",
    }


def export_rooms(records: Iterable[dict], output_dir: Path, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = normalize_records(records)
    metadata = metadata or {}

    json_path = output_dir / "ctrip_rooms.json"
    jsonl_path = output_dir / "ctrip_rooms.jsonl"
    markdown_path = output_dir / "ctrip_rooms.md"

    payload = {
        "metadata": {
            **metadata,
            "row_count": len(rows),
            "columns": COLUMNS,
        },
        "records": rows,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    markdown_path.write_text(_records_to_markdown(rows, payload["metadata"]), encoding="utf-8")

    return {
        "row_count": len(rows),
        "json_path": str(json_path),
        "jsonl_path": str(jsonl_path),
        "markdown_path": str(markdown_path),
    }


def _records_to_markdown(rows: list[dict], metadata: dict[str, Any]) -> str:
    title = metadata.get("hotel_name") or "携程房型价格结果"
    lines = [
        f"# {title}",
        "",
        "## Summary",
        "",
    ]
    for key in ("run_at", "url", "page_status", "row_count", "candidate_response_count", "saved_json_count"):
        if key in metadata:
            lines.append(f"- `{key}`: {metadata[key]}")
    lines.extend(["", "## Records", ""])
    if not rows:
        lines.append("_No records._")
        return "\n".join(lines) + "\n"

    display_columns = [
        "hotel_name",
        "stay_date",
        "source_type",
        "source",
        "status",
        "room_name",
        "price",
        "currency",
        "breakfast",
        "cancel_policy",
        "available",
        "note",
    ]
    lines.append("| " + " | ".join(display_columns) + " |")
    lines.append("| " + " | ".join("---" for _ in display_columns) + " |")
    for row in rows:
        values = [_escape_markdown_cell(row.get(column, "")) for column in display_columns]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines) + "\n"


def _escape_markdown_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ").replace("\r", " ")
