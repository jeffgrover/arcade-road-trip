import unittest

import duckdb

from arcade_query import (
    QueryResult,
    fuzzy_score,
    normalize_argv,
    rare_games,
    render,
    require_readonly_sql,
    search_games,
    where_to_play,
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

    def test_canonical_game_links_collapse_counts(self):
        conn = duckdb.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE games (
                game_id BIGINT,
                name VARCHAR NOT NULL,
                manufacturer VARCHAR
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE locations (
                location_id BIGINT,
                name VARCHAR NOT NULL,
                city VARCHAR,
                state VARCHAR,
                street_address VARCHAR,
                source_url VARCHAR
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE location_games (
                location_id BIGINT NOT NULL,
                game_id BIGINT NOT NULL,
                cabinet_type VARCHAR,
                year INTEGER
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE location_statuses (
                location_id BIGINT,
                status VARCHAR,
                replacement_name VARCHAR
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE game_canonical_links (
                alias_game_id BIGINT,
                canonical_game_id BIGINT NOT NULL,
                confidence REAL NOT NULL,
                reason VARCHAR NOT NULL,
                source VARCHAR NOT NULL DEFAULT 'auto',
                updated_at VARCHAR
            );
            """
        )
        conn.execute(
            """
            INSERT INTO games(game_id, name, manufacturer) VALUES
                (1, 'Tales of the Arabian Nights', 'Williams'),
                (-2000001364, 'Tales of the Arabian Nights', 'Williams');
            """
        )
        conn.execute(
            """
            INSERT INTO locations(location_id, name, city, state) VALUES
                (10, 'Arcade One', 'Sandy', 'UT'),
                (11, 'Arcade Two', 'Ogden', 'UT');
            """
        )
        conn.execute(
            """
            INSERT INTO location_games(location_id, game_id, cabinet_type) VALUES
                (10, 1, 'Pinball'),
                (11, -2000001364, 'Pinball');
            """
        )
        conn.execute(
            """
            INSERT INTO game_canonical_links(
                alias_game_id, canonical_game_id, confidence, reason, source
            ) VALUES (
                -2000001364, 1, 1.0, 'exact_compact_title', 'auto'
            );
            """
        )

        rare = rare_games(conn, "UT", max_locations=1, limit=10)
        search = search_games(conn, "Arabian Nights", limit=10)
        where = where_to_play(conn, "Arabian Nights", state="UT", limit=10)

        self.assertEqual([], rare.rows)
        self.assertEqual(1, len(search.rows))
        self.assertEqual(2, search.rows[0]["location_count"])
        self.assertEqual(2, len(where.rows))


if __name__ == "__main__":
    unittest.main()
