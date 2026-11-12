from __future__ import annotations

import json
import importlib.util
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_STAGING_FILE = Path("output/memory_staging/mnemosyne_pending.jsonl")
DEFAULT_CONFLICT_FILE = Path("output/memory_staging/mnemosyne_conflicts.jsonl")
DEFAULT_ENV_FILE = (Path.home() / ".hotel_agent/mnemosyne.env")
_HOTEL_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|token|password|passwd|cookie|secret|database_url)\b\s*[:=]\s*[\"']?[^\s,\"'}]{6,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"https://open\.feishu\.cn/open-apis/bot/v2/hook/[A-Za-z0-9_-]+", re.I),
)
_SECRET_KEYS = {"api_key", "apikey", "token", "access_token", "refresh_token", "password", "passwd", "cookie", "secret", "webhook", "database_url"}
_CONFIDENCE_IMPORTANCE = {"low": 0.3, "medium": 0.6, "high": 0.9}
_CONFIDENCE_RANK = {"low": 1, "medium": 2, "high": 3}
_RULE_SCOPES = {"global": 0, "hotel": 1, "room_type": 2, "event": 3}
_RULE_MEMORY_TYPES = {"pricing_lesson", "pricing_rule", "business_rule"}
_RULE_REQUIRED_METADATA = ("tenant", "hotel_key", "hotel_short_name", "rule_type", "scope", "priority", "source", "confidence", "valid_from", "valid_to")
GLOBAL_HOTEL_KEY = "GLOBAL"


def remember_global(
    content: str,
    memory_type: str,
    source: str | None = None,
    confidence: str = "medium",
    metadata: dict[str, Any] | None = None,
) -> str:
    return _remember("global", None, content, memory_type, source, confidence, metadata)


def remember_hotel(
    hotel_key: str,
    content: str,
    memory_type: str,
    source: str | None = None,
    confidence: str = "medium",
    metadata: dict[str, Any] | None = None,
) -> str:
    if not _HOTEL_KEY.fullmatch(hotel_key.strip()):
        return "rejected"
    return _remember("hotel", hotel_key.strip(), content, memory_type, source, confidence, metadata)


def recall_global(query: str, limit: int = 5) -> list[dict[str, Any]]:
    return _recall("global", None, query, limit)


def recall_hotel(hotel_key: str, query: str, limit: int = 5) -> list[dict[str, Any]]:
    if not _HOTEL_KEY.fullmatch(hotel_key.strip()):
        return []
    return _recall("hotel", hotel_key.strip(), query, limit)


