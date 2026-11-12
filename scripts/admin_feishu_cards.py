#!/usr/bin/env python3
"""Send reviewed weekly-candidate cards to the owning branch group."""
from __future__ import annotations

import argparse
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from lark_oapi import Client
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
from lark_oapi.core.const import FEISHU_DOMAIN, LARK_DOMAIN

ENV_FILE = (Path.home() / ".hermes/.env")
BRANCH_ENV_FILE = (Path.home() / ".hermes/profiles/branch/.env")
BRANCH_CHAT_ENV_RE = re.compile(r"^FEISHU_BRANCH_CHAT_ID_[1-5]$")
FACT_REFERENCE_LABELS_FILE = Path(__file__).resolve().parents[1] / "config" / "fact_reference_labels.json"


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_env(path: Path = ENV_FILE) -> None:
    for key, value in _read_env_file(path).items():
        os.environ.setdefault(key, value)


def text(value: Any, limit: int = 4000) -> str:
    return str(value or "").strip()[:limit]


@lru_cache
def display_labels() -> dict[str, dict[str, str]]:
    try:
        payload = json.loads(FACT_REFERENCE_LABELS_FILE.read_text(encoding="utf-8"))
        return {
            section: {str(key): text(value, 120) for key, value in values.items()}
            for section, values in payload.items()
            if section.endswith("_labels") and isinstance(values, dict)
        }
    except (OSError, json.JSONDecodeError):
        return {}


def display_text(value: Any, limit: int = 4000) -> str:
    rendered = text(value, limit)
    replacements = {
        **display_labels().get("hotel_key_labels", {}),
        **display_labels().get("room_type_labels", {}),
    }
    for source, label in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
        rendered = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(source)}(?![A-Za-z0-9_])", label, rendered)
    return rendered


def evidence_label(reference: Any) -> str:
    """Render stored fact identifiers without exposing internal key syntax."""
    raw = text(reference, 256)
    parts = raw.split(":")
    if len(parts) >= 3 and parts[0] == "fact":
        label = display_labels().get("fact_type_labels", {}).get(parts[1], "经营事实")
        day = parts[2]
        return f"{label}（{day}）" if len(day) == 10 and day[4:5] == "-" and day[7:8] == "-" else label
    return raw


