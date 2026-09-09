"""巡检群自动拉机器人服务单元测试。

覆盖：
- 「群ID」字段（群名 + applink 链接）解析 openConversationId（含 %3D%3D / &amp; 等场景）；
- 群机器人列表判定（robotCode 匹配）。
"""

from __future__ import annotations

import unittest

from services import group_bot_sync_service as gs


class ExtractOpenConversationIdTest(unittest.TestCase):
    def test_typical_applink_with_urlencoded_equals(self):
        text = (
            "售后巡检-中国证券登记结算有限责任公司深圳分公司-主动威胁欺骗防御系统（谛听）"
            "https://applink.dingtalk.com/page/conversation?corpId=ding56395822e2c6d50035c2f4657eb6378f"
            "&openConversationId=cidptN5zhwHHAOqlxzTftrlKg%3D%3D"
        )
        self.assertEqual(
            gs.extract_open_conversation_id(text),
            "cidptN5zhwHHAOqlxzTftrlKg==",
        )

    def test_html_escaped_ampersand(self):
        text = (
            "群名https://applink.dingtalk.com/page/conversation?corpId=ding1"
            "&amp;openConversationId=cidabc123%3D%3D"
        )
        self.assertEqual(gs.extract_open_conversation_id(text), "cidabc123==")

    def test_plain_equals_only(self):
        text = "https://x/?openConversationId=cidplain123"
        self.assertEqual(gs.extract_open_conversation_id(text), "cidplain123")

    def test_no_conversation_id(self):
        self.assertIsNone(gs.extract_open_conversation_id("售后巡检-无链接群名"))
        self.assertIsNone(gs.extract_open_conversation_id("https://applink.dingtalk.com/page/conversation?corpId=x"))

    def test_non_string(self):
        self.assertIsNone(gs.extract_open_conversation_id(None))
        self.assertIsNone(gs.extract_open_conversation_id(123))


class RobotInBotsTest(unittest.TestCase):
    def test_robot_present(self):
        bots = [
            {"name": "AI小钉", "robotCode": "intelligent"},
            {"name": "增值服务确认消息推送", "robotCode": "dingi0fmclwiroilfgca", "status": 1},
        ]
        self.assertTrue(gs._is_robot_in_bots(bots, gs.ROBOT_CODE))

    def test_robot_absent(self):
        bots = [{"name": "AI小钉", "robotCode": "intelligent"}]
        self.assertFalse(gs._is_robot_in_bots(bots, gs.ROBOT_CODE))

    def test_not_list(self):
        self.assertFalse(gs._is_robot_in_bots(None, gs.ROBOT_CODE))
        self.assertFalse(gs._is_robot_in_bots({"x": 1}, gs.ROBOT_CODE))


if __name__ == "__main__":
    unittest.main()
