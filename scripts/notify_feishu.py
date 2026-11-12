#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - python-dotenv is in requirements.txt
    load_dotenv = None

try:
    import fcntl
except ImportError:  # pragma: no cover - production runs on Linux
    fcntl = None


SENSITIVE_MARKERS = (
    "ctrip_manual_profile",
    "Set-Cookie:",
    "Cookie:",
    "password=",
    "passwd=",
    "短信验证码：",
    "验证码图片：",
    "DATABASE_URL",
    "/home/",
    "C:\\Users\\",
    "ssh -L",
)

USER_FACING_TERMS = {
    "bathtub_double": "浴缸大床房",
    "esports_single": "电竞单人房",
    "esports_double": "电竞双人房",
    "esports_twin": "电竞双床房",
    "esports_triple": "电竞三人房",
    "esports_quad": "电竞四人房",
    "esports_penta": "电竞五人房",
    "esports_hexa": "电竞六人房",
    "esports_other": "其他电竞房",
    "big_bed": "大床房",
    "twin": "双床房",
    "other": "其他房型",
    "captcha_required": "需要验证码",
    "session_expired": "登录状态已失效",
    "login_required": "需要登录",
    "decision_at": "决策时间",
    "stay_date": "入住日期",
    "run_id": "运行批次",
    "actual_sales_rate": "销售率",
    "occupancy_rate": "入住率",
    "pending_arrival_rooms": "未入住预订房",
    "pending_arrival": "未入住预订",
    "total_available": "总可售库存",
    "normalized_room_type": "标准房型",
    "normalized_room_name": "标准房型",
    "room_category": "房型",
    "room_type": "房型",
    "business_snapshot_report": "经营快照报告",
    "business_snapshot": "经营快照",
    "fallback_insufficient_data": "数据不足，使用规则分析",
    "fallback_llm_error": "分析服务异常，使用规则分析",
    "fallback_no_llm": "使用规则分析",
    "hermes": "周复盘分析",
    "rules_fallback": "规则分析降级",
    "own_hotel_key": "当前酒店",
    "competitor_hotel_key": "竞品酒店",
    "hotel_key": "酒店",
    "ota_room_prices": "OTA 房价数据",
    "own_daily_status": "酒店每日经营数据",
    "price_decisions": "调价建议",
    "default_keep": "默认维持",
    "low_occupancy_above_competitor": "低入住率且价格高于竞品",
    "missing_occupancy_keep": "入住率缺失时维持",
    "memory_candidates": "经验候选",
    "pricing_learning": "定价经验",
    "daily_snapshot": "每日经营快照",
    "missing_data": "缺失数据",
    "Mnemosyne": "经营经验库",
    "walk-in": "散客到店",
    "confidence": "置信度",
    "critical": "严重提醒",
    "warning": "提醒",
    "success": "成功",
    "failed": "失败",
    "pending": "待处理",
    "written": "已写入",
    "skipped": "已跳过",
    "sent": "已发送",
    "none": "无",
    "PMS": "酒店管理系统",
    "OTA": "在线旅游平台",
}

SNAKE_CASE_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+(?![A-Za-z0-9_])")
ROOM_CONFIGURATION_SUFFIX_RE = re.compile(
    r"(?<=[房间床])\s*(?:【[^】\n]*】|（(?=[^（）\n]*\+)[^（）\n]*）|\((?=[^()\n]*\+)[^()\n]*\)|\[(?=[^\[\]\n]*\+)[^\[\]\n]*\])"
)
BRANCH_PUSH_BACKEND = (Path.home() / ".hermes/scripts/branch_push_backend.py")


