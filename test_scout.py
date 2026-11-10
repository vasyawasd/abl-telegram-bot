import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

os.environ["BOT_TOKEN"] = "test-token"
import main


def player(number, name, games, points, rebounds=0, assists=0, made=0, attempts=0):
    return {"team_id": 4097, "games_count": games, "points": points,
            "rebounds": rebounds, "assists": assists, "steals": 0, "blocks": 0,
            "three_points_made": made, "three_point_attempts": attempts, "player_efficiency": points,
            "tournament_team_user": {"active": True, "league_player": {"id": number if number is not None else 999, "first_name": name, "last_name": "Игрок"},
                                     "team_user": {"number": number},
                                     "tournament_team": {"tournament_id": 4363}}}


class TestScout(unittest.TestCase):
    def setUp(self):
        self.game = {"id": 168257, "status": "open",
                     "datetime": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat(),
                     "team": {"id": 4097, "name": "BOOMS"},
                     "competitor_team": {"id": 8329, "name": "БК Путилково"},
                     "tournament": {"id": 4363, "name": "BASE"}}
        self.rows = [player(4, "Андрей", 3, 30, 15, 6, 3, 12),
                     player(0, "Вячеслав", 2, 40, 28, 4, 0, 0)]
        self.fetch = patch("main.fetch_raw_abl_games", return_value=[self.game]).start()
        self.post = patch("main.http_post").start()
        self.post.return_value = self.response(self.rows)
        self.season = {"id": 231, "name": "Сезон 2026/27"}
        self.memberships = [{"id": 34234, "tournament": {**self.game["tournament"], "date": "2026-10-31", "season": self.season}}]
        def lookup(url, headers):
            if "/users/" in url:
                return [{"active": True, "league_player": {"id": 0}},
                        {"active": True, "league_player": {"id": 4}}], {}
            if "tournament_seasons/" in url:
                return [self.season], {}
            if "/teams/" in url:
                return [{"team": self.game["team"], "tournament_id": 4363}], {}
            return self.memberships, {}
        self.lookup = patch("main._fetch_abl_page", side_effect=lookup).start()
        self.addCleanup(patch.stopall)

    @staticmethod
    def response(rows, count=None):
        response = MagicMock(status_code=200, headers={"X-Page-Count": str(len(rows) if count is None else count)})
        response.json.return_value = rows
        return response

    def test_auto_scout_selects_opponent_and_calculates_per_game(self):
        text = main.fetch_scout("БК Путилково")
        payload = self.post.call_args.kwargs["json"]
        self.assertEqual(payload["team_id"], 4097)
        self.assertEqual(payload["tournament_season_ids"], "231")
        self.assertNotIn("tournament_ids", payload)
        self.assertEqual(payload["per_game"], "0")
        self.assertEqual(payload["only_closed"], "1")
        self.assertIn("BOOMS", text)
        self.assertIn("2026/27", text)
        self.assertIn("№0", text)
        self.assertIn("20,0", text)
        self.assertIn("14,0", text)
        self.assertIn("3/12", text)
        self.assertIn("25%", text)
        self.assertLess(text.index("Вячеслав"), text.index("Андрей"))
        self.assertIn("Матчей", text)

    def test_home_team_also_gets_opponent(self):
        self.game["team"], self.game["competitor_team"] = self.game["competitor_team"], self.game["team"]
        main.fetch_scout("БК Путилково")
        self.assertEqual(self.post.call_args.kwargs["json"]["team_id"], 4097)

    def test_current_roster_keeps_real_player_without_new_phase_stats(self):
        previous = player(0, "Действующий", 4, 40)
        previous["tournament_team_user"]["active"] = False
        previous["tournament_team_user"]["tournament_team"]["tournament_id"] = 4152
        deleted = player(47, "Чужой", 1, 5)
        deleted["tournament_team_user"]["active"] = False
        self.post.return_value = self.response([previous, deleted])
        text = main.fetch_scout("БК Путилково")
        self.assertIn("Действующий", text)
        self.assertNotIn("Чужой", text)
        self.assertTrue(any("/tournament_team/34234/users/" in call.args[0]
                            for call in self.lookup.call_args_list))

    def test_explicit_team_is_target_not_its_opponent(self):
        self.fetch.return_value = None  # Прямая команда работает и без общего расписания.
        text = main.fetch_scout("BOOMS", direct=True)
        self.assertEqual(self.post.call_args.kwargs["json"]["team_id"], 4097)
        self.assertIn("BOOMS", text)
        self.fetch.assert_not_called()

    def test_transliteration_prefers_exact_mens_team_over_women(self):
        men = {"id": 18417, "name": "HDXСЕВЕР"}
        women = {"id": 14352, "name": "HDxSEVER WOMEN"}
        self.lookup.side_effect = [
            ([self.season], {}),
            ([{"team": women, "name": "HDxSEVER", "tournament_id": 4000},
              {"team": men, "name": "HD X Север", "tournament_id": 4371}], {}),
            ([{"id": 111, "tournament": {"id": 4152, "name": "Квалификация", "date": "2026-09-01"}},
              {"id": 222, "tournament": {"id": 4371, "name": "PURE LOVE", "date": "2026-10-31"}}], {}),
        ]
        self.post.return_value = self.response([])
        text = main.fetch_scout("HDXSEVER", direct=True)
        payload = self.post.call_args.kwargs["json"]
        self.assertEqual((payload["team_id"], payload["tournament_season_ids"]), (18417, "231"))
        self.assertIn("2026/27", text)

    def test_ambiguous_direct_name_requests_more_precise_name(self):
        self.lookup.side_effect = [
            ([{"id": 231}], {}),
            ([{"team": {"id": n, "name": name}, "tournament_id": 4363}
              for n, name in [(1, "Север Один"), (2, "Север Два")]], {}),
        ]
        text = main.fetch_scout("Север", direct=True)
        self.assertIn("несколько команд", text)
        self.post.assert_not_called()

    def test_no_game_and_network_error_do_not_query_statistics(self):
        self.fetch.return_value = []
        self.assertIn("нет", main.fetch_scout("БК Путилково").lower())
        self.fetch.return_value = None
        self.assertIn("Не удалось", main.fetch_scout("БК Путилково"))
        self.post.assert_not_called()

    def test_empty_stats_and_missing_number_are_explicit(self):
        self.post.return_value = self.response([])
        self.assertIn("нет", main.fetch_scout("БК Путилково").lower())
        row = player(None, "Новичок", 1, 12)
        self.post.return_value = self.response([row])
        text = main.fetch_scout("БК Путилково")
        self.assertIn("№?", text)
        self.assertIn("Мало данных", text)
        self.assertNotIn("nan", text)

    def test_pagination_reads_all_rows(self):
        self.post.side_effect = [self.response(self.rows[:1], 2), self.response(self.rows[1:], 2),
                                 self.response([{"team_id": 4097, "games_count": 3}])]
        text = main.fetch_scout("БК Путилково")
        self.assertIn("Андрей", text)
        self.assertIn("Вячеслав", text)
        self.assertEqual([call.kwargs["json"]["page"] for call in self.post.call_args_list], [1, 2, 1])

    def test_attendance_is_season_appearances_divided_by_team_games(self):
        # Два игрока сыграли 3 и 2 матча; команда сыграла 4: среднее 5/4=1,25.
        self.post.side_effect = [self.response(self.rows),
                                 self.response([{"team_id": 4097, "games_count": 4}])]
        text = main.fetch_scout("БК Путилково")
        self.assertIn("Доезд: *1,2*", text)
        self.assertIn("Матчей *4*", text)
        team_payload = self.post.call_args.kwargs["json"]
        self.assertEqual(team_payload["group_by"], "team")
        self.assertEqual(team_payload["tournament_season_ids"], "231")
        self.assertEqual(team_payload["only_closed"], "1")

    def test_attendance_failure_does_not_hide_available_player_stats(self):
        import requests
        self.post.side_effect = [self.response(self.rows), requests.ConnectionError("down")]
        text = main.fetch_scout("БК Путилково")
        self.assertIn("Доезд: нет данных", text)
        self.assertIn("Вячеслав", text)
        self.assertIn("ТОП-5", text)

    def test_bad_or_foreign_stats_are_not_reported_as_our_team(self):
        for data in ({"error": "unavailable"}, [dict(self.rows[0], team_id=999)],
                     [dict(self.rows[0], points="broken")],
                     [dict(self.rows[0], tournament_team_user={
                         **self.rows[0]["tournament_team_user"], "league_player": ["broken"]})]):
            with self.subTest(data=data):
                self.post.return_value = self.response(data)
                text = main.fetch_scout("БК Путилково")
                self.assertIn("Не удалось", text)

    def test_report_stays_within_telegram_limit(self):
        self.post.return_value = self.response([player(n, "ДлинноеИмя" * 20, 3, n) for n in range(30)])
        self.assertLess(len(main.fetch_scout("БК Путилково")), 4096)


if __name__ == "__main__":
    unittest.main()
