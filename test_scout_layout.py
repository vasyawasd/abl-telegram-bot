import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

os.environ["BOT_TOKEN"] = "test-token"
import main


def normalized_player(number, name, points, efficiency, games=4):
    averages = {"points": points, "rebounds": 5.0, "assists": 3.5,
                "steals": 1.0, "blocks": 0.5, "three_points_made": 2.0,
                "three_point_attempts": 6.5, "player_efficiency": efficiency}
    return {"label": f"№{number} {name}", "games": games,
            "totals": {key: value * games for key, value in averages.items()}, **averages}


class TestScoutLayout(unittest.TestCase):
    team = {"id": 4097, "name": "BOOMS"}
    season = {"id": 231, "name": "Осень 2026", "tournament_ids": [4152, 4363],
              "current_tournament_id": 4363}

    def report(self, players, **kwargs):
        with patch("main._scout_players", return_value=players, create=True):
            return main.format_scout_report(self.team, self.season, [], **kwargs)

    def test_season_header_and_short_cards_keep_zero_jersey(self):
        players = [normalized_player(7, "Игрок Второй", 10, 8),
                   normalized_player(0, "Игрок Первый", 14, 12)]
        text = self.report(players, game_date=datetime(2026, 11, 15, 12, tzinfo=timezone.utc))
        header, cards = text.split("\n\n", 1)
        self.assertIn("BOOMS", header)
        self.assertIn("Сезон: Осень 2026", header)
        self.assertNotIn("квалификация", header)
        self.assertIn("15.11.2026 в 15:00", header)
        self.assertIn("Доезд: нет данных", header)
        self.assertIn("*№0 Игрок Первый*\nОчки *14,0* · Подборы *5,0*\n"
                      "Передачи *3,5* · Перехваты *1,0*\nБлоки *0,5* · Матчей *4*\n"
                      "🎯 3-очковые: 8/26 · 31%", cards)
        self.assertIn("31%\n\n*№7 Игрок Второй*", cards)
        self.assertNotIn("**", text)
        self.assertNotIn("Показатели в среднем", text)
        self.assertNotIn("3-очковые — всего", text)
        self.assertNotIn("Акценты по статистике", text)

    def test_top_five_uses_efficiency_of_all_players_beyond_eight_cards(self):
        players = [normalized_player(n, f"Игрок {n}", 20 - n, n) for n in range(10)]
        players[9]["player_efficiency"] = 40
        text = self.report(players)
        cards, footer = text.split("*ТОП-5 игроков по КП*", 1)
        self.assertEqual(cards.count("🎯 3-очковые:"), 8)
        self.assertNotIn("№9 Игрок 9", cards)
        self.assertLess(cards.index("№0 Игрок 0"), cards.index("№7 Игрок 7"))
        self.assertIn("1. *№9 Игрок 9*\nОчки *11,0* · Подборы *5,0*\n"
                      "Передачи *3,5* · КП *40,0*", footer)
        ranking = [line for line in footer.splitlines() if line[:1].isdigit()]
        self.assertEqual([line.split(" ", 1)[1] for line in ranking],
                         [f"*№{n} Игрок {n}*" for n in (9, 8, 7, 6, 5)])
        self.assertNotIn("Перехваты", footer)
        self.assertNotIn("🎯", footer)

    def test_small_sample_warning_precedes_final_ranking_and_negative_efficiency(self):
        players = [normalized_player(0, "Первый", 4, -2.5, games=1),
                   normalized_player(8, "Второй", 2, -1, games=2)]
        text = self.report(players)
        self.assertLess(text.index("Мало данных"), text.index("*ТОП-5 игроков по КП*"))
        footer = text.split("*ТОП-5 игроков по КП*", 1)[1]
        self.assertIn("1. *№8 Второй*", footer)
        self.assertIn("2. *№0 Первый*", footer)
        self.assertNotIn("3.", footer)
        self.assertTrue(footer.endswith("Передачи *3,5* · КП *-2,5*"))

    def test_attendance_and_empty_season_are_explicit(self):
        text = self.report([], attendance={"average": 8.5, "games": 4})
        self.assertIn("Доезд: *8,5* игроков/матч · Матчей *4*", text)
        self.assertIn("нет индивидуальной статистики", text)
        self.assertIn("сезона", text)
        self.assertNotIn("ТОП-5", text)

    def test_realistic_long_names_fit_telegram_utf16_limit(self):
        players = [normalized_player(n, ("ДлиннаяФамилия" * 5)[:60], 123.5 - n, 100 - n)
                   for n in range(30)]
        with patch.dict(self.team, name="Команда" * 14), patch.dict(self.season, name="Сезон" * 20):
            text = self.report(players, attendance={"average": 12.5, "games": 22},
                               game_date=datetime(2026, 11, 15, 12, tzinfo=timezone.utc))
        self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 4096)
        self.assertEqual(text.count("🎯 3-очковые:"), 8)
        self.assertEqual(len(text.split("*ТОП-5 игроков по КП*", 1)[1].strip().split("\n\n")), 5)


if __name__ == "__main__":
    unittest.main()