def main() -> None:
    parser = argparse.ArgumentParser(description="发送携程价格监控飞书提醒")
    parser.add_argument("--mode", choices=["price-monitor", "business-snapshot", "weekly-review", "event-alert", "login-reminder"], default="price-monitor")
    parser.add_argument("--status", choices=["success", "failed"], default="success")
    parser.add_argument("--detail-level", choices=["full"], default="full")
    parser.add_argument("--reason", default="manual_verification_required")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--project-dir", default="")
    parser.add_argument("--db-write-status", choices=["success", "failed", ""], default="")
    parser.add_argument("--report", default="", help="指定单酒店 business_snapshot_report.json")
    parser.add_argument("--file", default="", help="指定 weekly_review.md、admin_overview.md 或未来活动预警正文")
    parser.add_argument("--webhook-env-key", default="FEISHU_WEBHOOK")
    parser.add_argument("--branch-push-hotel-key", default="")
    parser.add_argument("--branch-push-type", default="")
    parser.add_argument("--branch-push-source-type", default="")
    parser.add_argument("--branch-push-source-id", default="")
    args = parser.parse_args()

    default_project_dir = Path(__file__).resolve().parents[1]
    if load_dotenv:
        load_dotenv(default_project_dir / ".env")
        load_dotenv((Path.home() / ".hotel_agent/webhook.env"))
    else:
        load_env_file((Path.home() / ".hotel_agent/webhook.env"))
    configured_project_dir = args.project_dir or os.getenv("PROJECT_DIR", "").strip()
    project_dir = Path(configured_project_dir or default_project_dir).resolve()
    data_dir = project_dir if project_dir.exists() else default_project_dir
    display_project_dir = configured_project_dir or str(project_dir)

    if args.mode == "login-reminder":
        message = build_login_reminder_message(args.reason)
    elif args.mode == "business-snapshot":
        message = build_business_snapshot_message(
            data_dir,
            args.status,
            args.detail_level,
            args.reason,
            Path(args.report) if args.report else None,
        )
    elif args.mode in {"weekly-review", "event-alert"}:
        if not args.file:
            raise SystemExit(f"{args.mode} 需要 --file")
        report_path = Path(args.file)
        messages = build_weekly_review_messages(report_path)
    else:
        latest = data_dir / "output/latest"
        run_summary, hermes_payload, price_compare, rooms_all = load_latest_price_json(latest)
        message = build_message(
            display_project_dir,
            args.reason,
            run_summary,
            hermes_payload,
            price_compare,
            rooms_all,
            args.db_write_status,
        )
    if args.mode not in {"weekly-review", "event-alert"}:
        messages = [message]
    messages = [sanitize_feishu_message(message) for message in messages]
    if args.mode not in {"weekly-review", "event-alert"}:
        message = messages[0]
    for message in messages:
        assert_no_sensitive_payload(message)

    if args.dry_run:
        if args.mode in {"weekly-review", "event-alert"}:
            print(f"飞书推送预览：共 {len(messages)} 条，不会真实发送。")
        else:
            print("飞书推送预览：将显示待发送正文，不会真实发送。")
            print(message)
        return

    branch_push_args = (
        args.branch_push_hotel_key,
        args.branch_push_type,
        args.branch_push_source_type,
        args.branch_push_source_id,
    )
    if any(branch_push_args) and not all(branch_push_args):
        raise SystemExit("branch 推送台账参数必须同时提供")

    sent_count = 0
    for message_index, message in enumerate(messages, start=1):
        reservation: dict[str, Any] | None = None
        if all(branch_push_args):
            source_id = _message_source_id(args.branch_push_source_id, message_index, len(messages))
            reservation = reserve_branch_push(
                hotel_key=args.branch_push_hotel_key,
                push_type=args.branch_push_type,
                webhook_env_key=args.webhook_env_key,
                source_record_type=args.branch_push_source_type,
                source_record_id=source_id,
                message=message,
            )
            if not reservation["should_send"]:
                delivery_status = str(reservation.get("delivery_status") or "")
                if delivery_status == "sent":
                    print("飞书推送已发送，复用既有台账记录。")
                    continue
                if delivery_status == "uncertain":
                    raise SystemExit("企业应用投递状态待人工核对，系统不会自动重发。")
                raise SystemExit("该推送正在投递中，请稍后再试。")
            message = reservation["message"]
            assert_no_sensitive_payload(message)
            if reservation.get("delivery_channel") == "feishu_app":
                result = _branch_push_call(
                    "deliver-feishu-app",
                    {
                        "push_id": reservation["push_id"],
                        "delivery_token": reservation["delivery_token"],
                        "message": message,
                        "feishu_chat_env_key": reservation.get("feishu_chat_env_key"),
                    },
                )
                if not result.get("ok"):
                    if result.get("delivery_status") == "uncertain":
                        raise SystemExit("企业应用投递状态待人工核对，系统不会自动重发。")
                    raise SystemExit("企业应用投递失败，已按现有策略保留重试。")
                sent_count += 1
                continue

        webhook = os.getenv(args.webhook_env_key, "").strip()
        if not webhook:
            raise SystemExit(f"缺少 {args.webhook_env_key}；如只想预览，请使用 --dry-run")
        payload = build_feishu_payload(message, os.getenv("FEISHU_SECRET", "").strip())
        try:
            send_feishu(webhook, payload)
        except BaseException as exc:
            if reservation:
                error_code = str(getattr(exc, "code", None) or type(exc).__name__)
                mark_branch_push(reservation, "mark-failed", error_code, str(exc)[:300])
            raise
        if reservation:
            result = mark_branch_push(reservation, "mark-sent")
            if not result.get("changed"):
                raise SystemExit("飞书已接收，但推送台账状态未更新；请先核对后再重试")
        sent_count += 1
    print(f"飞书提醒已发送：{sent_count} 条。")


