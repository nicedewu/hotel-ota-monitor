#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.memory_provider import (
    existing_memory_contents,
    memory_backend_status,
    remember_global,
    remember_hotel,
)


DEFAULT_INPUTS = (
    Path("output/memory_staging/mnemosyne_pending.jsonl"),
    Path("output/memory_staging/existing_memory_export.jsonl"),
    Path("output/memory_staging/timescale_experience_candidates.jsonl"),
)
DEFAULT_REPORT = Path("output/memory_staging/import_report.json")
DEFAULT_FAILED = Path("output/memory_staging/mnemosyne_import_failed.jsonl")
DEFAULT_CLEANED = Path("output/memory_staging/mnemosyne_import_cleaned.jsonl")

CONFIDENCE = {"low": 1, "medium": 2, "high": 3}
CASE_TYPES = {"pricing_case", "data_quality_case", "event_pricing_case"}
RULE_TYPES = {"safety_rule", "pricing_rule", "hotel_rule", "room_baseline_price", "user_preference"}
RULE_MARKERS = ("禁止", "不能", "不得", "只允许", "必须", "应", "不要", "不允许", "优先", "需要")
BUSINESS_TERMS = ("pms", "ota", "携程", "飞书", "调价", "价格", "入住率", "销售率", "可售", "库存", "竞品", "展会", "酒店", "房型")
SENSITIVE_PATTERNS = (
    re.compile(r"(?i)\b(?:api[ _-]?key|token|cookie|webhook|secret|password|authorization|bearer|xoxb|database_url|private[ _-]?key)\b"),
    re.compile(r"(?i)(?:postgres(?:ql)?|mysql|redis)://[^\s]+"),
    re.compile(r"-----BEGIN [A-Z ]*(?:PRIVATE KEY|CERTIFICATE)-----"),
    re.compile(r"(?i)open\.feishu\.cn/open-apis/bot/v2/hook/"),
)
INTERNAL_PATH = re.compile(r"(?i)(?:/(?:home|Users)/|[A-Z]:\\Users\\|(?:^|\s)(?:src|scripts|deploy|output)/[^\s]+)")
LOW_VALUE = re.compile(r"(?i)(traceback \(most recent call last\)|exit code:|script running with cell id|^\{.*\}$)")


def normalize_content(content: str) -> str:
    normalized = unicodedata.normalize("NFKC", content).strip().lower()
    normalized = re.sub(r"\s*\[(?:dates|durations):[^\]]*\]\s*$", "", normalized)
    return re.sub(r"\s+", " ", normalized)


def audit_inputs(paths: Iterable[Path]) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    files: dict[str, Any] = {}
    for path in paths:
        if not path.is_file():
            files[str(path)] = {"status": "missing", "lines": 0, "valid": 0, "invalid": 0}
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        valid = 0
        invalid = 0
        for line_number, line in enumerate(lines, 1):
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                invalid += 1
                failed.append(_failed_ref(path, line_number, "invalid_json"))
                continue
            if not isinstance(item, dict):
                invalid += 1
                failed.append(_failed_ref(path, line_number, "json_not_object"))
                continue
            valid += 1
            item = dict(item)
            item["_input_file"] = str(path)
            item["_line_number"] = line_number
            records.append(item)
        files[str(path)] = {"status": "found", "lines": len(lines), "valid": valid, "invalid": invalid}
    return records, files, failed


