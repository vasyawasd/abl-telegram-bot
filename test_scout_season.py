import unittest
from copy import deepcopy

import main
from test_scout import player


class TestScoutSeasonPlayers(unittest.TestCase):
    def setUp(self):
        self.team = {"id": 4097, "name": "BOOMS"}
        self.season = {"id": 231, "name": "2026/27", "current_tournament_id": 4363}

    def test_qualification_and_main_phase_merge_by_player_not_jersey(self):
        qualification = player(11, "Иван", 4, 40, 16, 8, 3, 10)
        qualification["player_efficiency"] = 52
        qualification["tournament_team_user"]["league_player"]["id"] = 777
        qualification["tournament_team_user"]["tournament_team"]["tournament_id"] = 4152
        regular = deepcopy(qualification)
        qualification["tournament_team_user"]["active"] = False
        regular.update(games_count=2, points=30, rebounds=10, assists=4,
                       three_points_made=2, three_point_attempts=8, player_efficiency=20)
        regular["tournament_team_user"]["team_user"]["number"] = 0
        regular["tournament_team_user"]["tournament_team"]["tournament_id"] = 4363
        # Порядок ответа не определяет номер игрока; нужного старого турнира
        # может уже не быть в актуальной заявке команды.
        players = main._scout_players(self.team, self.season, [regular, qualification])
        self.assertEqual(len(players), 1)
        p = players[0]
        self.assertEqual(p["games"], 6)
        self.assertEqual(p["totals"]["points"], 70)
        self.assertAlmostEqual(p["points"], 70 / 6)
        self.assertEqual(p["player_efficiency"], 12)
        self.assertEqual(p["totals"]["three_points_made"], 5)
        self.assertEqual(p["totals"]["three_point_attempts"], 18)
        self.assertTrue(p["label"].startswith("№0 "))

    def test_deleted_player_is_absent_from_cards_and_top_five(self):
        actual = player(0, "Настоящий", 4, 40)
        deleted = player(47, "Михаил", 1, 5, 5, 6, 1, 5)
        deleted["tournament_team_user"]["active"] = False
        deleted["tournament_team_user"]["league_player"].update(id=27106, last_name="Василевич")
        rows = [deleted, actual]
        players = main._scout_players(self.team, self.season, rows)
        self.assertEqual(len(players), 1)
        self.assertIn("Настоящий", players[0]["label"])
        text = main.format_scout_report(self.team, self.season, rows)
        self.assertNotIn("Василевич", text)
        self.assertNotIn("№47", text)
        self.assertIn("Настоящий", text.split("ТОП-5", 1)[1])

    def test_inactive_phase_still_counts_for_an_active_player_with_no_new_games(self):
        previous = player(11, "Иван", 4, 40)
        previous["tournament_team_user"]["active"] = False
        previous["tournament_team_user"]["tournament_team"]["tournament_id"] = 4152
        current = player(0, "Иван", 0, 0)
        current["tournament_team_user"]["league_player"]["id"] = 11
        players = main._scout_players(self.team, self.season, [previous, current])
        self.assertEqual(len(players), 1)
        self.assertEqual(players[0]["games"], 4)
        self.assertEqual(players[0]["points"], 10)
        self.assertTrue(players[0]["label"].startswith("№0 "))
        # В API статистики новая заявка без матчей может вообще отсутствовать.
        self.season["active_player_ids"] = {11}
        players = main._scout_players(self.team, self.season, [previous])
        self.assertEqual(len(players), 1)
        self.assertEqual(players[0]["games"], 4)

    def test_same_name_and_number_do_not_merge_different_people(self):
        first = player(4, "Иван", 2, 20)
        second = deepcopy(first)
        second["tournament_team_user"]["league_player"]["id"] = 999
        self.assertEqual(len(main._scout_players(self.team, self.season, [first, second])), 2)

    def test_negative_efficiency_is_valid_and_nonfinite_totals_are_rejected(self):
        row = player(4, "Иван", 2, 4)
        row["player_efficiency"] = -6
        self.assertEqual(main._scout_players(self.team, self.season, [row])[0]["player_efficiency"], -3)
        row["player_efficiency"] = float("nan")
        with self.assertRaises(ValueError):
            main._scout_players(self.team, self.season, [row])


if __name__ == "__main__":
    unittest.main()