def _message_source_id(source_id: str, message_index: int, message_count: int) -> str:
    if message_count <= 1:
        return source_id
    return f"{source_id}:part:{message_index}/{message_count}"


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip("'\"")
        os.environ.setdefault(key, value)


def _branch_push_call(command: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [sys.executable, str(BRANCH_PUSH_BACKEND), command],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        result = json.loads(completed.stdout.strip())
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise SystemExit("推送台账处理失败") from exc
    if completed.returncode != 0 or not isinstance(result, dict) or not result.get("ok"):
        raise SystemExit("推送台账处理失败")
    return result


def reserve_branch_push(
    *,
    hotel_key: str,
    push_type: str,
    webhook_env_key: str,
    source_record_type: str,
    source_record_id: str,
    message: str,
) -> dict[str, Any]:
    result = _branch_push_call(
        "reserve",
        {
            "hotel_key": hotel_key,
            "push_type": push_type,
            "webhook_env_key": webhook_env_key,
            "source_record_type": source_record_type,
            "source_record_id": source_record_id,
            "message": message,
        },
    )
    if result.get("should_send") and (not result.get("delivery_token") or not result.get("message")):
        raise SystemExit("推送台账返回不完整")
    return result


def mark_branch_push(reservation: dict[str, Any], command: str, error_code: str = "", error_summary: str = "") -> dict[str, Any]:
    return _branch_push_call(
        command,
        {
            "push_id": reservation.get("push_id"),
            "delivery_token": reservation.get("delivery_token"),
            "error_code": error_code,
            "error_summary": error_summary,
        },
    )


def sanitize_feishu_message(text: str) -> str:
    text = ROOM_CONFIGURATION_SUFFIX_RE.sub("", text)
    for term, label in sorted(USER_FACING_TERMS.items(), key=lambda item: len(item[0]), reverse=True):
        text = re.sub(
            rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])",
            label,
            text,
            flags=re.I,
        )
    text = re.sub(r"(?<![A-Za-z0-9_])esports_[A-Za-z0-9_]+(?![A-Za-z0-9_])", "其他电竞房", text, flags=re.I)
    text = re.sub(r"(?<![A-Za-z0-9_])own_[A-Za-z0-9_]+(?![A-Za-z0-9_])", "当前酒店", text, flags=re.I)
    text = re.sub(r"(?<![A-Za-z0-9_])competitor_[A-Za-z0-9_]+(?![A-Za-z0-9_])", "竞品酒店", text, flags=re.I)
    return SNAKE_CASE_RE.sub("相关指标", text)


def build_weekly_review_messages(report_path: Path) -> list[str]:
    if not report_path.is_file():
        raise SystemExit(f"周复盘文件不存在: {report_path}")
    return _split_message(report_path.read_text(encoding="utf-8").strip(), 3500)