def clean_records(
    records: list[dict[str, Any]],
    *,
    scope: str | None = None,
    hotel_key: str | None = None,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], Counter[str], list[dict[str, Any]]]:
    skipped: Counter[str] = Counter()
    failed: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    exact: dict[tuple[str, str | None, str, str], int] = {}

    for raw in records:
        content = str(raw.get("content") or "").strip()
        record_scope = str(raw.get("scope") or "").strip().lower()
        record_hotel = str(raw.get("hotel_key") or "").strip() or None
        memory_type = str(raw.get("memory_type") or "").strip().lower()
        source = str(raw.get("source") or "").strip()
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}

        if scope and record_scope != scope:
            skipped["scope_filter"] += 1
            continue
        if hotel_key and record_hotel != hotel_key:
            skipped["hotel_filter"] += 1
            continue
        if not content or record_scope not in {"global", "hotel"} or not memory_type:
            failed.append(_failed_raw(raw, "missing_required_field"))
            continue
        if record_scope == "hotel" and not record_hotel:
            failed.append(_failed_raw(raw, "hotel_scope_without_hotel_key"))
            continue
        if "reflection" in memory_type:
            skipped["legacy_reflection_disabled"] += 1
            continue
        serialized_metadata = json.dumps(metadata, ensure_ascii=False, default=str)
        if _sensitive(content) or _sensitive(serialized_metadata):
            skipped["sensitive"] += 1
            continue
        if INTERNAL_PATH.search(content) or INTERNAL_PATH.search(serialized_metadata):
            skipped["internal_path"] += 1
            continue
        if not _high_value(content, memory_type):
            skipped["low_value"] += 1
            continue

        cleaned = {
            "scope": record_scope,
            "hotel_key": record_hotel,
            "memory_type": memory_type,
            "content": re.sub(r"\s+", " ", content).strip(),
            "source": "timescale" if source == "timescale" else "legacy_memory",
            "confidence": str(raw.get("confidence") or "medium").lower(),
            "created_at": str(raw.get("created_at") or datetime.now(timezone.utc).isoformat()),
            "metadata": metadata,
        }
        key = (record_scope, record_hotel, memory_type, normalize_content(content))
        if key in exact:
            skipped["duplicate"] += 1
            index = exact[key]
            if _record_rank(cleaned) > _record_rank(accepted[index]):
                accepted[index] = cleaned
            continue

        near_index = _near_duplicate_index(accepted, cleaned)
        if near_index is not None:
            skipped["near_duplicate"] += 1
            if _record_rank(cleaned) > _record_rank(accepted[near_index]):
                accepted[near_index] = cleaned
            continue
        exact[key] = len(accepted)
        accepted.append(cleaned)
        if limit is not None and len(accepted) >= limit:
            break
    return accepted, skipped, failed


def import_records(records: list[dict[str, Any]], *, apply: bool) -> tuple[Counter[str], list[dict[str, Any]]]:
    results: Counter[str] = Counter()
    failed: list[dict[str, Any]] = []
    if not apply:
        results["dry_run_candidates"] = len(records)
        return results, failed

    status = memory_backend_status()
    if not status["module_available"] or not status["enabled"] or status["write_mode"] != "direct":
        results["backend_unavailable"] = len(records)
        return results, failed

    existing: dict[tuple[str, str | None], set[str]] = {}
    for record in records:
        target = (str(record["scope"]), record.get("hotel_key"))
        if target not in existing:
            existing[target] = {normalize_content(content) for content in existing_memory_contents(*target)}
        normalized = normalize_content(str(record["content"]))
        if normalized in existing[target]:
            results["already_imported"] += 1
            continue
        if record["scope"] == "global":
            outcome = remember_global(
                str(record["content"]),
                str(record["memory_type"]),
                source=str(record["source"]),
                confidence=str(record["confidence"]),
                metadata=dict(record["metadata"]),
            )
        else:
            outcome = remember_hotel(
                str(record["hotel_key"]),
                str(record["content"]),
                str(record["memory_type"]),
                source=str(record["source"]),
                confidence=str(record["confidence"]),
                metadata=dict(record["metadata"]),
            )
        if outcome == "direct":
            results["imported"] += 1
            existing[target].add(normalized)
        else:
            results[f"write_{outcome}"] += 1
            failed.append({"reason": f"write_{outcome}", "scope": record["scope"], "hotel_key": record.get("hotel_key"), "memory_type": record["memory_type"]})
    return results, failed


