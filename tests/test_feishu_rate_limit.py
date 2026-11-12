import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import call, patch

from scripts import notify_feishu
from src.weekly_review import notify_weekly_report


def test_11232_retries_under_shared_lock_without_real_request() -> None:
    with TemporaryDirectory() as directory, patch.dict(os.environ, {
        "FEISHU_APP_LOCK_PATH": str(Path(directory) / "app.lock"),
        "FEISHU_APP_MIN_INTERVAL_SECONDS": "0",
    }, clear=False), patch.object(
        notify_feishu,
        "_send_feishu_once",
        side_effect=[notify_feishu.FeishuSendError("11232", "limited"), None],
    ) as send_once, patch.object(notify_feishu.time, "sleep") as sleep:
        notify_feishu.send_feishu("https://example.invalid/hook", {"msg_type": "text"})
    assert send_once.call_count == 2
    assert sleep.call_args_list == [call(30)]


def test_non_rate_limit_is_not_retried() -> None:
    with TemporaryDirectory() as directory, patch.dict(os.environ, {
        "FEISHU_APP_LOCK_PATH": str(Path(directory) / "app.lock"),
        "FEISHU_APP_MIN_INTERVAL_SECONDS": "0",
    }, clear=False), patch.object(
        notify_feishu,
        "_send_feishu_once",
        side_effect=notify_feishu.FeishuSendError("99999", "rejected"),
    ) as send_once, patch.object(notify_feishu.time, "sleep") as sleep:
        try:
            notify_feishu.send_feishu("https://example.invalid/hook", {"msg_type": "text"})
        except notify_feishu.FeishuSendError as exc:
            assert exc.code == "99999"
        else:  # pragma: no cover - assertion guard
            raise AssertionError("non-rate-limit response must fail")
    assert send_once.call_count == 1
    assert sleep.call_count == 0


def test_weekly_sender_returns_raw_11232_error_to_caller() -> None:
    errors: list[str] = []
    failed = subprocess.CalledProcessError(1, ["notify"], stderr="飞书发送失败: code=11232, msg=frequency limited")
    with patch("src.weekly_review.subprocess.run", side_effect=failed):
        status = notify_weekly_report("report.md", "ADMIN_FEISHU_WEBHOOK_URL", False, error_out=errors)
    assert status == "failed"
    assert errors == ["飞书发送失败: code=11232, msg=frequency limited"]
