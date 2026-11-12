from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any


def build_drafts(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Create conservative drafts only from a completed weekly analysis."""
    snapshot = report.get("fact_snapshot") or {}
    analysis = report.get("analysis") or {}
    allowed = {str(ref) for ref in snapshot.get("fact_references") or []}
    findings = list(analysis.get("key_findings") or [])
    drafts: list[dict[str, Any]] = []
    for action in list(analysis.get("actions") or [])[:3]:
        action_text = str(action.get("action") or "").strip()
        reason = str(action.get("reason") or "").strip()
        refs = [str(ref) for ref in action.get("fact_refs") or [] if str(ref) in allowed]
        for finding in findings:
            refs.extend(str(ref) for ref in finding.get("fact_refs") or [] if str(ref) in allowed)
            if len(set(refs)) >= 2:
                break
        refs = list(dict.fromkeys(refs))
        if not action_text:
            continue
        conditions = reason or "需由管理员结合已引用事实确认适用条件。"
        draft = {
            "rule_type": "weekly_operating_learning",
            "experience_summary": action_text,
            "applicable_conditions": conditions,
            "applicability": "仅适用于当前酒店；不得替代实时价格决策。",
            "evidence_refs": refs,
            "confidence": "medium",
        }
        drafts.append(draft)
    return drafts


def validate_draft(draft: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    allowed = {str(ref) for ref in snapshot.get("fact_references") or []}
    refs = list(dict.fromkeys(str(ref) for ref in draft.get("evidence_refs") or [] if str(ref)))
    missing = [ref for ref in refs if ref not in allowed]
    fields = ("experience_summary", "applicable_conditions", "applicability")
    invalid_fields = [field for field in fields if not str(draft.get(field) or "").strip()]
    generic_conditions = str(draft.get("applicable_conditions") or "").strip() == "需由管理员结合已引用事实确认适用条件。"
    valid = len(refs) >= 2 and not missing and not invalid_fields and not generic_conditions and bool(snapshot.get("fact_quality", {}).get("passed"))
    return {"valid": valid, "refs": refs, "missing_refs": missing, "invalid_fields": invalid_fields, "generic_conditions": generic_conditions}


def content_hash(draft: dict[str, Any], hotel_key: str) -> str:
    value = "\n".join((hotel_key, str(draft.get("rule_type") or ""), str(draft.get("experience_summary") or ""), str(draft.get("applicable_conditions") or ""), str(draft.get("applicability") or "")))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def memory_payload(candidate: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    metadata = {
        "tenant": "hotel_ops", "hotel_key": candidate["hotel_key"], "scope": "hotel", "scope_type": "single_hotel", "source": "approved_weekly_review_candidate",
        "weekly_review_id": candidate["weekly_review_id"], "candidate_id": str(candidate["id"]),
        "week_start": str(candidate["week_start"]), "week_end": str(candidate["week_end"]),
        "rule_type": candidate["rule_type"], "confidence": candidate["confidence"], "review_status": "approved", "approved_by": candidate.get("reviewed_by") or "",
        "evidence_refs": candidate["evidence_refs"], "conditions": candidate["applicable_conditions"],
        "applicability": candidate["applicability"], "approved_at": datetime.now(timezone.utc).isoformat(),
    }
    content = "\n".join((
        f"场景：{candidate['applicable_conditions']}",
        f"动作：{candidate['experience_summary']}",
        f"适用条件：{candidate['applicable_conditions']}",
        f"证据摘要：{json.dumps(candidate['evidence_refs'], ensure_ascii=False)}",
    ))
    return content, metadata
