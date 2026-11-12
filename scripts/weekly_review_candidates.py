#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.memory_provider import remember_hotel
from src.weekly_review_candidate_governance import build_drafts, content_hash, memory_payload, validate_draft
from src.weekly_review_repository import connect

HERMES_PYTHON = os.getenv("HERMES_PYTHON", str(Path.home() / ".hermes/hermes-agent/venv/bin/python"))
CARD_SENDER = str(PROJECT_ROOT / "scripts" / "admin_feishu_cards.py")
EXPIRY_ACTOR = "system:review-expiry"
EXPIRY_COMMENT = "审核超过24小时，未处理，默认不通过"
EXPIRY_ERROR = "候选审核已超过24小时，系统已默认不通过"


def generate(week_start: str, hotel_key: str, write: bool) -> dict:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT id,run_id,hotel_key,week_start,week_end,report_json,fact_snapshot_json
                FROM weekly_reviews WHERE week_start=%s AND hotel_key=%s
                ORDER BY generated_at DESC,id DESC LIMIT 1""", (week_start, hotel_key))
            row = cur.fetchone()
            if not row:
                raise ValueError("未找到周报")
            report = dict(row["report_json"] or {})
            report["fact_snapshot"] = dict(row["fact_snapshot_json"] or report.get("fact_snapshot") or {})
            if report.get("analysis_status") != "completed" or report.get("fact_quality_passed") is not True:
                raise ValueError("周报未通过分析或事实质量校验")
            if report.get("analysis_mode") != "hermes" or report.get("analysis_degraded") is True:
                return {"weekly_review_id": row["id"], "drafts": [], "write": write, "skip_reason": "analysis_degraded"}
            created = []
            for draft in build_drafts(report):
                check = validate_draft(draft, report["fact_snapshot"])
                digest = content_hash(draft, hotel_key)
                cur.execute("SELECT id,status FROM weekly_review_candidate_drafts WHERE hotel_key=%s AND content_hash=%s AND status IN ('approved','sync_pending','synced')", (hotel_key, digest))
                conflicts = [str(conflict["id"]) for conflict in cur.fetchall()]
                conflict_status = "conflict" if conflicts else "clear"
                status = "pending_review" if check["valid"] and not conflicts else "needs_more_evidence"
                item = {**draft, "content_hash": digest, "status": status, "evidence_status": "valid" if check["valid"] else "invalid", "evidence_details": check, "conflict_status": conflict_status, "conflict_details": {"matching_candidate_ids": conflicts}}
                created.append(item)
                if write:
                    cur.execute("""INSERT INTO weekly_review_candidate_drafts
                        (id,weekly_review_id,run_id,hotel_key,week_start,week_end,rule_type,experience_summary,applicable_conditions,applicability,evidence_refs,confidence,content_hash,evidence_status,evidence_details,conflict_status,conflict_details,status)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (weekly_review_id,content_hash) DO NOTHING""",
                        (str(uuid4()),row["id"],row["run_id"],hotel_key,row["week_start"],row["week_end"],draft["rule_type"],draft["experience_summary"],draft["applicable_conditions"],draft["applicability"],json.dumps(check["refs"]),draft["confidence"],digest,item["evidence_status"],json.dumps(check),conflict_status,json.dumps(item["conflict_details"]),status))
            if write: conn.commit()
    return {"weekly_review_id": row["id"], "drafts": created, "write": write}


def generate_many(week_start: str, hotel_key: str, write: bool) -> dict:
    if hotel_key != "all":
        return {"results": [generate(week_start, hotel_key, write)]}
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT hotel_key FROM weekly_reviews WHERE week_start=%s ORDER BY hotel_key", (week_start,))
            keys = [str(row["hotel_key"]) for row in cur.fetchall()]
    results = []
    for key in keys:
        try:
            results.append({"hotel_key": key, **generate(week_start, key, write)})
        except ValueError as exc:
            results.append({"hotel_key": key, "error": str(exc)})
    return {"results": results}


def last_natural_week_start() -> str:
    today = date.today()
    return (today - timedelta(days=today.weekday() + 7)).isoformat()


def _expire_locked_candidate(cur: Any, candidate_id: str) -> bool:
    cur.execute(
        """
        UPDATE weekly_review_candidate_drafts
        SET status='rejected', reviewed_by=%s, reviewed_at=now(), review_comment=%s,
            mnemosyne_sync_status='not_ready', updated_at=now()
        WHERE id=%s
          AND status IN ('pending_validation','needs_more_evidence','pending_review')
          AND admin_notification_status='sent'
          AND admin_notified_at IS NOT NULL
          AND admin_notified_at <= now() - INTERVAL '24 hours'
        RETURNING id
        """,
        (EXPIRY_ACTOR, EXPIRY_COMMENT, candidate_id),
    )
    if not cur.fetchone():
        return False
    cur.execute(
        "INSERT INTO weekly_review_candidate_reviews (id,candidate_id,action,comment,actor_id) VALUES (%s,%s,'reject',%s,%s)",
        (str(uuid4()), candidate_id, EXPIRY_COMMENT, EXPIRY_ACTOR),
    )
    return True


def expire_overdue(limit: int = 500, dry_run: bool = False) -> dict:
    if limit < 1:
        raise ValueError("limit 必须大于 0")
    result = {"dry_run": dry_run, "eligible": 0, "expired": 0, "limit": limit}
    with connect() as conn:
        with conn.cursor() as cur:
            query = """
                SELECT id
                FROM weekly_review_candidate_drafts
                WHERE status IN ('pending_validation','needs_more_evidence','pending_review')
                  AND admin_notification_status='sent'
                  AND admin_notified_at IS NOT NULL
                  AND admin_notified_at <= now() - INTERVAL '24 hours'
                ORDER BY admin_notified_at ASC
                LIMIT %s
            """
            if not dry_run:
                query += " FOR UPDATE SKIP LOCKED"
            cur.execute(query, (limit,))
            rows = cur.fetchall()
            result["eligible"] = len(rows)
            if not dry_run:
                for row in rows:
                    if _expire_locked_candidate(cur, str(row["id"])):
                        result["expired"] += 1
        conn.commit()
    return result


def review(candidate_id: str, action: str, actor: str, comment: str) -> None:
    if action not in {"approve", "reject", "request_more_evidence"}: raise ValueError("无效审核动作")
    comment = (comment or "").strip()
    if action == "reject" and not comment:
        raise ValueError("退回时必须填写理由")
    status = {"approve":"approved", "reject":"rejected", "request_more_evidence":"needs_more_evidence"}[action]
    allowed_statuses = {
        "approve": {"pending_review"},
        "reject": {"pending_validation", "needs_more_evidence", "pending_review"},
        "request_more_evidence": {"pending_validation", "needs_more_evidence", "pending_review"},
    }[action]
    expired = False
    with connect() as conn:
        with conn.cursor() as cur:
            expired = _expire_locked_candidate(cur, candidate_id)
            if not expired:
                cur.execute("SELECT status,evidence_status,conflict_status FROM weekly_review_candidate_drafts WHERE id=%s FOR UPDATE", (candidate_id,))
                candidate = cur.fetchone()
                if not candidate: raise ValueError("候选不存在")
                current_status = str(candidate["status"])
                if current_status not in allowed_statuses:
                    raise ValueError("候选已处理，不能重复审核")
                if action == "approve" and (candidate["status"] != "pending_review" or candidate["evidence_status"] != "valid" or candidate["conflict_status"] != "clear"):
                    raise ValueError("候选未通过事实或冲突校验，不能批准")
                cur.execute(
                    """UPDATE weekly_review_candidate_drafts
                       SET status=%s,reviewed_by=%s,reviewed_at=now(),review_comment=%s,
                           mnemosyne_sync_status=%s,updated_at=now()
                     WHERE id=%s AND status=%s
                     RETURNING id""",
                    (status, actor, comment, "pending" if action == "approve" else "not_ready", candidate_id, current_status),
                )
                if not cur.fetchone():
                    raise ValueError("候选已被其他人处理，请刷新后重试")
                cur.execute("INSERT INTO weekly_review_candidate_reviews (id,candidate_id,action,comment,actor_id) VALUES (%s,%s,%s,%s,%s)", (str(uuid4()),candidate_id,action,comment,actor))
        conn.commit()
    if expired:
        raise ValueError(EXPIRY_ERROR)


def enrich(candidate_id: str, actor: str, comment: str, conditions: str, evidence_refs: list[str]) -> None:
    expired = False
    with connect() as conn:
        with conn.cursor() as cur:
            expired = _expire_locked_candidate(cur, candidate_id)
            if not expired:
                cur.execute("""SELECT d.*,w.fact_snapshot_json FROM weekly_review_candidate_drafts d
                    JOIN weekly_reviews w ON w.id=d.weekly_review_id WHERE d.id=%s FOR UPDATE""", (candidate_id,))
                row = cur.fetchone()
                if not row or row["status"] not in {"needs_more_evidence", "pending_validation", "pending_review"}: raise ValueError("候选当前不允许修改适用条件")
                effective_refs = evidence_refs or list(row["evidence_refs"] or [])
                draft = {"experience_summary":row["experience_summary"],"applicable_conditions":conditions,"applicability":row["applicability"],"evidence_refs":effective_refs}
                check = validate_draft(draft, dict(row["fact_snapshot_json"] or {}))
                digest = content_hash(draft, row["hotel_key"])
                cur.execute("SELECT id FROM weekly_review_candidate_drafts WHERE hotel_key=%s AND content_hash=%s AND id<>%s AND status IN ('approved','sync_pending','synced')", (row["hotel_key"],digest,candidate_id))
                conflicts = [str(item["id"]) for item in cur.fetchall()]
                status = "pending_review" if check["valid"] and not conflicts else "needs_more_evidence"
                cur.execute("""UPDATE weekly_review_candidate_drafts SET applicable_conditions=%s,evidence_refs=%s,content_hash=%s,
                    evidence_status=%s,evidence_details=%s,conflict_status=%s,conflict_details=%s,status=%s,updated_at=now() WHERE id=%s""",
                    (conditions,json.dumps(check["refs"]),digest,"valid" if check["valid"] else "invalid",json.dumps(check),"conflict" if conflicts else "clear",json.dumps({"matching_candidate_ids":conflicts}),status,candidate_id))
                cur.execute("INSERT INTO weekly_review_candidate_reviews (id,candidate_id,action,comment,actor_id) VALUES (%s,%s,%s,%s,%s)", (str(uuid4()),candidate_id,"request_more_evidence",comment,actor))
        conn.commit()
    if expired:
        raise ValueError(EXPIRY_ERROR)


def sync(candidate_id: str) -> str:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM weekly_review_candidate_drafts WHERE id=%s FOR UPDATE", (candidate_id,))
            row = cur.fetchone()
            if not row or row["status"] != "approved" or row["mnemosyne_sync_status"] != "pending": raise ValueError("候选尚未获批或已处理")
            content, metadata = memory_payload(dict(row))
            outcome = remember_hotel(row["hotel_key"], content, "business_rule", source="approved_weekly_review_candidate", confidence=row["confidence"], metadata=metadata)
            if outcome != "direct":
                cur.execute("UPDATE weekly_review_candidate_drafts SET status='sync_failed',mnemosyne_sync_status='failed',mnemosyne_memory_id=%s,updated_at=now() WHERE id=%s", (outcome,candidate_id)); conn.commit(); return outcome
            cur.execute("UPDATE weekly_review_candidate_drafts SET status='synced',mnemosyne_sync_status='synced',mnemosyne_memory_id=%s,updated_at=now() WHERE id=%s", (outcome,candidate_id)); conn.commit(); return outcome


def card_action(candidate_id: str, action: str, actor: str, comment: str, conditions: str = "", evidence_refs: list[str] | None = None, expected_hotel_key: str | None = None) -> dict:
    """Apply one authenticated management-card operation deterministically."""
    if action not in {"approve", "reject", "enrich", "enrich_approve"}:
        raise ValueError("无效卡片审核动作")
    if action == "reject" and not (comment or "").strip():
        raise ValueError("退回时必须填写理由")
    expected_hotel_key = (expected_hotel_key or "").strip() or None
    expired = False
    row = None
    with connect() as conn:
        with conn.cursor() as cur:
            if expected_hotel_key:
                cur.execute("SELECT hotel_key FROM weekly_review_candidate_drafts WHERE id=%s FOR UPDATE", (candidate_id,))
                owner = cur.fetchone()
                if not owner:
                    raise ValueError("候选不存在")
                if str(owner["hotel_key"]) != expected_hotel_key:
                    raise ValueError("候选不属于当前门店")
            expired = _expire_locked_candidate(cur, candidate_id)
            if not expired:
                cur.execute("SELECT status,evidence_status,conflict_status FROM weekly_review_candidate_drafts WHERE id=%s FOR UPDATE", (candidate_id,))
                row = cur.fetchone()
                if not row:
                    raise ValueError("候选不存在")
                status = str(row["status"])
        conn.commit()
    if expired:
        raise ValueError(EXPIRY_ERROR)
    if action == "approve":
        if status != "pending_review":
            raise ValueError("候选尚未满足批准条件，请先补充条件或证据")
        review(candidate_id, "approve", actor, comment or "飞书卡片批准")
        return {"action": "approve", "sync": sync(candidate_id)}
    if action == "reject":
        if status not in {"pending_review", "needs_more_evidence", "pending_validation"}:
            raise ValueError("候选已处理，不能重复拒绝")
        review(candidate_id, "reject", actor, comment or "飞书卡片拒绝")
        return {"action": "reject"}
    if action == "enrich_approve":
        if status not in {"needs_more_evidence", "pending_validation", "pending_review"}:
            raise ValueError("候选已处理，不能修改后批准")
        if not conditions.strip():
            if status == "pending_review":
                raise ValueError("如认可当前适用条件请直接批准；如需修改请填写新的适用条件")
            raise ValueError("候选未通过校验，请填写具体适用条件后再批准")
        enrich(candidate_id, actor, comment or "飞书卡片修改并批准", conditions.strip(), evidence_refs or [])
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status,evidence_status,conflict_status FROM weekly_review_candidate_drafts WHERE id=%s", (candidate_id,))
                checked = cur.fetchone()
        if not checked or checked["status"] != "pending_review" or checked["evidence_status"] != "valid" or checked["conflict_status"] != "clear":
            return {"action": "enrich_approve", "approved": False, "status": str(checked["status"]) if checked else "missing"}
        review(candidate_id, "approve", actor, comment or "飞书卡片补充并批准")
        return {"action": "enrich_approve", "approved": True, "sync": sync(candidate_id)}
    if status not in {"needs_more_evidence", "pending_validation", "pending_review"}:
        raise ValueError("候选当前不允许修改适用条件")
    normalized_refs = evidence_refs or []
    if not conditions.strip():
        raise ValueError("请填写适用条件")
    enrich(candidate_id, actor, comment or "飞书卡片补充条件", conditions.strip(), normalized_refs)
    return {"action": "enrich", "status": "pending_review"}


def enrich_by_ref(candidate_ref: str, actor: str, comment: str, conditions: str) -> dict:
    prefix = re.sub(r"[^0-9a-fA-F]", "", candidate_ref).lower()
    if len(prefix) < 8:
        raise ValueError("候选编号无效")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT id FROM weekly_review_candidate_drafts
                WHERE replace(id::text,'-','') LIKE %s
                  AND status IN ('needs_more_evidence','pending_validation','pending_review')
                ORDER BY created_at DESC LIMIT 2""", (prefix + "%",))
            matches = cur.fetchall()
    if len(matches) != 1:
        raise ValueError("未找到唯一的待审核候选")
    return card_action(str(matches[0]["id"]), "enrich", actor, comment, conditions, [])


