import unittest

import duckdb

from scan_arcade_web_rosters import (
    PageSummaryParser,
    load_candidates,
    normalize_url,
    score_links,
)


class WebRosterReporterTests(unittest.TestCase):
    def test_normalize_url_adds_https_when_missing(self):
        self.assertEqual(normalize_url("example.test/games"), "https://example.test/games")
        self.assertEqual(normalize_url("http://example.test"), "http://example.test")

    def test_score_links_finds_rosterish_links(self):
        links = [
            ("Contact", "https://example.test/contact"),
            ("Current Pinball Lineup", "https://example.test/games"),
            ("Food Menu", "https://example.test/menu"),
            ("Arcade Machines", "https://example.test/collection"),
        ]

        hints = score_links(links)

        self.assertEqual({hint.text for hint in hints}, {"Current Pinball Lineup", "Arcade Machines"})
        self.assertGreaterEqual(hints[0].score, hints[1].score)

    def test_score_links_ignores_social_sites(self):
        links = [
            ("Arcade Instagram", "https://www.instagram.com/examplearcade/"),
            ("View Games", "https://example.test/games"),
        ]

        hints = score_links(links)

        self.assertEqual([hint.text for hint in hints], ["View Games"])

    def test_page_summary_parser_collects_title_and_links(self):
        parser = PageSummaryParser("https://example.test")
        parser.feed(
            """
            <html>
              <head><title>Example Arcade</title></head>
              <body><a href="/games">Current games</a></body>
            </html>
            """
        )

        self.assertEqual(parser.title, "Example Arcade")
        self.assertEqual(parser.links, [("Current games", "https://example.test/games")])

    def test_load_candidates_prefers_large_active_locations_with_websites(self):
        conn = duckdb.connect(":memory:")
        try:
            conn.execute(
                """
                CREATE TABLE locations (
                    location_id BIGINT,
                    name VARCHAR,
                    city VARCHAR,
                    state VARCHAR,
                    game_count INTEGER,
                    website_url VARCHAR,
                    google_place_id VARCHAR
                )
                """
            )
            conn.execute("CREATE TABLE location_statuses(location_id BIGINT, status VARCHAR)")
            conn.execute("CREATE TABLE location_games(location_id BIGINT, game_id BIGINT)")
            conn.execute(
                """
                INSERT INTO locations VALUES
                    (1, 'Big Open Arcade', 'Orlando', 'FL', 2, 'big.example', 'PlaceBig'),
                    (2, 'Small Open Arcade', 'Orlando', 'FL', 1, 'small.example', 'PlaceSmall'),
                    (3, 'Big Closed Arcade', 'Orlando', 'FL', 2, 'closed.example', 'PlaceClosed'),
                    (4, 'Big Missing Website', 'Orlando', 'FL', 2, '', 'PlaceMissing'),
                    (5, 'International Arcade', 'Rotterdam', 'NL', 2, 'nl.example', 'PlaceNL')
                """
            )
            conn.execute(
                """
                INSERT INTO location_games VALUES
                    (1, 10), (1, 11),
                    (2, 10),
                    (3, 10), (3, 11),
                    (4, 10), (4, 11),
                    (5, 10), (5, 11)
                """
            )
            conn.execute(
                """
                INSERT INTO location_statuses VALUES
                    (1, 'active'),
                    (2, 'active'),
                    (3, 'closed'),
                    (4, 'active'),
                    (5, 'active')
                """
            )

            candidates = load_candidates(conn, limit=10, min_game_count=2)

            self.assertEqual([candidate.location_id for candidate in candidates], [1])
            self.assertEqual(candidates[0].website_url, "big.example")
            self.assertEqual(candidates[0].google_place_id, "PlaceBig")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
