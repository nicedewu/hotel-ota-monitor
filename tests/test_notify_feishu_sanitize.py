import unittest

from scripts.notify_feishu import sanitize_feishu_message


class NotifyFeishuSanitizeTest(unittest.TestCase):
    def test_room_configuration_suffixes_are_removed(self) -> None:
        message = "\n".join(
            [
                "舒适电竞大床房（5070+华硕2K300+97X+零压床垫）",
                "电竞双床房(4070+2K)",
                "电竞浴缸大床房[5060Ti+2K260hz+DDR5-32G]",
                "特价无窗大床房【4070+2k300+三角洲特权】",
                "舒适电竞大床房（523元）",
            ]
        )

        cleaned = sanitize_feishu_message(message)

        self.assertEqual(
            cleaned.splitlines(),
            [
                "舒适电竞大床房",
                "电竞双床房",
                "电竞浴缸大床房",
                "特价无窗大床房",
                "舒适电竞大床房（523元）",
            ],
        )


if __name__ == "__main__":
    unittest.main()