def notify_pending(limit: int = 50) -> dict:
    """Deliver one idempotent management card per current reviewable draft."""
    delivered = failed = skipped = 0
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT d.id,d.hotel_key,d.week_start,d.week_end,d.experience_summary,d.applicable_conditions,d.evidence_refs,
                    d.status,d.evidence_status,d.conflict_status,
                    (SELECT r.feishu_chat_env_key
                       FROM hotel_push_routes AS r
                      WHERE r.own_hotel_key=d.hotel_key
                        AND r.push_type='weekly_review'
                        AND r.delivery_channel='feishu_app'
                        AND r.is_active=TRUE
                        AND COALESCE(r.feishu_chat_env_key, '') <> ''
                      ORDER BY r.id ASC
                      LIMIT 1) AS feishu_chat_env_key
                FROM weekly_review_candidate_drafts AS d
                WHERE d.status IN ('needs_more_evidence','pending_review')
                  AND d.admin_notification_status IN ('pending','failed')
                  AND EXISTS (
                      SELECT 1
                        FROM hotel_push_routes AS r
                       WHERE r.own_hotel_key=d.hotel_key
                         AND r.push_type='weekly_review'
                         AND r.delivery_channel='feishu_app'
                         AND r.is_active=TRUE
                         AND COALESCE(r.feishu_chat_env_key, '') <> ''
                  )
                ORDER BY d.created_at ASC LIMIT %s FOR UPDATE OF d SKIP LOCKED""", (limit,))
            rows = [dict(row) for row in cur.fetchall()]
            for row in rows:
                cur.execute("""UPDATE weekly_review_candidate_drafts SET admin_notification_status='sending',
                    admin_notification_error=NULL,updated_at=now() WHERE id=%s AND admin_notification_status IN ('pending','failed')""", (row["id"],))
            conn.commit()
    for row in rows:
        candidate_path = ""
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
                json.dump(row, handle, ensure_ascii=False, default=str)
                candidate_path = handle.name
            completed = subprocess.run([HERMES_PYTHON, CARD_SENDER, "--candidate-json", candidate_path], text=True, capture_output=True, timeout=45, check=False)
            if completed.returncode:
                raise RuntimeError((completed.stderr or completed.stdout or "飞书卡片投递失败").strip()[:500])
            message_id = str(json.loads(completed.stdout)["message_id"])
            with connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""UPDATE weekly_review_candidate_drafts SET admin_notification_status='sent',
                        admin_feishu_message_id=%s,admin_notified_at=now(),updated_at=now() WHERE id=%s AND admin_notification_status='sending'""", (message_id,row["id"]))
                conn.commit()
            delivered += 1
        except Exception as exc:
            with connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""UPDATE weekly_review_candidate_drafts SET admin_notification_status='failed',
                        admin_notification_error=%s,updated_at=now() WHERE id=%s AND admin_notification_status='sending'""", (str(exc)[:500],row["id"]))
                conn.commit()
            failed += 1
        finally:
            try:
                if candidate_path:
                    __import__("os").unlink(candidate_path)
            except OSError:
                pass
    return {"delivered": delivered, "failed": failed, "skipped": skipped}


def main() -> None:
    parser=argparse.ArgumentParser(description="周报候选治理")
    sub=parser.add_subparsers(dest="command",required=True)
    gen=sub.add_parser("generate"); week=gen.add_mutually_exclusive_group(required=True); week.add_argument("--week-start"); week.add_argument("--auto-last-week",action="store_true"); gen.add_argument("--hotel-key",default="all"); gen.add_argument("--write",action="store_true"); gen.add_argument("--notify",action="store_true")
    rev=sub.add_parser("review"); rev.add_argument("--candidate-id",required=True); rev.add_argument("--action",required=True); rev.add_argument("--actor",required=True); rev.add_argument("--comment",required=True)
    enr=sub.add_parser("enrich"); enr.add_argument("--candidate-id",required=True); enr.add_argument("--actor",required=True); enr.add_argument("--comment",required=True); enr.add_argument("--conditions",required=True); enr.add_argument("--evidence-refs",required=True,help="JSON 数组")
    card=sub.add_parser("card-action"); card.add_argument("--candidate-id",required=True); card.add_argument("--action",required=True); card.add_argument("--actor",required=True); card.add_argument("--comment",default=""); card.add_argument("--conditions",default=""); card.add_argument("--evidence-refs",default="[]",help="JSON 数组"); card.add_argument("--hotel-key",default="")
    byref=sub.add_parser("enrich-by-ref"); byref.add_argument("--candidate-ref",required=True); byref.add_argument("--actor",required=True); byref.add_argument("--comment",default=""); byref.add_argument("--conditions",required=True)
    syn=sub.add_parser("sync"); syn.add_argument("--candidate-id",required=True)
    exp=sub.add_parser("expire-overdue"); exp.add_argument("--limit",type=int,default=500); exp.add_argument("--dry-run",action="store_true")
    ntf=sub.add_parser("notify-pending"); ntf.add_argument("--limit",type=int,default=50)
    a=parser.parse_args()
    if a.command=="generate":
        result = generate_many(a.week_start or last_natural_week_start(),a.hotel_key,a.write)
        if a.notify:
            if not a.write: raise ValueError("真实卡片投递必须同时使用 --write")
            result["notifications"] = notify_pending()
        print(json.dumps(result,ensure_ascii=False,default=str))
    elif a.command=="review": review(a.candidate_id,a.action,a.actor,a.comment); print("reviewed")
    elif a.command=="enrich": enrich(a.candidate_id,a.actor,a.comment,a.conditions,json.loads(a.evidence_refs)); print("enriched")
    elif a.command=="card-action":
        if not a.hotel_key:
            raise ValueError("分店审核必须提供 hotel-key")
        print(json.dumps(card_action(a.candidate_id,a.action,a.actor,a.comment,a.conditions,json.loads(a.evidence_refs),a.hotel_key),ensure_ascii=False))
    elif a.command=="enrich-by-ref":
        raise ValueError("周复盘候选审核已转由对应分店群处理")
    elif a.command=="expire-overdue": print(json.dumps(expire_overdue(a.limit,a.dry_run),ensure_ascii=False))
    elif a.command=="notify-pending": print(json.dumps(notify_pending(a.limit),ensure_ascii=False))
    else: print("sync="+sync(a.candidate_id))

if __name__ == "__main__": main()
