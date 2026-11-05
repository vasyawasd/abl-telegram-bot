import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ["BOT_TOKEN"] = "test-token"
import main


class TestSeasonTransition(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        chats = patch.object(main, "CHATS_FILE", os.path.join(directory.name, "chats.json"))
        chats.start()
        self.addCleanup(chats.stop)
        now = datetime.now(timezone.utc)
        # Реальные названия и ID из API ABL; даты относительные, чтобы тест не устарел.
        self.games = [
            {"id": 164598, "status": "closed", "datetime": (now - timedelta(days=7)).isoformat(),
             "team": {"id": 19520, "name": "BASKET COMPANY"},
             "competitor_team": {"id": 8329, "name": "БК Путилково"},
             "tournament": {"id": 4148, "name": "Москвич"},
             "team_score": 44, "competitor_team_score": 34},
            {"id": 168257, "status": "open", "datetime": (now + timedelta(days=3)).isoformat(),
             "team": {"id": 4097, "name": "BOOMS"},
             "competitor_team": {"id": 8329, "name": "БК Путилково"},
             "tournament": {"id": 4363, "name": "BASE"}, "tournament_tour": 2,
             "tournament_court": {"name": "Москвич Б"}, "location": "Волгоградский пр-т., 46/15с13"},
        ]
        fetch = patch("main.fetch_raw_abl_games", return_value=self.games)
        fetch.start()
        self.addCleanup(fetch.stop)
        main.save_chats_config({"1": {"team": "БК Путилково", "division": "Москвич",
                                      "enabled": True, "last_game_id": 164598,
                                      "reminded_stages": ["164598:announced", "164598:poll"]}})

    def test_next_game_uses_current_division_despite_legacy_filter(self):
        for team, division in [("БК Путилково", "Москвич"), ("БК Путилково | Москвич", "")]:
            with self.subTest(team=team):
                gid, card = main.fetch_next_game(team, division)
                self.assertEqual(gid, 168257)
                self.assertIn("*Дивизион:* BASE", card)
                self.assertIn("BOOMS", card)
                self.assertIn("ЧЁРНАЯ", card)

    def test_schedule_includes_main_season_after_qualification(self):
        schedule = main.fetch_schedule("БК Путилково", "Москвич")
        self.assertIn("BOOMS", schedule)
        self.assertIn("BASE", schedule)
        self.assertIn("BASKET COMPANY 44:34 БК Путилково", schedule)

    @patch("main.send_poll", return_value=102)
    @patch("main.send_msg", return_value=101)
    def test_existing_chat_follows_team_in_commands_and_monitor(self, send, poll):
        main.process_message({"text": "/game", "chat": {"id": 1, "type": "private"}})
        self.assertIn("BOOMS", send.call_args.args[1])
        self.assertIn("BASE", send.call_args.args[1])
        send.reset_mock()
        main.monitor_tick()
        send.assert_called_once()
        poll.assert_called_once()
        self.assertIn("BASE", send.call_args.args[1])
        main.monitor_tick()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(poll.call_count, 1)

    def test_other_team_does_not_match(self):
        gid, _ = main.fetch_next_game("Несуществующая команда", "Москвич")
        self.assertIsNone(gid)


if __name__ == "__main__":
    unittest.main()
