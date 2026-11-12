from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from src.hermes_json import normalize_hermes_json_output


class WeeklyAnalysisError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def analyze_weekly_snapshot(snapshot: dict[str, Any], timeout_seconds: int = 90) -> tuple[dict[str, Any], dict[str, Any]]:
    executable = Path(os.getenv("HERMES_CLI", str(Path.home() / "bin/hermes-hotel-analysis")))
    if not executable.is_file():
        raise WeeklyAnalysisError("HERMES_IMPORT_ERROR", "Hermes CLI 不可用")
    analysis_input = _compact_analysis_input(snapshot)
    prompt = (
        "你是无会话的酒店经营周复盘分析器。只根据下面 JSON 中的 facts 和 fact_references 输出一个 JSON 对象。"
        "不得调用工具、不得查询外部数据、不得写数据库、不得生成候选经验或记忆字段、不得跨店。"
        "所有 finding、risk、action 都必须携带输入中的 fact_refs；每条 action 必须包含非空 reason，"
        "说明该动作的适用条件或事实原因；actions 最多三条；不要在自由文本中编造数字。"
        "输出字段必须是 schema_version,run_id,hotel_key,period_start,period_end,summary,key_findings,risks,actions,quality。\n"
        + json.dumps(analysis_input, ensure_ascii=False, default=str, separators=(",", ":"))
    )
    started = time.monotonic()
    try:
        result = subprocess.run(
            [str(executable), "--ignore-rules", "--oneshot", prompt],
            cwd="/tmp", text=True, capture_output=True, timeout=timeout_seconds, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise WeeklyAnalysisError("HERMES_TIMEOUT", f"Hermes 调用超时 {timeout_seconds}s") from exc
    duration_ms = int((time.monotonic() - started) * 1000)
    diagnostics = {
        "analysis_duration_ms": duration_ms,
        "analysis_model": _version(executable),
        "analysis_schema_version": "1.0",
        "analysis_input_bytes": len(prompt.encode("utf-8")),
        "stderr_summary": (result.stderr or "")[:400],
    }
    if result.returncode:
        raise WeeklyAnalysisError("HERMES_PROCESS_ERROR", f"Hermes 退出码={result.returncode}")
    try:
        normalized_output, output_wrapper = normalize_hermes_json_output(result.stdout)
        diagnostics["analysis_output_wrapper"] = output_wrapper
        output = json.loads(normalized_output)
    except json.JSONDecodeError as exc:
        raise WeeklyAnalysisError("HERMES_INVALID_JSON", "Hermes 未返回合法 JSON") from exc
    validate_weekly_analysis(output, snapshot)
    return output, diagnostics


def _compact_analysis_input(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Keep one-shot input below Linux' per-argument limit without losing identity or refs."""
    return _compact_value(snapshot)


def _compact_value(value: Any, *, depth: int = 0) -> Any:
    if isinstance(value, str):
        return value if len(value) <= 240 else value[:240] + "…"
    if isinstance(value, dict):
        raw_field_names = {"raw", "raw_payload", "raw_context", "input_snapshot"}
        items = [(key, item) for key, item in value.items() if str(key).lower() not in raw_field_names]
        limit = 24 if depth <= 1 else 12
        compacted = {str(key): _compact_value(item, depth=depth + 1) for key, item in items[:limit]}
        omitted = len(value) - len(items)
        if len(items) > limit or omitted:
            compacted["_truncated_field_count"] = len(items) - limit + omitted
        return compacted
    if isinstance(value, (list, tuple)):
        limit = 8 if depth >= 2 else 16
        compacted = [_compact_value(item, depth=depth + 1) for item in value[:limit]]
        if len(value) > limit:
            compacted.append({"_truncated_item_count": len(value) - limit})
        return compacted
    return value


def validate_weekly_analysis(output: Any, snapshot: dict[str, Any]) -> None:
    if not isinstance(output, dict):
        raise WeeklyAnalysisError("HERMES_SCHEMA_INVALID", "分析结果不是对象")
    forbidden = {"candidate_drafts", "memory_candidates", "experience_candidates", "mnemosyne_writeback"}
    if forbidden & set(output):
        raise WeeklyAnalysisError("HERMES_SCHEMA_INVALID", "分析结果包含禁止字段")
    for key in ("run_id", "hotel_key", "period_start", "period_end"):
        if output.get(key) != snapshot.get(key):
            raise WeeklyAnalysisError("HERMES_IDENTITY_MISMATCH", f"{key} 不一致")
    if output.get("schema_version") != "1.0" or not str(output.get("summary") or "").strip():
        raise WeeklyAnalysisError("HERMES_SCHEMA_INVALID", "缺少支持的 schema 或 summary")
    actions = output.get("actions")
    if not isinstance(actions, list) or len(actions) > 3:
        raise WeeklyAnalysisError("HERMES_SCHEMA_INVALID", "actions 必须是至多三条的数组")
    allowed_refs = set(snapshot.get("fact_references") or [])
    for section, field in (("key_findings", "finding"), ("risks", "risk"), ("actions", "action")):
        items = output.get(section)
        if not isinstance(items, list):
            raise WeeklyAnalysisError("HERMES_SCHEMA_INVALID", f"{section} 必须是数组")
        for item in items:
            if not isinstance(item, dict) or not str(item.get(field) or "").strip():
                raise WeeklyAnalysisError("HERMES_SCHEMA_INVALID", f"{section} 格式错误")
            if section == "actions" and not str(item.get("reason") or "").strip():
                raise WeeklyAnalysisError("HERMES_SCHEMA_INVALID", "actions 缺少适用条件或事实原因")
            refs = item.get("fact_refs")
            if not isinstance(refs, list) or not refs or not set(refs) <= allowed_refs:
                raise WeeklyAnalysisError("HERMES_FACT_REFERENCE_INVALID", f"{section} 的事实引用无效")


def _version(executable: Path) -> str | None:
    try:
        result = subprocess.run([str(executable), "--version"], cwd="/tmp", text=True, capture_output=True, timeout=5, check=False)
        return result.stdout.strip()[:120] or None
    except (OSError, subprocess.TimeoutExpired):
        return None
