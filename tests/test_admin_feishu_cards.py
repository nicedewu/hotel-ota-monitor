from __future__ import annotations

import sys
import unittest
from types import ModuleType


try:
    import lark_oapi  # noqa: F401
except ModuleNotFoundError:
    lark_module = ModuleType("lark_oapi")
    lark_module.Client = object
    im_module = ModuleType("lark_oapi.api.im.v1")
    im_module.CreateMessageRequest = object
    im_module.CreateMessageRequestBody = object
    const_module = ModuleType("lark_oapi.core.const")
    const_module.FEISHU_DOMAIN = "feishu"
    const_module.LARK_DOMAIN = "lark"
    sys.modules.update({
        "lark_oapi": lark_module,
        "lark_oapi.api": ModuleType("lark_oapi.api"),
        "lark_oapi.api.im": ModuleType("lark_oapi.api.im"),
        "lark_oapi.api.im.v1": im_module,
        "lark_oapi.core": ModuleType("lark_oapi.core"),
        "lark_oapi.core.const": const_module,
    })

from scripts.admin_feishu_cards import card, send


def candidate(status: str, evidence_status: str) -> dict:
    return {
        "id": "11111111-1111-1111-1111-111111111111",
        "hotel_key": "own_demo_a",
        "week_start": "2026-07-20",
        "week_end": "2026-07-26",
        "experience_summary": "继续观察库存",
        "applicable_conditions": "周末库存较紧时适用",
        "evidence_refs": ["fact:metrics:2026-07-20:own_demo_a"],
        "status": status,
        "evidence_status": evidence_status,
        "conflict_status": "clear",
    }


def rendered_text(payload: object) -> str:
    if isinstance(payload, dict):
        return "\n".join(rendered_text(value) for value in payload.values())
    if isinstance(payload, list):
        return "\n".join(rendered_text(value) for value in payload)
    return str(payload)


class AdminCandidateCardTests(unittest.TestCase):
    def test_valid_candidate_supports_direct_or_modified_approval(self) -> None:
        text = rendered_text(card(candidate("pending_review", "valid")))
        self.assertIn("批准", text)
        self.assertIn("修改并批准", text)
        self.assertIn("修改适用条件（可选）", text)
        self.assertIn("24 小时", text)
        self.assertIn("退回理由（拒绝时必填）", text)

    def test_invalid_candidate_marks_conditions_required(self) -> None:
        text = rendered_text(card(candidate("needs_more_evidence", "invalid")))
        self.assertNotIn("修改适用条件（可选）", text)
        self.assertIn("补充适用条件（必填）", text)
        self.assertIn("补充并批准", text)

    def test_reject_button_submits_form_values(self) -> None:
        payload = card(candidate("pending_review", "valid"))
        form = next(element for element in payload["body"]["elements"] if element.get("tag") == "form")
        buttons = [column["elements"][0] for column in form["elements"][1]["columns"] if column["elements"][0].get("tag") == "button"]
        actions = {button["behaviors"][0]["value"]["weekly_review_candidate_action"] for button in buttons}
        self.assertIn("reject", actions)
        self.assertTrue(all(button.get("form_action_type") == "submit" for button in buttons))

    def test_send_requires_branch_route(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "缺少分店群路由"):
            send(candidate("pending_review", "valid"))


if __name__ == "__main__":
    unittest.main()
