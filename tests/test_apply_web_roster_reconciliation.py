import unittest

from apply_web_roster_reconciliation import GameMatch, resolve_game, similarity


class ApplyWebRosterReconciliationTests(unittest.TestCase):
    def test_similarity_prefers_base_title_over_parenthetical_edition(self):
        self.assertGreater(
            similarity("Addams Family", "Addams Family, The")[0],
            similarity("Addams Family", "Addams Family (Special Collector's Edition)")[0],
        )

    def test_similarity_keeps_remake_qualifier(self):
        self.assertEqual(similarity("Medieval Madness (Remake)", "Medieval Madness (CGC)")[1], "remake_compatible")

    def test_resolve_game_uses_manual_resolution_for_ambiguous_title(self):
        games = [
            GameMatch(2340, "Rapid Fire", "Hanaho", 4, 0.0, ""),
            GameMatch(2393, "Rapid Fire", "Bally", 2, 0.0, ""),
        ]

        match = resolve_game(games, "Rapid Fire")

        self.assertIsNotNone(match)
        self.assertEqual(match.game_id, 2393)


if __name__ == "__main__":
    unittest.main()