def run(
    inputs: list[Path],
    *,
    apply: bool = False,
    scope: str | None = None,
    hotel_key: str | None = None,
    limit: int | None = None,
    report_path: Path = DEFAULT_REPORT,
    failed_path: Path = DEFAULT_FAILED,
    cleaned_path: Path = DEFAULT_CLEANED,
) -> dict[str, Any]:
    raw, files, parse_failed = audit_inputs(inputs)
    cleaned, skipped, validation_failed = clean_records(raw, scope=scope, hotel_key=hotel_key, limit=limit)
    results, write_failed = import_records(cleaned, apply=apply)
    failed = parse_failed + validation_failed + write_failed

    cleaned_path.parent.mkdir(parents=True, exist_ok=True)
    cleaned_path.write_text("".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in cleaned), encoding="utf-8")
    failed_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in failed), encoding="utf-8")
    report = {
        "mode": "apply" if apply else "dry-run",
        "completed": bool(apply and results.get("backend_unavailable", 0) == 0 and not write_failed),
        "files": files,
        "raw_records": len(raw),
        "cleaned_records": len(cleaned),
        "scope": dict(Counter(str(row["scope"]) for row in cleaned)),
        "hotel_key": dict(Counter(str(row.get("hotel_key")) for row in cleaned)),
        "memory_type": dict(Counter(str(row["memory_type"]) for row in cleaned)),
        "skipped": dict(skipped),
        "failed": len(failed),
        "results": dict(results),
        "backend": memory_backend_status(),
        "cleaned_file": str(cleaned_path),
        "failed_file": str(failed_path),
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _sensitive(text: str) -> bool:
    return any(pattern.search(text) for pattern in SENSITIVE_PATTERNS)


def _high_value(content: str, memory_type: str) -> bool:
    lowered = content.lower()
    if LOW_VALUE.search(content) or len(content) < 8:
        return False
    if memory_type in CASE_TYPES:
        return True
    if memory_type == "general_note":
        return any(word in content for word in ("偏好", "习惯", "长期")) and len(content) >= 12
    if memory_type not in RULE_TYPES:
        return False
    term_count = sum(term in lowered for term in BUSINESS_TERMS)
    if memory_type == "safety_rule":
        return term_count >= 1 and any(marker in content for marker in RULE_MARKERS)
    return term_count >= 2 or (term_count >= 1 and any(marker in content for marker in RULE_MARKERS))


def _record_rank(record: dict[str, Any]) -> tuple[int, str]:
    return CONFIDENCE.get(str(record.get("confidence")), 2), str(record.get("created_at") or "")


def _near_duplicate_index(records: list[dict[str, Any]], candidate: dict[str, Any]) -> int | None:
    candidate_content = normalize_content(str(candidate["content"]))
    for index, record in enumerate(records):
        if (record["scope"], record.get("hotel_key"), record["memory_type"]) != (
            candidate["scope"], candidate.get("hotel_key"), candidate["memory_type"]
        ):
            continue
        if SequenceMatcher(None, normalize_content(str(record["content"])), candidate_content).ratio() >= 0.96:
            return index
    return None


def _failed_ref(path: Path, line_number: int, reason: str) -> dict[str, Any]:
    return {"input_file": str(path), "line_number": line_number, "reason": reason}


def _failed_raw(raw: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "input_file": str(raw.get("_input_file") or ""),
        "line_number": raw.get("_line_number"),
        "reason": reason,
        "scope": raw.get("scope"),
        "hotel_key": raw.get("hotel_key"),
        "memory_type": raw.get("memory_type"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="清洗 staging JSONL 并导入 Mnemosyne")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--input", type=Path, action="append", dest="inputs")
    parser.add_argument("--hotel-key")
    parser.add_argument("--scope", choices=("global", "hotel"))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    if args.limit is not None and args.limit <= 0:
        raise SystemExit("--limit 必须大于 0")
    report = run(
        args.inputs or list(DEFAULT_INPUTS),
        apply=args.apply,
        scope=args.scope,
        hotel_key=args.hotel_key,
        limit=args.limit,
        report_path=args.report,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.apply and not report["completed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
