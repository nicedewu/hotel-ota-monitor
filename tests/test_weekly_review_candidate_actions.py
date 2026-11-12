from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from scripts.weekly_review_candidates import card_action, expire_overdue, main, notify_pending, review


def connection_with_row(row: dict, *, expired: bool = False, check_expiry: bool = True) -> MagicMock:
    connection = MagicMock()
    cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
    if expired:
        cursor.fetchone.side_effect = [{"id": row.get("id", "candidate-1")}]
    elif check_expiry:
        cursor.fetchone.side_effect = [None, row]
    else:
        cursor.fetchone.return_value = row
    return connection


class WeeklyCandidateActionTests(unittest.TestCase):
    def test_reject_requires_reason(self) -> None:
        initial = connection_with_row({"status": "pending_review", "evidence_status": "valid", "conflict_status": "clear"})
        with patch("scripts.weekly_review_candidates.connect", return_value=initial):
            with self.assertRaisesRegex(ValueError, "退回时必须填写理由"):
                card_action("candidate-1", "reject", "manager-1", "")

    def test_reject_cannot_overwrite_processed_candidate(self) -> None:
        connection = MagicMock()
        cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [
            None,
            {"status": "synced", "evidence_status": "valid", "conflict_status": "clear"},
        ]
        with patch("scripts.weekly_review_candidates.connect", return_value=connection):
            with self.assertRaisesRegex(ValueError, "候选已处理"):
                review("candidate-1", "reject", "manager-1", "重复候选")
        self.assertFalse(any("weekly_review_candidate_reviews" in call.args[0] for call in cursor.execute.call_args_list))

    def test_review_update_requires_atomic_status_match(self) -> None:
        connection = MagicMock()
        cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [
            None,
            {"status": "pending_review", "evidence_status": "valid", "conflict_status": "clear"},
            None,
        ]
        with patch("scripts.weekly_review_candidates.connect", return_value=connection):
            with self.assertRaisesRegex(ValueError, "已被其他人处理"):
                review("candidate-1", "reject", "manager-1", "并发测试")
        update_sql = next(call.args[0] for call in cursor.execute.call_args_list if "SET status=%s" in call.args[0] and "RETURNING id" in call.args[0])
        self.assertIn("AND status=%s", update_sql)
        self.assertFalse(any("weekly_review_candidate_reviews" in call.args[0] for call in cursor.execute.call_args_list))

    def test_cross_store_candidate_is_rejected(self) -> None:
        connection = MagicMock()
        cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {"hotel_key": "own_other"}
        with patch("scripts.weekly_review_candidates.connect", return_value=connection):
            with self.assertRaisesRegex(ValueError, "不属于当前门店"):
                card_action("candidate-1", "approve", "manager-1", "", expected_hotel_key="own_demo_a")

    def test_notify_pending_locks_candidate_table_only(self) -> None:
        connection = MagicMock()
        cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = []
        with patch("scripts.weekly_review_candidates.connect", return_value=connection):
            result = notify_pending(limit=0)
        self.assertEqual(result, {"delivered": 0, "failed": 0, "skipped": 0})
        query = cursor.execute.call_args_list[0].args[0]
        self.assertIn("FOR UPDATE OF d SKIP LOCKED", query)
        self.assertNotIn("LEFT JOIN LATERAL", query)

    def test_valid_candidate_can_be_modified_then_approved(self) -> None:
        initial = connection_with_row({"status": "pending_review", "evidence_status": "valid", "conflict_status": "clear"})
        checked = connection_with_row({"status": "pending_review", "evidence_status": "valid", "conflict_status": "clear"}, check_expiry=False)
        with (
            patch("scripts.weekly_review_candidates.connect", side_effect=[initial, checked]),
            patch("scripts.weekly_review_candidates.enrich") as enrich,
            patch("scripts.weekly_review_candidates.review") as review,
            patch("scripts.weekly_review_candidates.sync", return_value="direct"),
        ):
            result = card_action("candidate-1", "enrich_approve", "admin-1", "", "仅在周末库存紧张时适用", [])
        enrich.assert_called_once()
        review.assert_called_once()
        self.assertTrue(result["approved"])

    def test_valid_candidate_blank_edit_uses_direct_approve_button(self) -> None:
        initial = connection_with_row({"status": "pending_review", "evidence_status": "valid", "conflict_status": "clear"})
        with patch("scripts.weekly_review_candidates.connect", return_value=initial):
            with self.assertRaisesRegex(ValueError, "直接批准"):
                card_action("candidate-1", "enrich_approve", "admin-1", "", "", [])

    def test_invalid_candidate_still_requires_specific_conditions(self) -> None:
        initial = connection_with_row({"status": "needs_more_evidence", "evidence_status": "invalid", "conflict_status": "clear"})
        with patch("scripts.weekly_review_candidates.connect", return_value=initial):
            with self.assertRaisesRegex(ValueError, "未通过校验"):
                card_action("candidate-1", "enrich_approve", "admin-1", "", "", [])

    def test_expired_card_action_defaults_to_rejected(self) -> None:
        initial = connection_with_row({"id": "candidate-1", "status": "pending_review"}, expired=True)
        with patch("scripts.weekly_review_candidates.connect", return_value=initial):
            with self.assertRaisesRegex(ValueError, "超过24小时"):
                card_action("candidate-1", "approve", "admin-1", "")
        initial.__enter__.return_value.commit.assert_called_once()

    def test_expire_overdue_records_default_rejection(self) -> None:
        connection = MagicMock()
        cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [{"id": "candidate-1"}]
        cursor.fetchone.return_value = {"id": "candidate-1"}
        with patch("scripts.weekly_review_candidates.connect", return_value=connection):
            result = expire_overdue()
        self.assertEqual(result["eligible"], 1)
        self.assertEqual(result["expired"], 1)
        self.assertGreaterEqual(cursor.execute.call_count, 3)
        self.assertIn("weekly_review_candidate_reviews", cursor.execute.call_args_list[-1].args[0])

    def test_cli_card_action_requires_hotel_key(self) -> None:
        with patch("sys.argv", ["weekly_review_candidates.py", "card-action", "--candidate-id", "candidate-1", "--action", "approve", "--actor", "manager-1"]):
            with self.assertRaisesRegex(ValueError, "hotel-key"):
                main()

    def test_cli_enrich_by_ref_is_disabled(self) -> None:
        with patch("sys.argv", ["weekly_review_candidates.py", "enrich-by-ref", "--candidate-ref", "E-ABCDEF123456", "--actor", "manager-1", "--conditions", "仅周末适用"]):
            with self.assertRaisesRegex(ValueError, "转由对应分店群处理"):
                main()

if __name__ == "__main__":
    unittest.main()
