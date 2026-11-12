from pathlib import Path
from unittest.mock import patch

from scripts.notify_feishu import build_business_snapshot_message
from src.capture import _page_status_value
from src.extract import _extract_ctrip_room_list, _has_hidden_price
from src.multi_hotel import _notify_admin_login_alert


def test_hidden_price_blocks_price_extraction_and_sets_login_required() -> None:
    payload = {
        "physicRoomMap": {"1": {"name": "电竞大床房"}},
        "saleRoomMap": {
            "1": {
                "physicalRoomId": 1,
                "isHidePrice": True,
                "priceInfo": {"price": 599},
                "cancelPolicy": {"amount": 199},
            }
        },
    }

    assert _has_hidden_price(payload)
    assert _extract_ctrip_room_list(payload, "测试酒店", "", "", Path("raw.json"), "") == []
    assert _page_status_value("success", "", True) == "login_required"


def test_admin_login_alert_uses_existing_admin_webhook() -> None:
    with patch("src.multi_hotel.subprocess.run") as run:
        _notify_admin_login_alert([{"own_hotel_key": "own_demo_a"}, {"own_hotel_key": "own_minhang"}])

    command = run.call_args.args[0]
    assert command[command.index("--webhook-env-key") + 1] == "ADMIN_FEISHU_WEBHOOK_URL"
    assert command[command.index("--reason") + 1] == "登录态异常：own_demo_a、own_minhang"
    assert run.call_args.kwargs["check"] is True


def test_admin_login_alert_message_blocks_price_work() -> None:
    message = build_business_snapshot_message(Path("."), "failed", "full", "登录态异常：own_demo_a")

    assert "【携程登录态异常告警】" in message
    assert "停止写入 OTA 价格、生成调价结论和推送门店经营快照" in message
