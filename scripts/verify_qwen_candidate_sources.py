from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.db_writer import _load_database_url, psycopg
from src.event_corroboration import assess
from src.event_discovery import _load_config, _verify_candidate_sources
from src.event_intelligence import upsert_event_candidate


def _candidate_rows(candidate_ids: list[int], limit: int) -> list[dict]:
    where = "AND id = ANY(%s)" if candidate_ids else ""
    params = (candidate_ids,) if candidate_ids else ()
    query = f"""
        SELECT *, primary_source_url AS source_url,
               'qwen-native-discovery' AS source_key,
               event_name AS source_title, NULL::timestamptz AS published_at
        FROM event_candidates
        WHERE raw->'entry'->>'provider' = 'qwen_native'
          AND city = '上海' AND canonical_event_id IS NULL
          AND event_status NOT IN ('cancelled', 'promoted')
          AND event_end_date >= CURRENT_DATE
          AND event_start_date <= CURRENT_DATE + interval '90 days'
          {where}
        ORDER BY CASE WHEN lower(COALESCE(raw->'entry'->>'impact_level', '')) IN ('high','高','极高')
                      THEN 0 ELSE 1 END,
                 event_start_date, id
        LIMIT {int(limit)}
    """
    with psycopg.connect(_load_database_url(), row_factory=psycopg.rows.dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]


def _summary(candidate: dict) -> dict:
    rows = [{"source_url": item.get("url"), "raw": {"source_validation": item.get("validation") or {}}}
            for item in candidate.get("source_observations", [])]
    result = assess(candidate, rows, min_domains=int(candidate.get("corroboration_min_domains", 2)))
    result["candidate_id"] = candidate["id"]
    result["event_name"] = candidate["event_name"]
    result["pages"] = [{"domain": item.get("url", "").split("/")[2] if "/" in item.get("url", "") else "",
                        "title": str(item.get("title") or "")[:160],
                        "supported": bool(item.get("validation", {}).get("supported")),
                        "source_level": item.get("validation", {}).get("source_level") or item.get("source_level"),
                        "reason": item.get("validation", {}).get("reason"),
                        "origin": item.get("validation", {}).get("origin"),
                        "overall_dates": bool(item.get("validation", {}).get("overall_dates")),
                        "matched_venues": item.get("validation", {}).get("matched_venues") or [],
                        "excerpt": str(item.get("validation", {}).get("excerpt") or "")[:240]}
                       for item in candidate.get("source_observations", [])]
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate Qwen event evidence pages without promoting canonical events")
    parser.add_argument("--config", default="config/event_discovery.json")
    parser.add_argument("--candidate-id", action="append", type=int, default=[])
    parser.add_argument("--alias", action="append", default=[])
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.limit <= 5:
        parser.error("--limit must be between 1 and 5")
    config = _load_config(str(Path(args.config)))
    output = []
    for candidate in _candidate_rows(args.candidate_id, args.limit):
        raw = candidate.get("raw") if isinstance(candidate.get("raw"), dict) else {}
        entry = raw.get("entry") if isinstance(raw.get("entry"), dict) else {}
        existing_aliases = entry.get("aliases") if isinstance(entry.get("aliases"), list) else []
        aliases = [
            str(item).strip()
            for item in [*existing_aliases, *args.alias]
            if str(item).strip()
        ]
        aliases = list(dict.fromkeys(aliases))
        candidate["aliases"] = aliases
        candidate["raw"] = {**raw, "entry": {**entry, "aliases": aliases}}
        mode = str(entry.get("offline_mode") or "").lower()
        if candidate.get("offline_mode") == "unknown" and ("线下" in mode or mode in {"offline", "hybrid"}):
            candidate["offline_mode"] = "hybrid" if mode == "hybrid" or "线上" in mode else "offline"
        candidate["corroboration_min_domains"] = int(config.get("corroboration_min_domains", 2))
        enriched = _verify_candidate_sources(candidate, config)
        result = _summary(enriched)
        if args.apply:
            upsert_event_candidate(enriched)
            result["applied"] = True
        output.append(result)
    print(json.dumps({"mode": "apply" if args.apply else "dry_run", "results": output}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
