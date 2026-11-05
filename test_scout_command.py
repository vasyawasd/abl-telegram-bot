import os
import unittest
from unittest.mock import patch

os.environ["BOT_TOKEN"] = "test-token"
import main


class TestScoutCommand(unittest.TestCase):
    def test_scout_selects_next_opponent_or_explicit_team_for_all_members(self):
        cases = [
            ("/scout", "private", {}, "БК Путилково", False),
            ("/scout", "supergroup", {}, "БК Путилково", False),
            ("/scout", "supergroup", {"team": "Метеор | Старый дивизион"}, "Метеор", False),
            ("/scout", "supergroup", {"team": "Метеор", "division": "Старый дивизион"}, "Метеор", False),
            ("/scout HDXSEVER", "supergroup", {"team": "Метеор"}, "HDXSEVER", True),
            ("/scout@abl_bot БК Север | Старый дивизион", "supergroup", {}, "БК Север", True),
            ("/скаут", "group", {"team": "Метеор"}, "Метеор", False),
            ("/скаут HDXSEVER", "group", {"team": "Метеор"}, "HDXSEVER", True),
            ("/scout | Старый дивизион", "private", {}, "БК Путилково", True),
        ]
        for command, chat_type, config, expected_team, direct in cases:
            with self.subTest(command=command, chat_type=chat_type, config=config):
                with patch("main.DEFAULT_TEAM", "БК Путилково"), \
                     patch("main.load_chats_config", return_value={"123": config}), \
                     patch("main.fetch_scout", return_value="Отчёт о ближайшем сопернике", create=True) as scout, \
                     patch("main.send_msg") as send, \
                     patch("main.send_poll") as poll, \
                     patch("main.is_group_admin", return_value=False) as admin:
                    main.process_message({
                        "text": command, "chat": {"id": 123, "type": chat_type},
                        "from": {"id": 456}, "message_id": 789,
                    })
                    scout.assert_called_once_with(expected_team, direct=direct)
                    send.assert_called_once_with("123", "Отчёт о ближайшем сопернике")
                    poll.assert_not_called()
                    admin.assert_not_called()

    def test_help_exposes_scout_in_private_and_groups(self):
        for chat_type, config in [("private", {}), ("supergroup", {}),
                                  ("supergroup", {"team": "Метеор"})]:
            with self.subTest(chat_type=chat_type, config=config):
                with patch("main.load_chats_config", return_value={"123": config}), \
                     patch("main.send_msg") as send:
                    main.process_message({"text": "/help", "chat": {"id": 123, "type": chat_type}})
                    send.assert_called_once()
                    self.assertIn("/scout", send.call_args.args[1])
                    self.assertIn("/scout Команда", send.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