def recall_for_pricing_decision(
    hotel_key: str,
    stay_date: str | None = None,
    room_type: str | None = None,
    query: str | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    terms = [query or "酒店调价规则 安全约束 竞品价格 入住率 可售 展会"]
    if stay_date:
        terms.append(str(stay_date))
    if room_type:
        terms.append(str(room_type))
    effective_query = " ".join(terms)
    combined = recall_global(effective_query, limit) + recall_hotel(hotel_key, effective_query, limit)
    return filter_and_sort_pricing_rules(combined, hotel_key, stay_date, room_type, limit)


def filter_and_sort_pricing_rules(
    items: list[dict[str, Any]],
    hotel_key: str,
    stay_date: str | None = None,
    room_type: str | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    allowed = {str(hotel_key).strip(), GLOBAL_HOTEL_KEY}
    filtered: list[dict[str, Any]] = []
    for item in items:
        metadata = _item_rule_metadata(item)
        rule_hotel_key = str(metadata.get("hotel_key") or item.get("hotel_key") or "").strip()
        if not rule_hotel_key and item.get("scope") == "global":
            rule_hotel_key = GLOBAL_HOTEL_KEY
            metadata["hotel_key"] = rule_hotel_key
        if (
            rule_hotel_key not in allowed
            or not _has_complete_rule_metadata(metadata)
            or not _valid_for_date(metadata, stay_date)
            or not _room_type_applies(metadata, room_type)
            or metadata.get("conflict_status") == "conflict"
        ):
            continue
        item = dict(item)
        item["metadata"] = metadata
        filtered.append(item)
    filtered.sort(
        key=lambda item: (
            _rule_sort_key(item.get("metadata", {}), room_type),
            _relevance_score(item.get("relevance_score")),
        ),
        reverse=True,
    )
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in filtered:
        content = str(item.get("content") or "").strip()
        if content and content not in seen:
            seen.add(content)
            unique.append(item)
    return unique[:max(0, limit)]


def get_memory_setting(name: str, default: str = "") -> str:
    return _setting(name, default)


def memory_backend_status() -> dict[str, Any]:
    return {
        "enabled": _enabled(),
        "write_mode": _setting("MNEMOSYNE_WRITE_MODE", "staging").strip().lower(),
        "module_available": importlib.util.find_spec("mnemosyne") is not None,
        "primary_backend": _setting("MEMORY_PRIMARY_BACKEND", "staging").strip().lower(),
        "auxiliary_backend": _setting("MEMORY_AUXILIARY_BACKEND", "none").strip().lower(),
    }


def memory_count(scope: str, hotel_key: str | None = None) -> int:
    """Return records the provider can actually read, not legacy write totals."""
    return int(memory_health(scope, hotel_key).get("recallable_records") or 0)


def memory_health(scope: str, hotel_key: str | None = None) -> dict[str, Any]:
    """Expose usable records separately from Mnemosyne's legacy write count."""
    try:
        memory = _mnemosyne_instance(scope, hotel_key)
        stats = memory.get_stats() or {}
        beam_rows = memory.get_all_memories() or []
        legacy_rows = _legacy_memory_rows(memory, scope, hotel_key)
    except Exception:
        return {
            "status": "unavailable",
            "stored_records": 0,
            "recallable_records": 0,
            "beam_visible_records": 0,
            "active_pricing_rules": 0,
            "records_without_complete_rule_metadata": 0,
        }

    merged = _merge_memory_rows(beam_rows, legacy_rows)
    recallable = [
        row for row in merged
        if str(row.get("content") or "").strip()
        and not contains_sensitive_data(str(row.get("content") or ""), _row_outer_metadata(row))
    ]
    active_rules = sum(
        1
        for row in recallable
        if _has_complete_rule_metadata(_row_rule_metadata(row, scope, hotel_key))
        and _valid_for_date(_row_rule_metadata(row, scope, hotel_key), date.today().isoformat())
    )
    return {
        "status": "ok",
        "stored_records": int(stats.get("total_memories") or len(legacy_rows)),
        "recallable_records": len(recallable),
        "beam_visible_records": len(beam_rows),
        "active_pricing_rules": active_rules,
        "records_without_complete_rule_metadata": max(0, len(recallable) - active_rules),
        "last_memory": stats.get("last_memory"),
    }


def existing_memory_contents(scope: str, hotel_key: str | None = None) -> set[str]:
    try:
        memory = _mnemosyne_instance(scope, hotel_key)
        rows = _merge_memory_rows(memory.get_all_memories() or [], _legacy_memory_rows(memory, scope, hotel_key))
        return {str(row.get("content") or "").strip() for row in rows if isinstance(row, dict)}
    except Exception:
        return set()


def contains_sensitive_data(content: str, metadata: dict[str, Any] | None = None) -> bool:
    if any(pattern.search(content) for pattern in _SECRET_PATTERNS):
        return True

    def has_secret_key(value: Any) -> bool:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
                if normalized in _SECRET_KEYS and item not in (None, "", [], {}):
                    return True
                if has_secret_key(item):
                    return True
        elif isinstance(value, (list, tuple)):
            return any(has_secret_key(item) for item in value)
        return False

    return has_secret_key(metadata or {})


def _row_outer_metadata(row: dict[str, Any]) -> dict[str, Any]:
    metadata = row.get("metadata") or row.get("metadata_json") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            metadata = {}
    return dict(metadata) if isinstance(metadata, dict) else {}


def _legacy_memory_rows(memory: Any, scope: str, hotel_key: str | None) -> list[dict[str, Any]]:
    """Read the dual-write table because BEAM omits rule metadata in recall rows."""
    connection = getattr(memory, "conn", None)
    if connection is None:
        return []
    try:
        rows = connection.execute(
            "SELECT id, content, source, timestamp, session_id, importance, metadata_json FROM memories"
        ).fetchall()
    except Exception:
        return []
    result: list[dict[str, Any]] = []
    for raw in rows:
        try:
            item = dict(raw)
        except (TypeError, ValueError):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        outer_metadata = _row_outer_metadata(item)
        result.append(
            {
                "id": item.get("id"),
                "content": content,
                "source": item.get("source"),
                "timestamp": item.get("timestamp"),
                "importance": item.get("importance"),
                "scope": outer_metadata.get("scope", scope),
                "hotel_key": outer_metadata.get("hotel_key", hotel_key),
                "memory_type": outer_metadata.get("memory_type"),
                "confidence": outer_metadata.get("confidence"),
                "metadata": outer_metadata,
            }
        )
    return result


def _merge_memory_rows(primary_rows: list[dict[str, Any]], legacy_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep BEAM relevance fields while restoring metadata from the dual-write row."""
    legacy_by_id = {str(row.get("id")): row for row in legacy_rows if str(row.get("id") or "")}
    legacy_by_content = {str(row.get("content") or ""): row for row in legacy_rows if str(row.get("content") or "")}
    merged: dict[str, dict[str, Any]] = {}
    for row in [*primary_rows, *legacy_rows]:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        fallback = legacy_by_id.get(str(item.get("id") or "")) or legacy_by_content.get(str(item.get("content") or ""))
        if fallback:
            if not _row_outer_metadata(item):
                item["metadata"] = fallback.get("metadata", {})
            for key in ("scope", "hotel_key", "memory_type", "confidence"):
                if item.get(key) is None and fallback.get(key) is not None:
                    item[key] = fallback[key]
        identity = str(item.get("id") or item.get("content") or "")
        if identity:
            merged[identity] = item
    return list(merged.values())


def _remember(
    scope: str,
    hotel_key: str | None,
    content: str,
    memory_type: str,
    source: str | None,
    confidence: str,
    metadata: dict[str, Any] | None,
) -> str:
    content = str(content).strip()
    memory_type = str(memory_type).strip()
    confidence = str(confidence).strip().lower()
    metadata = dict(metadata or {})
    if _is_rule_memory(memory_type, metadata):
        metadata = _normalize_rule_metadata(scope, hotel_key, memory_type, source, confidence, metadata)
        if not metadata:
            return "rejected"
    if not content or not memory_type or confidence not in _CONFIDENCE_IMPORTANCE:
        return "rejected"
    if contains_sensitive_data(content, metadata):
        return "rejected"

    record = {
        "scope": scope,
        "hotel_key": hotel_key,
        "memory_type": memory_type,
        "content": content,
        "source": source,
        "confidence": confidence,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "metadata": metadata,
    }
    if _is_rule_memory(memory_type, metadata):
        conflict = _find_conflicting_rule(scope, hotel_key, metadata, content)
        if conflict:
            _append_conflict({"created_at": record["created_at"], "status": "conflict", "new": record, "existing": conflict})
            return "conflict"
    if _direct_enabled():
        try:
            memory = _mnemosyne_instance(scope, hotel_key)
            memory.remember(
                content,
                source=source or memory_type,
                importance=_CONFIDENCE_IMPORTANCE[confidence],
                metadata={key: value for key, value in record.items() if key != "content"},
                scope="global",
            )
            return "direct"
        except Exception:
            pass
    return "staged" if _append_staging(record) else "failed"


def _recall(scope: str, hotel_key: str | None, query: str, limit: int) -> list[dict[str, Any]]:
    if not _enabled() or not str(query).strip() or limit <= 0:
        return []
    try:
        limit = min(limit, max(1, int(_setting("MNEMOSYNE_RECALL_LIMIT", "8"))))
    except ValueError:
        limit = min(limit, 8)
    try:
        memory = _mnemosyne_instance(scope, hotel_key)
    except Exception:
        return []
    try:
        recalled_rows = memory.recall(str(query), top_k=limit) or []
    except Exception:
        recalled_rows = []
    try:
        all_rows = memory.get_all_memories() or []
    except Exception:
        all_rows = []
    legacy_rows = _legacy_memory_rows(memory, scope, hotel_key)
    legacy_by_id = {str(row.get("id")): row for row in legacy_rows if str(row.get("id") or "")}
    legacy_by_content = {str(row.get("content") or ""): row for row in legacy_rows if str(row.get("content") or "")}
    terms = {term.lower() for term in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{2,}", str(query))}
    ranked: dict[str, tuple[int, float, dict[str, Any]]] = {}
    for native, rows in ((True, recalled_rows), (False, all_rows), (False, legacy_rows)):
        for row in rows:
            if not isinstance(row, dict):
                continue
            row = dict(row)
            fallback = legacy_by_id.get(str(row.get("id") or "")) or legacy_by_content.get(str(row.get("content") or ""))
            if fallback:
                if not _row_outer_metadata(row):
                    row["metadata"] = fallback.get("metadata", {})
                for key in ("scope", "hotel_key", "memory_type", "confidence"):
                    if row.get(key) is None and fallback.get(key) is not None:
                        row[key] = fallback[key]
            content = str(row.get("content") or "").strip()
            if not content:
                continue
            score = sum(term in content.lower() for term in terms)
            native_score = _relevance_score(row.get("score"))
            source = str(row.get("source") or "").lower()
            if score == 0 and native_score <= 0:
                continue
            rank = score * 1000 + int(native_score * 100) + (10 if native else 0) + (200 if source in {"manual", "feishu_analysis", "postmortem"} and score else 0)
            if content not in ranked or rank > ranked[content][0]:
                ranked[content] = (rank, score + native_score, row)
    result: list[dict[str, Any]] = []
    for _, relevance_score, row in sorted(ranked.values(), key=lambda item: item[0], reverse=True):
        content = str(row.get("content") or "").strip()
        metadata = _row_outer_metadata(row)
        if not content or contains_sensitive_data(content, metadata if isinstance(metadata, dict) else {}):
            continue
        metadata = metadata if isinstance(metadata, dict) else {}
        nested_metadata = metadata.get("metadata") if isinstance(metadata.get("metadata"), dict) else {}
        rule_metadata = {**(nested_metadata or metadata)}
        for key in ("hotel_key", "memory_type", "source", "confidence"):
            if key not in rule_metadata and metadata.get(key) is not None:
                rule_metadata[key] = metadata.get(key)
        if "scope" not in rule_metadata:
            rule_metadata["scope"] = metadata.get("scope", scope)
        result.append(
            {
                "content": content,
                "memory_id": row.get("id"),
                "relevance_score": relevance_score,
                "scope": metadata.get("scope", scope),
                "hotel_key": metadata.get("hotel_key", hotel_key),
                "memory_type": metadata.get("memory_type") or ("pricing_lesson" if row.get("source") in {"manual", "feishu_analysis", "postmortem"} else None),
                "source": metadata.get("source", row.get("source")),
                "confidence": metadata.get("confidence"),
                "metadata": rule_metadata,
            }
        )
    return result[:limit]


def _normalize_rule_metadata(
    storage_scope: str,
    hotel_key: str | None,
    memory_type: str,
    source: str | None,
    confidence: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    metadata = dict(metadata)
    expected_hotel_key = GLOBAL_HOTEL_KEY if storage_scope == "global" else str(hotel_key or "").strip()
    rule_hotel_key = str(metadata.get("hotel_key") or expected_hotel_key).strip()
    if not rule_hotel_key or (storage_scope == "hotel" and rule_hotel_key != expected_hotel_key):
        return {}
    if storage_scope == "global" and rule_hotel_key != GLOBAL_HOTEL_KEY:
        return {}
    metadata.update(
        {
            "tenant": str(metadata.get("tenant") or _setting("MNEMOSYNE_TENANT", "default")).strip(),
            "hotel_key": rule_hotel_key,
            "hotel_short_name": str(metadata.get("hotel_short_name") or metadata.get("hotel_name") or rule_hotel_key).strip(),
            "rule_type": str(metadata.get("rule_type") or memory_type).strip(),
            "scope": str(metadata.get("scope") or ("global" if storage_scope == "global" else "hotel")).strip(),
            "priority": _priority(metadata.get("priority")),
            "source": str(metadata.get("source") or source or memory_type).strip(),
            "confidence": _confidence_label(metadata.get("confidence") or confidence),
            "valid_from": str(metadata.get("valid_from") or metadata.get("stay_date") or metadata.get("week_start") or date.today().isoformat()),
            "valid_to": metadata.get("valid_to") if "valid_to" in metadata else None,
        }
    )
    return metadata if all(key in metadata for key in _RULE_REQUIRED_METADATA) else {}


def _find_conflicting_rule(scope: str, hotel_key: str | None, metadata: dict[str, Any], content: str) -> dict[str, Any]:
    room_categories = _room_categories(metadata)
    if not room_categories:
        return {}
    try:
        rows = _mnemosyne_instance(scope, hotel_key).get_all_memories() or []
    except Exception:
        return {}
    normalized = _normalize_text(content)
    for row in rows:
        if not isinstance(row, dict):
            continue
        existing_content = str(row.get("content") or "").strip()
        existing_metadata = _row_rule_metadata(row, scope, hotel_key)
        if not existing_content or existing_metadata.get("conflict_status") == "conflict":
            continue
        if str(existing_metadata.get("hotel_key") or "") != str(metadata.get("hotel_key") or ""):
            continue
        if str(existing_metadata.get("rule_type") or "") != str(metadata.get("rule_type") or ""):
            continue
        if room_categories.isdisjoint(_room_categories(existing_metadata)):
            continue
        if _normalize_text(existing_content) != normalized:
            return {"content": existing_content[:240], "metadata": existing_metadata}
    return {}


def _item_rule_metadata(item: dict[str, Any]) -> dict[str, Any]:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    return dict(metadata)


def _row_rule_metadata(row: dict[str, Any], fallback_scope: str, fallback_hotel_key: str | None) -> dict[str, Any]:
    outer = _row_outer_metadata(row)
    inner = outer.get("metadata") if isinstance(outer.get("metadata"), dict) else {}
    metadata = dict(inner)
    for key in ("hotel_key", "memory_type", "source", "confidence"):
        if key not in metadata and outer.get(key) is not None:
            metadata[key] = outer.get(key)
    metadata.setdefault("scope", inner.get("scope") or outer.get("scope") or fallback_scope)
    metadata.setdefault("hotel_key", fallback_hotel_key or (GLOBAL_HOTEL_KEY if fallback_scope == "global" else ""))
    return metadata


def _room_categories(metadata: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for key in ("room_category", "normalized_room_type"):
        value = str(metadata.get(key) or "").strip()
        if value:
            values.add(value)
    for value in metadata.get("affected_room_categories") or []:
        text = str(value or "").strip()
        if text:
            values.add(text)
    return values


def _has_complete_rule_metadata(metadata: dict[str, Any]) -> bool:
    return all(key in metadata for key in _RULE_REQUIRED_METADATA)


def _room_type_applies(metadata: dict[str, Any], room_type: str | None) -> bool:
    if not room_type:
        return True
    categories = _room_categories(metadata)
    return not categories or str(room_type).strip() in categories


def _relevance_score(value: Any) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _rule_sort_key(metadata: dict[str, Any], room_type: str | None) -> tuple[int, int, int, str]:
    scope = str(metadata.get("scope") or "").strip()
    if room_type and str(metadata.get("room_category") or metadata.get("normalized_room_type") or "").strip() == str(room_type).strip():
        scope = "room_type"
    return (
        _RULE_SCOPES.get(scope, 0),
        _priority(metadata.get("priority")),
        _CONFIDENCE_RANK.get(_confidence_label(metadata.get("confidence")), 0),
        _date_key(metadata.get("valid_from")),
    )


def _valid_for_date(metadata: dict[str, Any], stay_date: str | None) -> bool:
    if not stay_date:
        return True
    current = _date_key(stay_date)
    valid_from = _date_key(metadata.get("valid_from"))
    valid_to = _date_key(metadata.get("valid_to"))
    return (not valid_from or valid_from <= current) and (not valid_to or current <= valid_to)


def _is_rule_memory(memory_type: str, metadata: dict[str, Any]) -> bool:
    return str(memory_type).strip() in _RULE_MEMORY_TYPES or bool(metadata.get("rule_type"))


def _confidence_label(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in _CONFIDENCE_IMPORTANCE:
        return text
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
        return 0


def _date_key(value: Any) -> str:
    if value in (None, ""):
        return ""
    text = str(value)
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return text


def _normalize_text(value: str) -> str:
    return re.sub(r"\W+", "", value, flags=re.UNICODE).lower()


def _mnemosyne_instance(scope: str, hotel_key: str | None) -> Any:
    root = Path(_setting("MNEMOSYNE_DATA_ROOT", str(Path.home() / ".hermes/mnemosyne")))
    global_dir = Path(_setting("MNEMOSYNE_GLOBAL_DIR", str(root / "global")))
    hotels_dir = Path(_setting("MNEMOSYNE_HOTELS_DIR", str(root / "hotels")))
    db_dir = global_dir if scope == "global" else hotels_dir / str(hotel_key)
    os.environ.setdefault("MNEMOSYNE_DATA_DIR", str(global_dir))
    from mnemosyne import Mnemosyne

    return Mnemosyne(session_id=hotel_key or "global", db_path=db_dir / "mnemosyne.db")


def _append_staging(record: dict[str, Any]) -> bool:
    path = Path(_setting("MNEMOSYNE_STAGING_FILE", str(DEFAULT_STAGING_FILE)))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return True
    except OSError:
        return False


def _append_conflict(record: dict[str, Any]) -> bool:
    path = Path(_setting("MNEMOSYNE_CONFLICT_FILE", str(DEFAULT_CONFLICT_FILE)))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return True
    except OSError:
        return False


def _direct_enabled() -> bool:
    return _enabled() and _setting("MNEMOSYNE_WRITE_MODE", "staging").strip().lower() == "direct"


def _enabled() -> bool:
    return _setting("MNEMOSYNE_ENABLED", "false").strip().lower() == "true"


def _setting(name: str, default: str) -> str:
    if name in os.environ:
        return os.environ[name]
    path = Path(os.getenv("MNEMOSYNE_ENV_FILE", str(DEFAULT_ENV_FILE)))
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            if key.strip() == name:
                return value.strip().strip('"').strip("'")
    except OSError:
        pass
    return default