def card(candidate: dict[str, Any]) -> dict[str, Any]:
    candidate_id = text(candidate["id"], 64)
    base = {"weekly_review_candidate_action": "", "candidate_id": candidate_id}
    def callback(action: str) -> dict[str, Any]:
        return {"type": "callback", "value": {**base, "weekly_review_candidate_action": action}}
    def button(label: str, action: str, kind: str = "default") -> dict[str, Any]:
        return {"tag": "button", "text": {"tag": "plain_text", "content": label}, "type": kind,
                "behaviors": [callback(action)]}
    refs = "\n".join(f"- {evidence_label(item)}" for item in (candidate.get("evidence_refs") or [])) or "- 暂无"
    can_direct_approve = (
        text(candidate.get("status")) == "pending_review"
        and text(candidate.get("evidence_status")) == "valid"
        and text(candidate.get("conflict_status")) == "clear"
    )
    elements = [
        {"tag": "markdown", "content": "\n".join([
            f"**门店**：{display_text(candidate.get('hotel_key'), 80)}  |  **周期**：{text(candidate.get('week_start'), 32)} 至 {text(candidate.get('week_end'), 32)}",
            f"**候选动作**：{display_text(candidate.get('experience_summary'), 700)}",
            f"**当前适用条件**：{display_text(candidate.get('applicable_conditions'), 700)}",
            "**已引用事实**：", refs,
            "认可当前条件可直接批准；意见不同时可修改条件后批准。",
            "审核时限：卡片成功推送后 24 小时内处理，逾期未处理将默认不通过。",
            "退回时必须填写理由。",
        ])},
    ]
    field_label = "修改适用条件（可选）" if can_direct_approve else "补充适用条件（必填）"
    submit_label = "修改并批准" if can_direct_approve else "补充并批准"
    elements.append({"tag": "form", "name": "weekly_review_candidate_enrich", "elements": [
            {"tag": "input", "name": "comment",
             "label": {"tag": "plain_text", "content": "退回理由（拒绝时必填）"},
             "label_position": "top",
             "placeholder": {"tag": "plain_text", "content": "批准可留空；退回请说明具体原因"},
             "input_type": "multiline_text", "rows": 2, "auto_resize": True,
             "width": "fill", "max_length": 500},
            {"tag": "column_set", "flex_mode": "stretch", "horizontal_spacing": "8px", "columns": [
                {"tag": "column", "width": "weighted", "weight": 4, "elements": [
                    {"tag": "input", "name": "conditions",
                     "label": {"tag": "plain_text", "content": field_label},
                     "label_position": "top",
                     "placeholder": {"tag": "plain_text", "content": "例如：仅在周末入住率低于 60% 且提前 3 天仍有空房时适用"},
                     "input_type": "multiline_text", "rows": 2, "auto_resize": True,
                     "width": "fill", "max_length": 700},
                ]},
                {"tag": "column", "width": "weighted", "weight": 1, "vertical_align": "bottom", "elements": [
                    {"tag": "button", "name": "enrich_and_approve", "form_action_type": "submit",
                     "text": {"tag": "plain_text", "content": submit_label}, "type": "primary",
                     "size": "medium", "width": "fill", "behaviors": [callback("enrich_approve")]},
                ]},
                {"tag": "column", "width": "weighted", "weight": 1, "vertical_align": "bottom", "elements": [
                    {"tag": "button", "name": "reject", "form_action_type": "submit",
                     "text": {"tag": "plain_text", "content": "拒绝"}, "type": "danger",
                     "size": "medium", "width": "fill", "behaviors": [callback("reject")]},
                ]},
            ]},
        ]})
    if can_direct_approve:
        elements.append({"tag": "column_set", "flex_mode": "none", "columns": [
            {"tag": "column", "width": "auto", "elements": [button("批准", "approve", "primary")]}
        ]})
    return {
        "schema": "2.0",
        "config": {"width_mode": "fill"},
        "header": {"title": {"tag": "plain_text", "content": "周复盘经验候选审核"}, "template": "blue"},
        "body": {"elements": elements},
    }


def send(candidate: dict[str, Any]) -> str:
    branch_chat_env_key = text(candidate.get("feishu_chat_env_key"), 128)
    if not branch_chat_env_key:
        raise RuntimeError("缺少分店群路由")
    if not BRANCH_CHAT_ENV_RE.fullmatch(branch_chat_env_key):
        raise RuntimeError("分店群路由配置无效")
    branch_env = _read_env_file(BRANCH_ENV_FILE)
    chat_id = text(branch_env.get(branch_chat_env_key) or os.getenv(branch_chat_env_key), 256)
    app_id = text(
        branch_env.get("FEISHU_BRANCH_APP_ID")
        or branch_env.get("FEISHU_APP_ID")
        or os.getenv("FEISHU_BRANCH_APP_ID"),
        256,
    )
    app_secret = text(
        branch_env.get("FEISHU_BRANCH_APP_SECRET")
        or branch_env.get("FEISHU_APP_SECRET")
        or os.getenv("FEISHU_BRANCH_APP_SECRET"),
        512,
    )
    domain_value = branch_env.get("FEISHU_DOMAIN") or os.getenv("FEISHU_DOMAIN")
    if not chat_id or not app_id or not app_secret:
        raise RuntimeError("分店群或企业应用配置不完整")
    domain = LARK_DOMAIN if text(domain_value).lower() == "lark" else FEISHU_DOMAIN
    client = Client.builder().app_id(app_id).app_secret(app_secret).domain(domain).build()
    body = CreateMessageRequestBody.builder().receive_id(chat_id).msg_type("interactive").content(json.dumps(card(candidate), ensure_ascii=False)).build()
    response = client.im.v1.message.create(CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build())
    if not response.success():
        raise RuntimeError(f"飞书卡片投递失败：{getattr(response, 'code', '')}")
    message_id = text(getattr(getattr(response, "data", None), "message_id", ""), 256)
    if not message_id:
        raise RuntimeError("飞书未返回消息 ID")
    return message_id


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-json", required=True)
    args = parser.parse_args()
    print(json.dumps({"message_id": send(json.loads(Path(args.candidate_json).read_text(encoding="utf-8")))}, ensure_ascii=False))


if __name__ == "__main__":
    main()
