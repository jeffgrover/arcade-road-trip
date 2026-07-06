import unittest

from arcade_query import (
    QueryResult,
    fuzzy_score,
    normalize_argv,
    render,
    require_readonly_sql,
)


class ArcadeQueryTests(unittest.TestCase):
    def test_normalize_argv_moves_global_flags_before_subcommand(self):
        self.assertEqual(
            normalize_argv(["games", "Godzilla", "--format", "json", "--db", "arcades.sqlite"]),
            ["--format", "json", "--db", "arcades.sqlite", "games", "Godzilla"],
        )

    def test_normalize_argv_moves_global_boolean_flags(self):
        self.assertEqual(
            normalize_argv(["rare", "--state", "UT", "--include-inactive", "--format", "json"]),
            ["--include-inactive", "--format", "json", "rare", "--state", "UT"],
        )

    def test_normalize_argv_moves_lazy_verification_flags(self):
        self.assertEqual(
            normalize_argv(
                [
                    "locations",
                    "Quarters",
                    "--verify-missing",
                    "--verify-stale-days",
                    "30",
                    "--verify-limit",
                    "3",
                ]
            ),
            [
                "--verify-missing",
                "--verify-stale-days",
                "30",
                "--verify-limit",
                "3",
                "locations",
                "Quarters",
            ],
        )

    def test_require_readonly_sql_rejects_writes(self):
        with self.assertRaises(ValueError):
            require_readonly_sql("DELETE FROM locations")

    def test_fuzzy_score_prefers_specific_candidate(self):
        query = "Star Wars Comic Art"

        self.assertGreater(
            fuzzy_score(query, "Star Wars (Comic Art Premium)"),
            fuzzy_score(query, "Star Wars"),
        )

    def test_render_markdown_table(self):
        result = QueryResult(
            title="Tiny Result",
            columns=["name", "count"],
            rows=[{"name": "Quarters", "count": 19}],
            notes=["sample note"],
        )

        output = render(result, "markdown")

        self.assertIn("## Tiny Result", output)
        self.assertIn("sample note", output)
        self.assertIn("| Quarters", output)


if __name__ == "__main__":
    unittest.main()
