import unittest

from src.weekly_review_candidate_governance import build_drafts, memory_payload, validate_draft


class WeeklyCandidateGovernanceTests(unittest.TestCase):
    def test_draft_requires_two_real_fact_refs(self):
        report = {
            "fact_snapshot": {"fact_quality": {"passed": True}, "fact_references": ["fact:metrics:x", "fact:inventory:x"]},
            "analysis": {"key_findings": [{"finding": "库存紧", "fact_refs": ["fact:inventory:x"]}], "actions": [{"action": "减少低价房量", "reason": "库存紧", "fact_refs": ["fact:metrics:x"]}]},
        }
        draft = build_drafts(report)[0]
        self.assertTrue(validate_draft(draft, report["fact_snapshot"])["valid"])

    def test_unknown_fact_ref_is_not_approvable(self):
        snapshot = {"fact_quality": {"passed": True}, "fact_references": ["fact:metrics:x", "fact:inventory:x"]}
        draft = {"experience_summary": "结论", "applicable_conditions": "条件", "applicability": "本店", "evidence_refs": ["fact:metrics:x", "fact:invented"]}
        self.assertFalse(validate_draft(draft, snapshot)["valid"])

    def test_generic_conditions_require_administrator_enrichment(self):
        snapshot = {"fact_quality": {"passed": True}, "fact_references": ["fact:metrics:x", "fact:inventory:x"]}
        draft = {"experience_summary": "结论", "applicable_conditions": "需由管理员结合已引用事实确认适用条件。", "applicability": "本店", "evidence_refs": ["fact:metrics:x", "fact:inventory:x"]}
        self.assertFalse(validate_draft(draft, snapshot)["valid"])

    def test_approved_memory_payload_keeps_hotel_scope_and_evidence(self):
        content, metadata = memory_payload({"id":"candidate-1","weekly_review_id":1,"hotel_key":"own_demo_a","week_start":"2026-07-13","week_end":"2026-07-19","rule_type":"weekly_operating_learning","experience_summary":"动作","applicable_conditions":"条件","applicability":"本店","confidence":"medium","evidence_refs":["fact:metrics:x","fact:inventory:x"],"reviewed_by":"admin"})
        self.assertEqual(metadata["scope"], "hotel")
        self.assertEqual(metadata["review_status"], "approved")
        self.assertIn("证据摘要", content)
