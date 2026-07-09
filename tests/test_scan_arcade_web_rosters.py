import unittest

import duckdb

from scan_arcade_web_rosters import (
    PageSummaryParser,
    RosterPageResult,
    compare_roster_to_database,
    extract_machine_name_candidates,
    extract_pinside_machine_names,
    is_internal_url,
    should_follow_roster_url,
    load_candidates,
    load_location_game_names,
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
        self.assertIn("Current games", parser.text)

    def test_is_internal_url_normalizes_www_prefix(self):
        self.assertTrue(is_internal_url("https://www.example.test", "https://example.test/games"))
        self.assertFalse(is_internal_url("https://example.test", "https://elsewhere.test/games"))

    def test_should_follow_roster_url_allows_trusted_external_hosts(self):
        self.assertTrue(
            should_follow_roster_url(
                "https://pasttimesarcade.com",
                "https://pinside.com/pinball/map/where-to-play/17578-past-times-arcade-girard-oh",
                True,
            )
        )
        self.assertFalse(
            should_follow_roster_url(
                "https://pasttimesarcade.com",
                "https://pinside.com/pinball/map/where-to-play/17578-past-times-arcade-girard-oh",
                False,
            )
        )
        self.assertFalse(
            should_follow_roster_url(
                "https://pasttimesarcade.com",
                "https://www.arcade-museum.com/members/detail/Past-Times-Arcade-504534",
                True,
            )
        )

    def test_extract_machine_name_candidates_filters_page_chrome(self):
        names = extract_machine_name_candidates(
            """
            Hours and Admission
            Medieval Madness
            The Addams Family (working)
            Contact us
            Attack from Mars - out of order
            1942
            1. Galaga
            https://example.test/games
            """
        )

        self.assertEqual(names, ["Medieval Madness", "The Addams Family", "Attack from Mars", "1942", "Galaga"])

    def test_extract_pinside_machine_names_uses_games_list_marker(self):
        names = extract_pinside_machine_names(
            """
            Header
            There are are 3 games listed for this location.
            "300"
            EM Gottlieb, 1975 - Added on 2023-05-25
            Machine: Bride of Pinbot, The
            SS Williams, 1991 - Added on 2024-08-31
            Medieval Madness
            SS Williams, 1997 - Added on 2023-05-25
            Photos
            Not A Game
            Contact us
            """
        )

        self.assertEqual(names, ["300", "Bride of Pinbot, The", "Medieval Madness"])

    def test_compare_roster_to_database_reports_matches_and_gaps(self):
        page = RosterPageResult(
            source_text="Games",
            source_url="https://example.test/games",
            source_score=4,
            ok=True,
            final_url="https://example.test/games",
            status_code=200,
            content_type="text/html",
            title="Game List",
            roster_score=8,
            extracted_names=["Medieval Madness", "Attack from Mars", "Atarians, The", "Website Only Game"],
            cache_path="/tmp/example.html",
            error="",
        )

        comparison = compare_roster_to_database(
            ["Medieval Madness", "Attack from Mars", "The Atarians", "Missing DB Game"],
            [page],
        )

        self.assertEqual(comparison.matched_db_games, ["Medieval Madness", "Attack from Mars", "The Atarians"])
        self.assertEqual(comparison.missing_db_games, ["Missing DB Game"])
        self.assertEqual(comparison.website_only_names, ["Website Only Game"])

    def test_compare_roster_to_database_does_not_infer_missing_without_pages(self):
        comparison = compare_roster_to_database(["Medieval Madness"], [])

        self.assertEqual(comparison.db_game_count, 1)
        self.assertEqual(comparison.roster_page_count, 0)
        self.assertEqual(comparison.missing_db_games, [])

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

    def test_load_location_game_names_groups_names_by_location(self):
        conn = duckdb.connect(":memory:")
        try:
            conn.execute("CREATE TABLE games(game_id BIGINT, name VARCHAR)")
            conn.execute("CREATE TABLE location_games(location_id BIGINT, game_id BIGINT)")
            conn.execute("INSERT INTO games VALUES (10, 'Medieval Madness'), (11, 'Attack from Mars')")
            conn.execute("INSERT INTO location_games VALUES (1, 10), (1, 10), (1, 11), (2, 10)")

            names = load_location_game_names(conn, [1, 2, 3])

            self.assertEqual(names[1], ["Attack from Mars", "Medieval Madness"])
            self.assertEqual(names[2], ["Medieval Madness"])
            self.assertEqual(names[3], [])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
