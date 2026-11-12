#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.memory_provider import contains_sensitive_data


DEFAULT_SOURCES = (
    (Path.home() / ".hermes/memories/MEMORY.md"),
    (Path.home() / ".hermes/memories/USER.md"),
    (Path.home() / ".hermes/SOUL.md"),
)
DEFAULT_OUTPUT = Path("output/memory_staging/existing_memory_export.jsonl")
SAFETY_KEYWORDS = ("飞书", "安全", "禁止", "不能", "删除", "覆盖", "代码", "路径", "API Key")
PRICING_KEYWORDS = ("PMS", "价格接口", "OTA", "携程", "竞品", "调价", "入住率", "可售", "展会")
HOTEL_KEYWORDS = ("own_demo_a", "示例自营酒店")


def classify_note(content: str) -> tuple[str, str, str | None]:
    if any(keyword.lower() in content.lower() for keyword in SAFETY_KEYWORDS):
        return "safety_rule", "global", None
    if any(keyword.lower() in content.lower() for keyword in PRICING_KEYWORDS):
        memory_type = "hotel_rule" if any(keyword.lower() in content.lower() for keyword in HOTEL_KEYWORDS) else "pricing_rule"
        return memory_type, "hotel", "own_demo_a"
    return "general_note", "global", None


def iter_notes(text: str) -> Iterable[str]:
    for line in text.splitlines():
        note = line.strip().lstrip("-* ").strip()
        if note and not note.startswith("#"):
            yield note


def export_existing_memory(
    source_paths: Iterable[Path] = DEFAULT_SOURCES,
    output_path: Path = DEFAULT_OUTPUT,
    *,
    dry_run: bool = False,
) -> dict[str, object]:
    records: list[dict[str, object]] = []
    read_files: list[str] = []
    skipped_sensitive = 0
    created_at = datetime.now(timezone.utc).isoformat()
    for source_path in source_paths:
        path = Path(source_path)
        if not path.is_file():
            continue
        read_files.append(str(path))
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for content in iter_notes(text):
            if contains_sensitive_data(content):
                skipped_sensitive += 1
                continue
            memory_type, scope, hotel_key = classify_note(content)
            records.append(
                {
                    "scope": scope,
                    "hotel_key": hotel_key,
                    "memory_type": memory_type,
                    "content": content,
                    "source": str(path),
                    "confidence": "medium",
                    "created_at": created_at,
                    "metadata": {"migration_source": path.name},
                }
            )

    if not dry_run:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
            encoding="utf-8",
        )
    return {
        "read_files": read_files,
        "exported": len(records),
        "memory_types": dict(Counter(str(record["memory_type"]) for record in records)),
        "skipped_sensitive": skipped_sensitive,
        "output": str(output_path),
        "dry_run": dry_run,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="只读导出 Hermes 旧记忆为 Mnemosyne staging JSONL")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(json.dumps(export_existing_memory(output_path=args.output, dry_run=args.dry_run), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
