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
