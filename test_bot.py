import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta
import main

class TestAblBot(unittest.TestCase):
    def setUp(self):
        # Очищаем или изолируем конфигурацию чатов
        self.mock_config = {}
        def _save(cfg):
            self.mock_config.clear()
            self.mock_config.update(cfg)
        self.patcher_load = patch("main.load_chats_config", side_effect=lambda: dict(self.mock_config))
        self.patcher_save = patch("main.save_chats_config", side_effect=_save)
        self.mock_load = self.patcher_load.start()
        self.mock_save = self.patcher_save.start()

    def tearDown(self):
        self.patcher_load.stop()
        self.patcher_save.stop()

    # --- 1. Next game fetching & formatting (включая проверку Timezone без zoneinfo) ---

    @patch("main.requests.get")
    def test_fetch_next_game_home_team_and_timezone(self, mock_get):
        # 12:00 UTC должно стать 15:00 МСК (+3 часа)
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {
                "id": 101,
                "status": "open",
                "team": {"name": "БК Путилково"},
                "competitor_team": {"name": "Соперник"},
                "tournament": {"name": "Москвич"},
                "tournament_tour": 1,
                "tournament_court": {"name": "Зал 1"},
                "location": "Москва",
                "datetime": "2026-10-18T12:00:00Z"
            }
        ]
        mock_get.return_value = mock_resp

        game_id, text = main.fetch_next_game("БК Путилково")
        self.assertEqual(game_id, 101)
        self.assertIn("БК Путилково — Соперник", text)
        self.assertIn("⚪️ СВЕТЛАЯ", text)  # Левая команда = светлая форма
        self.assertIn("18.10.2026 в 15:00 (МСК)", text)

    @patch("main.requests.get")
    def test_fetch_next_game_away_team(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {
                "id": 102,
                "status": "open",
                "team": {"name": "Хозяева"},
                "competitor_team": {"name": "БК Путилково"},
                "tournament": {"name": "Лига 1"},
                "datetime": "2026-10-18T16:30:00Z"
            }
        ]
        mock_get.return_value = mock_resp

        game_id, text = main.fetch_next_game("БК Путилково")
        self.assertEqual(game_id, 102)
        self.assertIn("⚫️ ЧЁРНАЯ", text)  # Правая команда = темная форма
        self.assertIn("18.10.2026 в 19:30 (МСК)", text)

    @patch("main.requests.get")
    def test_fetch_next_game_no_games(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_get.return_value = mock_resp

        game_id, text = main.fetch_next_game("Неизвестная Команда")
        self.assertIsNone(game_id)
        self.assertIn("Пока нет открытых назначенных игр", text)

    @patch("main.requests.get")
    def test_fetch_next_game_network_error(self, mock_get):
        mock_get.side_effect = Exception("Connection timeout")
        game_id, text = main.fetch_next_game("БК Путилково")
        self.assertIsNone(game_id)
        self.assertIn("Не удалось получить данные о расписании ABL", text)

    @patch("main.requests.get")
    def test_fetch_schedule(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {
                "id": 101,
                "status": "closed",
                "team": {"name": "БК Путилково"},
                "competitor_team": {"name": "Старый Соперник"},
                "team_score": 75,
                "competitor_team_score": 68,
                "datetime": "2026-10-01T12:00:00Z"
            },
            {
                "id": 102,
                "status": "open",
                "team": {"name": "БК Путилково"},
                "competitor_team": {"name": "Новый Соперник"},
                "datetime": "2026-10-25T12:00:00Z"
            }
        ]
        mock_get.return_value = mock_resp

        text = main.fetch_schedule("БК Путилково")
        self.assertIn("Календарь матчей ABL для БК Путилково", text)
        self.assertIn("75:68", text)
        self.assertIn("Новый Соперник", text)

    # --- 2. Command handling (/game, /game Team, /abl, /schedule, /mlbl, etc.) ---

    @patch("main.send_msg")
    def test_command_start_help_private(self, mock_send):
        msg = {
            "text": "/start",
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345}
        }
        main.process_message(msg)
        mock_send.assert_called_once()
        self.assertIn("Привет!", mock_send.call_args[0][1])

    @patch("main.send_msg")
    @patch("main.fetch_next_game")
    def test_command_game_default(self, mock_fetch, mock_send):
        mock_fetch.return_value = (999, "Ближайшая игра: БК Путилково")
        msg = {
            "text": "/game",
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345}
        }
        main.process_message(msg)
        mock_fetch.assert_called_with("БК Путилково", "")
        mock_send.assert_called_with("12345", "Ближайшая игра: БК Путилково")

    @patch("main.send_msg")
    @patch("main.fetch_next_game")
    def test_command_game_with_team_query(self, mock_fetch, mock_send):
        mock_fetch.return_value = (999, "Игра команды Спартак")
        msg = {
            "text": "/game Спартак",
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345}
        }
        main.process_message(msg)
        mock_fetch.assert_called_with("Спартак", "")

    @patch("main.send_msg")
    @patch("main.fetch_next_game")
    def test_command_game_with_bot_mention(self, mock_fetch, mock_send):
        # Проверяем, что /game@abl_bot Спартак не подмешивает имя бота в название команды
        mock_fetch.return_value = (999, "Игра Спартак")
        msg = {
            "text": "/game@abl_basket_bot Спартак",
            "chat": {"id": -100999, "type": "group"},
            "from": {"id": 111}
        }
        main.process_message(msg)
        mock_fetch.assert_called_with("Спартак", "")

    @patch("main.send_msg")
    @patch("main.fetch_next_game")
    def test_command_game_mention_without_args(self, mock_fetch, mock_send):
        # /game@abl_basket_bot без аргументов
        self.mock_config["-100999"] = {"team": "Моя Команда", "division": ""}
        mock_fetch.return_value = (999, "Игра Моя Команда")
        msg = {
            "text": "/game@abl_basket_bot",
            "chat": {"id": -100999, "type": "group"},
            "from": {"id": 111}
        }
        main.process_message(msg)
        mock_fetch.assert_called_with("Моя Команда", "")

    @patch("main.send_msg")
    @patch("main.fetch_next_game")
    def test_command_abl_alias(self, mock_fetch, mock_send):
        mock_fetch.return_value = (999, "Игра ABL")
        msg = {
            "text": "/abl ЦСКА",
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345}
        }
        main.process_message(msg)
        mock_fetch.assert_called_with("ЦСКА", "")

    @patch("main.send_msg")
    def test_command_mlbl_cross_info(self, mock_send):
        msg = {
            "text": "/mlbl",
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345}
        }
        main.process_message(msg)
        mock_send.assert_called_once()
        self.assertIn("Этот бот предназначен для лиги ABL", mock_send.call_args[0][1])

    @patch("main.send_msg")
    @patch("main.fetch_schedule")
    def test_command_schedule(self, mock_fetch_sched, mock_send):
        mock_fetch_sched.return_value = "Календарь матчей"
        msg = {
            "text": "/schedule БК Путилково",
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345}
        }
        main.process_message(msg)
        mock_fetch_sched.assert_called_with("БК Путилково", "")
        mock_send.assert_called_with("12345", "Календарь матчей")

    # --- 3. Non-slash message filtering ---

    @patch("main.send_msg")
    def test_ignore_plain_text_messages(self, mock_send):
        for plain in ["игра", "форма", "привет бот", "расписание на завтра", "11.10"]:
            msg = {
                "text": plain,
                "chat": {"id": 12345, "type": "group"},
                "from": {"id": 12345}
            }
            main.process_message(msg)
        mock_send.assert_not_called()

    @patch("main.send_msg")
    def test_ignore_non_text_messages(self, mock_send):
        msg = {
            "photo": [{"file_id": "123"}],
            "chat": {"id": 12345, "type": "group"},
            "from": {"id": 12345}
        }
        main.process_message(msg)
        mock_send.assert_not_called()

    # --- 4. Admin permission checks & message deletion logic ---

    @patch("main.send_msg")
    @patch("main.delete_later")
    @patch("main.is_group_admin", return_value=False)
    def test_set_rejected_for_non_admin_in_group(self, mock_admin, mock_delete, mock_send):
        mock_send.return_value = 555
        msg = {
            "message_id": 444,
            "text": "/set Химки",
            "chat": {"id": -100555, "type": "group"},
            "from": {"id": 999}
        }
        main.process_message(msg)
        # Отправлено предупреждение
        mock_send.assert_called_with("-100555", "⛔️ Только администратор этой группы может настраивать или менять команду.")
        # Запланировано автоудаление
        mock_delete.assert_called_once_with("-100555", [555, 444], delay=1.5)
        # Конфигурация не изменена
        self.assertNotIn("-100555", self.mock_config)

    @patch("main.send_msg")
    @patch("main.is_group_admin", return_value=True)
    def test_set_allowed_for_admin_in_group(self, mock_admin, mock_send):
        msg = {
            "message_id": 444,
            "text": "/set Химки | Первый дивизион",
            "chat": {"id": -100555, "type": "group"},
            "from": {"id": 999}
        }
        main.process_message(msg)
        self.assertIn("-100555", self.mock_config)
        self.assertEqual(self.mock_config["-100555"]["team"], "Химки")
        self.assertEqual(self.mock_config["-100555"]["division"], "Первый дивизион")

    @patch("main.send_msg")
    @patch("main.is_group_admin", return_value=True)
    def test_unset_command(self, mock_admin, mock_send):
        self.mock_config["-100555"] = {"team": "Химки", "division": ""}
        msg = {
            "text": "/unset",
            "chat": {"id": -100555, "type": "group"},
            "from": {"id": 999}
        }
        main.process_message(msg)
        self.assertNotIn("-100555", self.mock_config)
        self.assertIn("удалена", mock_send.call_args[0][1])

    # --- 5. Edge cases ---

    @patch("main.send_msg")
    def test_just_slash_and_unknown_command(self, mock_send):
        for raw in ["/", "/   ", "/unknown_command", "/xyz 123"]:
            msg = {
                "text": raw,
                "chat": {"id": 12345, "type": "private"},
                "from": {"id": 12345}
            }
            main.process_message(msg)
        # Неизвестные команды и одиночный слэш не должны вызывать падений или невалидных ответов
        mock_send.assert_not_called()

    @patch("main.send_msg")
    def test_set_without_arguments(self, mock_send):
        msg = {
            "text": "/set",
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345}
        }
        main.process_message(msg)
        self.assertIn("Укажите команду", mock_send.call_args[0][1])

    @patch("main.requests.post")
    def test_send_msg_markdown_fallback(self, mock_post):
        # 1-й вызов с Markdown возвращает ошибку (например, неэкранированный спецсимвол)
        # 2-й вызов без parse_mode успешен
        mock_resp_fail = MagicMock()
        mock_resp_fail.json.return_value = {"ok": False, "description": "Bad Request: can't parse entities"}
        mock_resp_ok = MagicMock()
        mock_resp_ok.json.return_value = {"ok": True, "result": {"message_id": 9999}}
        mock_post.side_effect = [mock_resp_fail, mock_resp_ok]

        mid = main.send_msg("12345", "Текст с *незакрытой звездочкой")
        self.assertEqual(mid, 9999)
        self.assertEqual(mock_post.call_count, 2)
        # Проверяем, что второй вызов был без parse_mode
        self.assertNotIn("parse_mode", mock_post.call_args_list[1][1]["json"])

if __name__ == "__main__":
    unittest.main()
