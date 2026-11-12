from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.memory_provider import memory_backend_status, recall_hotel, remember_hotel


def select_verified_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected = []
    seen: set[str] = set()
    for item in candidates:
        try:
            confidence = float(item.get("confidence", 0))
        except (TypeError, ValueError):
            continue
        evidence_refs = list(dict.fromkeys(
            str(ref).strip() for ref in item.get("evidence_refs") or [] if str(ref).strip()
        ))
        content = str(item.get("content") or "").strip()
        if (
            confidence < 0.75
            or len(evidence_refs) < 2
            or not content
            or not str(item.get("conditions") or "").strip()
            or not str(item.get("applicability") or "").strip()
            or content in seen
        ):
            continue
        seen.add(content)
        selected.append({**item, "evidence_refs": evidence_refs})
    return selected[:2]


def persist_memory_candidates(
    *,
    hotel_key: str,
    hotel_name: str,
    week_start: str,
    week_end: str,
    candidates: list[dict[str, Any]],
    per_hotel_path: Path,
    dry_run: bool,
    write_memory: bool,
) -> dict[str, Any]:
    records = []
    for item in select_verified_candidates(candidates):
        content = str(item.get("content") or "").strip()
        if not content or _already_remembered(hotel_key, content):
            continue
        confidence = _confidence_label(item.get("confidence", 0.4))
        room_category = str(item.get("room_category") or item.get("normalized_room_type") or "").strip()
        metadata = {
            "tenant": "hotel_ops",
            "hotel_key": hotel_key,
            "hotel_short_name": hotel_name,
            "rule_type": str(item.get("rule_type") or "pricing_learning"),
            "scope": "room_type" if room_category else "hotel",
            "priority": _priority(item.get("priority")),
            "source": "weekly_review",
            "confidence": confidence,
            "valid_from": week_start,
            "valid_to": None,
            "week_start": week_start,
            "week_end": week_end,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "evidence_refs": item.get("evidence_refs") or [],
            "evidence_count": len(item.get("evidence_refs") or []),
            "conditions": str(item.get("conditions") or ""),
            "applicability": str(item.get("applicability") or ""),
        }
        if room_category:
            metadata["room_category"] = room_category
        records.append({
            "content": content,
            "metadata": metadata,
        })
    staging_path = Path("data/mnemosyne_staging") / f"weekly_review_{week_start}_{week_end}.jsonl"
    if not records:
        return {"status": "skipped_no_candidates", "count": 0, "path": str(staging_path)}
    _write_jsonl(per_hotel_path, records)
    if dry_run:
        return {"status": "dry_run_staging", "count": len(records), "path": str(per_hotel_path)}
    _append_jsonl(staging_path, records)
    if not write_memory:
        return {"status": "dry_run_staging" if dry_run else "pending_import", "count": len(records), "path": str(staging_path)}

    backend = memory_backend_status()
    if not (backend["enabled"] and backend["module_available"] and backend["write_mode"] == "direct"):
        return {"status": "pending_import", "count": len(records), "path": str(staging_path)}

    written = 0
    for record in records:
        outcome = remember_hotel(
            hotel_key,
            record["content"],
            "pricing_lesson",
            source="weekly_review",
            confidence=record["metadata"]["confidence"],
            metadata=record["metadata"],
        )
        if outcome == "direct":
            written += 1
    return {"status": "written" if written == len(records) else "partial_failed", "count": len(records), "written": written, "path": str(staging_path)}


def _already_remembered(hotel_key: str, content: str) -> bool:
    try:
        return any(str(row.get("content") or "").strip() == content for row in recall_hotel(hotel_key, content, limit=10))
    except Exception:
        return False


def _confidence_label(value: Any) -> str:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return "medium"
    if score >= 0.75:
        return "high"
    if score >= 0.45:
        return "medium"
    return "low"


def _priority(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 50


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in rows), encoding="utf-8")


def _append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