def _split_message(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    remainder = text
    while remainder:
        if len(remainder) <= limit:
            chunks.append(remainder.strip())
            break
        split_at = remainder.rfind("\n", 0, limit + 1)
        split_at = limit if split_at <= 0 else split_at
        chunks.append(remainder[:split_at].strip())
        remainder = remainder[split_at:].lstrip("\n")
    return chunks or [""]


def load_latest_price_json(latest: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Load the complete structured result for one completed price-monitor run."""
    run_summary = _read_json_object(latest / "run_summary.json")
    hermes_payload = _read_json_object(latest / "hermes_payload.json")
    price_compare = _read_json_object(latest / "price_compare.json")
    rooms_all = _read_jsonl(latest / "rooms_all.jsonl")
    return run_summary, hermes_payload, price_compare, rooms_all


def build_business_snapshot_message(
    data_dir: Path,
    status: str,
    detail_level: str,
    reason: str,
    report_path: Path | None = None,
) -> str:
    if status == "failed":
        if reason.startswith("登录态异常："):
            return "\n".join(
                [
                    "【携程登录态异常告警】",
                    f"告警时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
                    f"受影响门店：{reason.removeprefix('登录态异常：')}",
                    "已停止写入 OTA 价格、生成调价结论和推送门店经营快照。",
                    "处理：在授权运维机双击「携程重登」快捷方式，在打开的浏览器中完成携程人工登录。",
                    "登录确认成功后会话自动清理，等待下一次定时任务自动恢复。",
                ]
            )
        failed_stage = reason if reason and reason != "manual_verification_required" else "定时执行链"
        return "\n".join(
            [
                "【后端定时经营快照失败】",
                f"快照时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
                f"失败阶段：{failed_stage}",
                "简短错误原因：任务未完成，建议查看服务器任务日志。",
            ]
        )
    if detail_level != "full":
        raise SystemExit("business-snapshot 只支持 --detail-level full")
    report_path = report_path or data_dir / "output/latest/business_snapshot_report.json"
    report = _read_json_object(report_path)
    message = str(report.get("feishu_message") or "").strip()
    if not message:
        raise SystemExit("business_snapshot_report.json 缺少 feishu_message")
    return message


def build_login_reminder_message(reason: str) -> str:
    return "\n".join(
        [
            "【携程登录态维护提醒】",
            f"提醒时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            "当前抓价仍正常，但人工登录会话已进入常见失效窗口。",
            f"建议：{reason}",
            "处理：在授权运维机双击「携程重登」快捷方式，在打开的浏览器中完成携程人工登录。",
            "登录确认成功后，下一次定时任务会自动使用新会话。",
        ]
    )


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SystemExit("未找到最新价格结果，请先执行酒店价格抓取。")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"最新价格结果格式错误: {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"最新价格结果格式错误: {path.name}")
    return data


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise SystemExit("未找到最新价格结果，请先执行酒店价格抓取。")
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                if isinstance(item, dict):
                    rows.append(item)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"最新价格结果格式错误: {path.name}: {exc}") from exc
    return rows


def build_message(
    project_dir: str,
    reason: str,
    run_summary: dict[str, Any],
    hermes_payload: dict[str, Any],
    price_compare: dict[str, Any],
    rooms_all: list[dict[str, Any]],
    db_write_status: str = "",
) -> str:
    message = render_price_reply_from_json(run_summary, hermes_payload, price_compare, rooms_all)
    if db_write_status == "success":
        message = f"{message}\n\n本次价格已写入数据库。"
    elif db_write_status == "failed":
        message = f"{message}\n\n本次价格已生成，但数据库写入失败，请检查 db-write。"
    statuses = collect_problem_statuses(hermes_payload)
    if not statuses:
        return message

    status_text = ", ".join(sorted(set(item["status"] for item in statuses)))
    reason_text = reason_text_for(status_text, reason)
    hotel_lines = []
    for item in statuses:
        hotel_lines.append(f"- {item.get('hotel_name', '')}: {item.get('status', '')} {item.get('error_reason', '')}".strip())
    if not hotel_lines:
        hotel_lines.append("- 未发现具体酒店异常；请查看服务器日志。")

    return "\n".join(
        [
            message,
            "",
            "## 需要人工处理",
            f"状态：{status_text}",
            f"原因：{reason_text}",
            "",
            "异常酒店：",
            *hotel_lines,
            "",
            "处理提示：",
            "请按服务器上的人工验证流程处理携程登录或安全验证，完成后重新发起价格查询。",
            "",
            "安全说明：",
            "程序不会自动识别或绕过验证码；飞书只发送提醒和操作步骤，不发送登录态、Cookie、账号密码、短信验证码或验证码图片。",
        ]
    )


def render_price_reply_from_json(
    run_summary: dict[str, Any],
    hermes_payload: dict[str, Any],
    price_compare: dict[str, Any],
    rooms_all: list[dict[str, Any]],
) -> str:
    roles = _hotel_roles(hermes_payload)
    own_rooms = [row for row in rooms_all if _room_role(row, roles) == "own"]
    competitor_rooms = [row for row in rooms_all if _room_role(row, roles) == "competitor"]
    comparisons = [row for row in price_compare.get("comparisons", []) if isinstance(row, dict)]
    hotel_count = _int_value(run_summary.get("hotel_count")) or len({str(row.get("hotel_key") or row.get("key") or row.get("hotel") or "") for row in rooms_all})

    lines = [
        "【携程酒店价格监控】",
        "",
        "## 本次抓取摘要",
        f"- 抓取日期：{_date_text(run_summary.get('captured_at') or hermes_payload.get('captured_at'))}",
        f"- 酒店数量：{hotel_count}",
        f"- 自有酒店可售房型数量：{sum(_is_available(row) for row in own_rooms)}",
        f"- 竞品酒店可售房型数量：{sum(_is_available(row) for row in competitor_rooms)}",
        "",
        "## 自有酒店房型明细",
        *_room_lines(own_rooms, include_hotel=False),
        "",
        "## 竞品酒店房型明细",
        *_room_lines(competitor_rooms, include_hotel=True),
        "",
        "## 同类价格对比",
    ]
    if comparisons:
        lines.extend(_comparison_lines(comparisons))
    elif competitor_rooms:
        lines.append("已抓到竞品房型，但本次没有形成同类房型价格对比。")
    else:
        lines.append("未发现竞品房型数据。")
    return "\n".join(lines)


def _hotel_roles(hermes_payload: dict[str, Any]) -> dict[str, str]:
    roles: dict[str, str] = {}
    for role, group in (("own", "own_hotels"), ("competitor", "competitor_hotels")):
        for hotel in hermes_payload.get(group, []) or []:
            if not isinstance(hotel, dict):
                continue
            for value in (hotel.get("key"), hotel.get("hotel_key"), hotel.get("name"), hotel.get("hotel_name")):
                if value:
                    roles[str(value)] = role
    return roles


def _room_role(row: dict[str, Any], roles: dict[str, str]) -> str:
    explicit = str(row.get("hotel_role") or row.get("role") or "").lower()
    if explicit in {"own", "competitor"}:
        return explicit
    for value in (row.get("hotel_key"), row.get("key"), row.get("hotel"), row.get("hotel_name"), row.get("name")):
        if value and str(value) in roles:
            return roles[str(value)]
    return ""


def _room_lines(rows: list[dict[str, Any]], include_hotel: bool) -> list[str]:
    if not rows:
        return ["- 无"]
    lines: list[str] = []
    for row in rows:
        prefix = f"{_field(row, 'stay_date', 'checkin_date', 'date')}"
        if include_hotel:
            prefix += f"｜{_field(row, 'hotel_name', 'name', default='未识别酒店')}"
        lines.append(
            f"- {prefix}｜{_field(row, 'room_name', 'title', default='未识别房型')}"
            f"｜{_field(row, 'normalized_room_type', 'normalized_room_name', default='未分类')}"
            f"｜{_price_text(_value(row, 'price', 'amount', 'lowest_price'))}"
        )
    return lines


def _comparison_lines(comparisons: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for row in comparisons:
        pct = _value(row, "price_diff_pct")
        pct_text = "缺失" if pct in (None, "") else f"{float(pct) * 100:.2f}%"
        lines.append(
            f"- {_field(row, 'stay_date', 'checkin_date', 'date')}｜{_field(row, 'normalized_room_type', 'normalized_room_name', default='未分类')}"
            f"｜自有：{_field(row, 'own_room_name', default='未识别房型')} {_price_text(row.get('own_price'))}"
            f"｜竞品：{_field(row, 'competitor_hotel_name', default='未识别酒店')} {_field(row, 'competitor_room_name', default='未识别房型')} {_price_text(row.get('competitor_price'))}"
            f"｜差价：{_price_text(row.get('price_diff'))}｜差价比例：{pct_text}"
            f"｜建议：{_field(row, 'suggestion', default='无')}｜告警：{_field(row, 'alert_level', default='none')}"
        )
    return lines


def _value(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


def _field(row: dict[str, Any], *names: str, default: str = "缺失") -> str:
    value = _value(row, *names)
    return str(value) if value not in (None, "") else default


def _is_available(row: dict[str, Any]) -> bool:
    values = [_value(row, name) for name in ("available", "sellable", "can_book")]
    text = " ".join(str(value).strip().lower() for value in values if value not in (None, ""))
    return text in {"true", "1", "yes", "available", "bookable"} or any(token in text for token in ("可订", "在售", "仅剩", "预订", "有房"))


def _date_text(value: Any) -> str:
    return str(value or "未知").split("T", 1)[0]


def _int_value(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _price_text(value: Any) -> str:
    try:
        return f"¥{float(value):.2f}"
    except (TypeError, ValueError):
        return "价格未知"


def collect_problem_statuses(hermes_payload: dict[str, Any]) -> list[dict[str, str]]:
    statuses: list[dict[str, str]] = []
    for group in ("own_hotels", "competitor_hotels", "unknown_hotels"):
        for hotel in hermes_payload.get(group, []) or []:
            status = str(hotel.get("status", ""))
            if status in {"login_required", "session_expired", "captcha_required"}:
                statuses.append(
                    {
                        "hotel_key": str(hotel.get("key", "")),
                        "hotel_name": str(hotel.get("name", "")),
                        "status": status,
                        "error_reason": str(hotel.get("error_reason", "")),
                    }
                )
    for alert in hermes_payload.get("alerts", []) or []:
        if alert.get("level") == "critical" and "验证码" in str(alert.get("message", "")):
            hotel_key = str(alert.get("hotel_key", ""))
            if not any(item["hotel_key"] == hotel_key and item["status"] == "captcha_required" for item in statuses):
                statuses.append(
                    {
                        "hotel_key": hotel_key,
                        "hotel_name": str(alert.get("hotel_name", "")),
                        "status": "captcha_required",
                        "error_reason": str(alert.get("message", "")),
                    }
                )
    return statuses


def reason_text_for(status_text: str, reason: str) -> str:
    if "captcha_required" in status_text:
        return "携程出现验证码或安全验证，需要人工处理。"
    if "session_expired" in status_text or "login_required" in status_text:
        return "携程登录态失效或需要重新验证。"
    return reason


def build_feishu_payload(message: str, secret: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "msg_type": "text",
        "content": {"text": message},
    }
    if secret:
        timestamp = str(int(time.time()))
        string_to_sign = f"{timestamp}\n{secret}"
        digest = hmac.new(string_to_sign.encode("utf-8"), b"", hashlib.sha256).digest()
        payload["timestamp"] = timestamp
        payload["sign"] = base64.b64encode(digest).decode("utf-8")
    return payload


class FeishuSendError(RuntimeError):
    def __init__(self, code: str | None, message: str):
        self.code = code
        super().__init__(message)


def _feishu_app_lock_path() -> Path:
    return Path(os.getenv("FEISHU_APP_LOCK_PATH", "/tmp/feishu-app-1500.lock"))


@contextmanager
def _serialized_feishu_app() -> Iterator[None]:
    lock_path = _feishu_app_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _reserve_feishu_send_window() -> None:
    state_path = Path(f"{_feishu_app_lock_path()}.next")
    try:
        next_allowed = float(state_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        next_allowed = 0.0
    delay = max(0.0, next_allowed - time.time())
    if delay:
        time.sleep(delay)
    interval = max(0.0, float(os.getenv("FEISHU_APP_MIN_INTERVAL_SECONDS", "10")))
    try:
        state_path.write_text(f"{time.time() + interval:.6f}\n", encoding="utf-8")
    except OSError:
        pass


def _send_feishu_once(webhook: str, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(webhook, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise FeishuSendError(None, f"飞书发送失败: {exc}") from exc
    try:
        result = json.loads(body)
    except json.JSONDecodeError as exc:
        raise FeishuSendError(None, f"飞书响应格式错误: {body}") from exc
    code = result.get("code", result.get("StatusCode"))
    if code not in (0, "0"):
        raise FeishuSendError(str(code), f"飞书发送失败: code={code}, msg={result.get('msg', '')}")
    print(body)


def send_feishu(webhook: str, payload: dict[str, Any]) -> None:
    retry_delays = (30, 60, 120)
    with _serialized_feishu_app():
        for attempt in range(len(retry_delays) + 1):
            _reserve_feishu_send_window()
            try:
                _send_feishu_once(webhook, payload)
                return
            except FeishuSendError as exc:
                if exc.code != "11232" or attempt == len(retry_delays):
                    raise
                time.sleep(retry_delays[attempt])


def assert_no_sensitive_payload(message: str) -> None:
    lowered = message.lower()
    blocked = [word for word in SENSITIVE_MARKERS if word.lower() in lowered]
    if blocked:
        raise SystemExit(f"飞书消息包含敏感字段，已停止发送: {', '.join(blocked)}")


if __name__ == "__main__":
    main()
