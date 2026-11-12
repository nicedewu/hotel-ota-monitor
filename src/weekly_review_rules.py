from __future__ import annotations

from typing import Any


def build_rules_analysis(snapshot: dict[str, Any], error_code: str = "") -> dict[str, Any]:
    """Deterministic weekly-review analysis used whenever Hermes is unavailable."""
    metrics = snapshot.get("facts", {}).get("metrics") or {}
    refs = set(snapshot.get("fact_references") or [])
    metric_ref = _first(refs, "fact:metrics:")
    inventory_ref = _first(refs, "fact:inventory:") or metric_ref
    pricing_ref = _first(refs, "fact:pricing:") or metric_ref
    sales_rate = metrics.get("weekly_sales_rate", metrics.get("avg_occupancy_rate"))
    delta = metrics.get("week_over_week_pp")
    min_available = metrics.get("min_available_rooms", metrics.get("avg_available_rooms"))
    trend = "稳定"
    if isinstance(delta, (int, float)):
        trend = "改善" if delta >= 5 else "走弱" if delta <= -5 else "稳定"
    summary = f"本周经营整体{trend}。"
    if sales_rate is not None:
        summary += f"周综合真实销售率为 {sales_rate}%。"
    findings = [{"finding": summary, "fact_refs": _refs(metric_ref)}]
    risks = []
    if min_available is not None:
        risks.append({"risk": f"最低可售库存为 {min_available}，需持续按房型观察库存压力。", "fact_refs": _refs(inventory_ref)})
    actions = [{
        "action": "按日期和房型继续核对竞品价差、销售率与库存变化。",
        "reason": "规则降级仅依据已保存的本周经营事实，不引入额外推断。",
        "fact_refs": _refs(pricing_ref),
    }]
    return {
        "schema_version": "1.0",
        "run_id": snapshot["run_id"],
        "hotel_key": snapshot["hotel_key"],
        "period_start": snapshot["period_start"],
        "period_end": snapshot["period_end"],
        "summary": summary,
        "key_findings": findings,
        "risks": risks,
        "actions": actions,
        "quality": {"unsupported_claims": [], "warnings": [error_code] if error_code else []},
    }


def _first(values: set[str], prefix: str) -> str:
    return next((value for value in sorted(values) if value.startswith(prefix)), "")


def _refs(value: str) -> list[str]:
    return [value] if value else []
