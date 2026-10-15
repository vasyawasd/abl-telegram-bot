import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta
import main

class TestAblBot(unittest.TestCase):
    def setUp(self):
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

    # --- 1. Next game fetching & formatting (Timezone and Form) ---

    @patch("main.requests.get")
    def test_fetch_next_game_home_team_and_timezone(self, mock_get):
        # 12:00 UTC должно стать 15:00 МСК (+3 часа) без zoneinfo
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
        self.assertIn("⚪️ СВЕТЛАЯ", text)
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
        self.assertIn("⚫️ ЧЁРНАЯ", text)
        self.assertIn("18.10.2026 в 19:30 (МСК)", text)

    @patch("main.requests.get")
    def test_fetch_next_game_smart_match_hd_x_sever(self, mock_get):
        # Точный кейс пользователя: запрос 'HD X Север' при базе с 'HDXСЕВЕР' и турниром 'PURE'
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {
                "id": 163799,
                "status": "open",
                "team": {"name": "HDXСЕВЕР"},
                "competitor_team": {"name": "Пиратская Руда"},
                "tournament": {"name": "PURE"},
                "datetime": "2026-10-17T14:00:00Z",
                "location": "Волгоградский пр-т"
            }
        ]
        mock_get.return_value = mock_resp

        # Проверяем запрос без дивизиона
        game_id, text = main.fetch_next_game("HD X Север")
        self.assertEqual(game_id, 163799)
        self.assertIn("HDXСЕВЕР — Пиратская Руда", text)
        self.assertIn("⚪️ СВЕТЛАЯ", text)

        # Проверяем запрос с дивизионом через пайп
        game_id2, text2 = main.fetch_next_game("HD X Север", "PURE")
        self.assertEqual(game_id2, 163799)

        # Проверяем запрос с дивизионом в скобках
        game_id3, text3 = main.fetch_next_game("HD X Север (PURE)")
        self.assertEqual(game_id3, 163799)

        # Проверяем с русской буквой Х вместо английской X
        game_id4, text4 = main.fetch_next_game("HD \u0445 Север")
        self.assertEqual(game_id4, 163799)

    def test_parse_team_and_division(self):
        self.assertEqual(main.parse_team_and_division("HD X Север | PURE"), ("HD X Север", "PURE"))
        self.assertEqual(main.parse_team_and_division("HD X Север (PURE)"), ("HD X Север", "PURE"))
        self.assertEqual(main.parse_team_and_division("HD X Север [PURE]"), ("HD X Север", "PURE"))
        self.assertEqual(main.parse_team_and_division("БК Путилково"), ("БК Путилково", ""))
        self.assertEqual(main.parse_team_and_division(""), ("", ""))

    def test_match_text_comprehensive(self):
        # Пробелы и регистр
        self.assertTrue(main.match_text("HD X Север", "HDXСЕВЕР"))
        self.assertTrue(main.match_text("hdxсевер", "HD X Север"))
        self.assertTrue(main.match_text("HDX", "HDXСЕВЕР"))
        self.assertTrue(main.match_text("Север", "HDXСЕВЕР"))
        # Русская 'х' vs латинская 'x'
        self.assertTrue(main.match_text("HD \u0445 Север", "HDXСЕВЕР"))
        # Транслитерация
        self.assertTrue(main.match_text("hd x sever", "HDxSEVER WOMEN"))
        self.assertTrue(main.match_text("HD X Север", "HDxSEVER WOMEN"))
        # Префикс БК и тире/кавычки
        self.assertTrue(main.match_text("БК Путилково", "БК Путилково"))
        self.assertTrue(main.match_text("Путилково", "БК Путилково"))
        self.assertTrue(main.match_text("БК Путилково", "Путилково"))
        self.assertTrue(main.match_text("Спартак-2", "Спартак 2"))
        self.assertTrue(main.match_text("Пиратская руда", "Пиратская Руда"))
        # Отрицательные тесты (не должно быть ложных срабатываний)
        self.assertFalse(main.match_text("ЦСКА", "Спартак"))
        self.assertFalse(main.match_text("Путилково", "HDXСЕВЕР"))
        self.assertFalse(main.match_text("а", "Спартак"))
        self.assertFalse(main.match_text("бк", "Спартак"))

    @patch("main.requests.get")
    def test_fetch_next_game_no_games(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_get.return_value = mock_resp

        game_id, text = main.fetch_next_game("Неизвестная Команда")
        self.assertIsNone(game_id)
        self.assertIn("Пока нет открытых назначенных игр", text)

    def test_fetch_next_game_empty_team_name(self):
        # Пустое название команды не должно матчить случайные игры
        game_id, text = main.fetch_next_game("")
        self.assertIsNone(game_id)
        self.assertIn("укажите название команды", text)

    @patch("main.requests.get")
    def test_fetch_next_game_network_error(self, mock_get):
        mock_get.side_effect = Exception("Connection timeout")
        game_id, text = main.fetch_next_game("БК Путилково")
        self.assertIsNone(game_id)
        self.assertIn("Не удалось получить данные о расписании ABL", text)

    @patch("main.requests.get")
    def test_fetch_schedule_success(self, mock_get):
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

    def test_fetch_schedule_empty_team_name(self):
        text = main.fetch_schedule("")
        self.assertIn("укажите название команды", text)

    @patch("main.requests.get")
    def test_fetch_schedule_missing_datetime(self, mock_get):
        # Игра без объявленной даты должна выводить "Дата уточняется", а не 0001 год
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {
                "id": 103,
                "status": "open",
                "team": {"name": "БК Путилково"},
                "competitor_team": {"name": "Соперник 3"},
                "datetime": None
            }
        ]
        mock_get.return_value = mock_resp

        text = main.fetch_schedule("БК Путилково")
        self.assertIn("Дата уточняется", text)
        self.assertNotIn("01.01 в 03:00", text)

    # --- 2. Command handling (/start, /help, /game, /schedule, etc.) ---

    @patch("main.send_msg")
    def test_command_start_help_private(self, mock_send):
        for cmd in ["/start", "/help"]:
            msg = {
                "text": cmd,
                "chat": {"id": 12345, "type": "private"},
                "from": {"id": 12345}
            }
            main.process_message(msg)
            self.assertIn("Привет!", mock_send.call_args[0][1])

    @patch("main.send_msg")
    def test_command_start_help_group_configured_and_unconfigured(self, mock_send):
        # 1. Ненастроенная группа
        msg = {
            "text": "/start",
            "chat": {"id": -100888, "type": "group"},
            "from": {"id": 123}
        }
        main.process_message(msg)
        self.assertIn("пока не настроена отдельная команда", mock_send.call_args[0][1])

        # 2. Настроенная группа
        self.mock_config["-100888"] = {"team": "Химки", "division": "Див 1"}
        main.process_message(msg)
        self.assertIn("Этот чат настроен на команду: *Химки*", mock_send.call_args[0][1])

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
    def test_command_game_with_pipe_division(self, mock_fetch, mock_send):
        mock_fetch.return_value = (999, "Игра Спартак Дивизион 1")
        msg = {
            "text": "/game Спартак | Дивизион 1",
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345}
        }
        main.process_message(msg)
        mock_fetch.assert_called_with("Спартак", "Дивизион 1")

    @patch("main.send_msg")
    @patch("main.fetch_next_game")
    def test_command_game_with_empty_pipe(self, mock_fetch, mock_send):
        # /game | должен безопасно использовать команду по умолчанию
        mock_fetch.return_value = (999, "Игра БК Путилково")
        msg = {
            "text": "/game |",
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345}
        }
        main.process_message(msg)
        mock_fetch.assert_called_with("БК Путилково", "")

    @patch("main.send_msg")
    @patch("main.fetch_next_game")
    def test_command_game_with_bot_mention(self, mock_fetch, mock_send):
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
        mock_send.assert_called_with("-100555", "⛔️ Только администратор этой группы может настраивать или менять команду.")
        mock_delete.assert_called_once_with("-100555", [555, 444], delay=1.5)
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
    def test_sender_chat_none_does_not_crash(self, mock_send):
        # Критичный тест: sender_chat: None не должен вызывать AttributeError
        msg = {
            "message_id": 100,
            "text": "/game",
            "chat": {"id": -100555, "type": "supergroup"},
            "sender_chat": None,
            "from": {"id": 123}
        }
        main.process_message(msg)
        mock_send.assert_called_once()

    @patch("main.send_msg")
    def test_anonymous_admin_allowed_in_group(self, mock_send):
        # Анонимный администратор шлет от имени чата (sender_chat.id == chat.id)
        msg = {
            "message_id": 101,
            "text": "/set Рубин",
            "chat": {"id": -100555, "type": "supergroup"},
            "sender_chat": {"id": -100555},
            "from": None
        }
        main.process_message(msg)
        self.assertIn("-100555", self.mock_config)
        self.assertEqual(self.mock_config["-100555"]["team"], "Рубин")

    @patch("main.send_msg")
    def test_channel_post_allowed(self, mock_send):
        # В канале сообщения отправляют только администраторы
        msg = {
            "message_id": 102,
            "text": "/set Динамо",
            "chat": {"id": -100777, "type": "channel"},
            "from": None
        }
        main.process_message(msg)
        self.assertIn("-100777", self.mock_config)
        self.assertEqual(self.mock_config["-100777"]["team"], "Динамо")

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

    # --- 5. Message deletion logic tests (delete_msg & delete_later) ---

    @patch("main.requests.post")
    def test_delete_msg_success_and_failure(self, mock_post):
        mock_resp_ok = MagicMock()
        mock_resp_ok.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp_ok

        main.delete_msg("12345", 555)
        mock_post.assert_called_with(
            f"https://api.telegram.org/bot{main.TOKEN}/deleteMessage",
            json={"chat_id": "12345", "message_id": 555},
            timeout=5
        )

        # Проверка безопасного перехвата сетевой ошибки
        mock_post.side_effect = Exception("Network error")
        try:
            main.delete_msg("12345", 555)
        except Exception:
            self.fail("delete_msg threw an uncaught exception on network error")

    @patch("main.delete_msg")
    @patch("main.requests.post")
    def test_delete_later_batch_success(self, mock_post, mock_del_msg):
        # deleteMessages успешен -> поштучное удаление не вызывается
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        # Тестируем worker напрямую без задержки
        with patch("main.threading.Thread") as mock_thread:
            main.delete_later("12345", [10, 20, "30", None], delay=0)
            mock_thread.assert_called_once()
            worker = mock_thread.call_args[1]["target"]
            worker()

        mock_post.assert_called_once_with(
            f"https://api.telegram.org/bot{main.TOKEN}/deleteMessages",
            json={"chat_id": "12345", "message_ids": [10, 20, 30]},
            timeout=5
        )
        mock_del_msg.assert_not_called()

    @patch("main.delete_msg")
    @patch("main.requests.post")
    def test_delete_later_fallback_to_individual(self, mock_post, mock_del_msg):
        # deleteMessages вернул ok: False -> фолбэк на delete_msg для каждого id
        mock_resp_fail = MagicMock()
        mock_resp_fail.json.return_value = {"ok": False, "description": "Not allowed"}
        mock_post.return_value = mock_resp_fail

        with patch("main.threading.Thread") as mock_thread:
            with patch("time.sleep"):
                main.delete_later("12345", [10, 20], delay=0)
                worker = mock_thread.call_args[1]["target"]
                worker()

        self.assertEqual(mock_del_msg.call_count, 2)
        mock_del_msg.assert_any_call("12345", 10)
        mock_del_msg.assert_any_call("12345", 20)

    # --- 6. Edge cases & Markdown fallback ---

    @patch("main.send_msg")
    def test_just_slash_and_unknown_command(self, mock_send):
        for raw in ["/", "/   ", "/unknown_command", "/xyz 123"]:
            msg = {
                "text": raw,
                "chat": {"id": 12345, "type": "private"},
                "from": {"id": 12345}
            }
            main.process_message(msg)
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
        mock_resp_fail = MagicMock()
        mock_resp_fail.json.return_value = {"ok": False, "description": "Bad Request: can't parse entities"}
        mock_resp_ok = MagicMock()
        mock_resp_ok.json.return_value = {"ok": True, "result": {"message_id": 9999}}
        mock_post.side_effect = [mock_resp_fail, mock_resp_ok]

        mid = main.send_msg("12345", "Текст с *незакрытой звездочкой")
        self.assertEqual(mid, 9999)
        self.assertEqual(mock_post.call_count, 2)
        self.assertNotIn("parse_mode", mock_post.call_args_list[1][1]["json"])

if __name__ == "__main__":
    unittest.main()
