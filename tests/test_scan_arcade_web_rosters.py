import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import duckdb

from scan_arcade_web_rosters import (
    Candidate,
    LinkHint,
    PageSummaryParser,
    PageFetchResult,
    ProbeResult,
    RosterPageResult,
    build_manifest_records,
    compare_roster_to_database,
    extract_acam_machine_names,
    discover_roster_pages,
    extract_machine_name_candidates,
    extract_pinside_machine_names,
    is_internal_url,
    known_roster_link_hints,
    likely_manifest_from_counts,
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

    def test_known_roster_link_hints_include_funspot_acam(self):
        hints = known_roster_link_hints(1)

        self.assertEqual(len(hints), 1)
        self.assertEqual(hints[0].text, "American Classic Arcade Museum Games")
        self.assertEqual(hints[0].url, "https://www.classicarcademuseum.org/games")
        self.assertEqual(known_roster_link_hints(999999), [])

    def test_discover_roster_pages_follows_explicit_known_external_hints(self):
        probe = ProbeResult(
            ok=True,
            final_url="https://funspotnh.com",
            status_code=200,
            content_type="text/html",
            title="Funspot",
            roster_score=1,
            link_hints=[LinkHint("GAMES", "https://www.funspotnh.com/games.php", 1)],
            cache_path="/tmp/home.html",
            error="",
        )
        fetched_urls = []

        def fake_fetch(url, cache_dir, timeout_seconds, max_bytes):
            fetched_urls.append(url)
            return PageFetchResult(
                ok=True,
                final_url=url,
                status_code=200,
                content_type="text/html",
                title="ACAM Games",
                links=[],
                text="Pac-Man\nGalaga",
                cache_path="/tmp/acam.html",
                error="",
            )

        with patch("scan_arcade_web_rosters.fetch_page", fake_fetch), redirect_stdout(StringIO()):
            pages = discover_roster_pages(
                probe,
                Path("/tmp"),
                timeout_seconds=1,
                max_bytes=1000,
                max_pages=1,
                delay_seconds=0,
                allow_trusted_external=False,
                extra_hints=[
                    LinkHint(
                        "American Classic Arcade Museum Games",
                        "https://www.classicarcademuseum.org/games",
                        10,
                    )
                ],
            )

        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].source_text, "American Classic Arcade Museum Games")
        self.assertEqual(pages[0].extracted_names, ["Pac-Man", "Galaga"])
        self.assertEqual(fetched_urls, ["https://www.classicarcademuseum.org/games"])

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

    def test_extract_acam_machine_names_uses_list_and_detail_titles(self):
        names = extract_acam_machine_names(
            """
            Current list of games on the floor!
            PINBALL!
            F14 Tomcat
            Black Knight 2000
            ACAM Has Some Of The Rarest
            Games On Earth!
            Star Trek: Strategic Operations Simulator
            Manufacturer: Sega • Released: 1983
            Game Description:
            A long paragraph about Star Trek.
            Historical Information:
            Another long paragraph.
            Cloak & Dagger
            Manufacturer: Atari • Released: 1983
            """
        )

        self.assertEqual(
            names,
            [
                "F14 Tomcat",
                "Black Knight 2000",
                "Star Trek: Strategic Operations Simulator",
                "Cloak & Dagger",
            ],
        )

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

    def test_likely_manifest_from_counts_requires_roster_page_and_enough_matches(self):
        self.assertFalse(likely_manifest_from_counts(30, 80, 0))
        self.assertFalse(likely_manifest_from_counts(9, 12, 1))
        self.assertTrue(likely_manifest_from_counts(20, 80, 1))
        self.assertTrue(likely_manifest_from_counts(10, 12, 1))

    def test_build_manifest_records_summarizes_best_roster_page(self):
        candidate = Candidate(
            location_id=1,
            name="Example Arcade",
            city="Orlando",
            state="FL",
            website_url="example.test",
            game_count=40,
            source_game_count=40,
            status="active",
            google_place_id="",
        )
        probe = ProbeResult(
            ok=True,
            final_url="https://example.test",
            status_code=200,
            content_type="text/html",
            title="Example",
            roster_score=5,
            link_hints=[],
            cache_path="/tmp/home.html",
            error="",
        )
        pages = [
            RosterPageResult(
                source_text="Small",
                source_url="https://example.test/small",
                source_score=1,
                ok=True,
                final_url="https://example.test/small",
                status_code=200,
                content_type="text/html",
                title="Small",
                roster_score=1,
                extracted_names=["Galaga"],
                cache_path="/tmp/small.html",
                error="",
            ),
            RosterPageResult(
                source_text="Games",
                source_url="https://example.test/games",
                source_score=4,
                ok=True,
                final_url="https://example.test/games",
                status_code=200,
                content_type="text/html",
                title="Games",
                roster_score=8,
                extracted_names=["Galaga", "Pac-Man"],
                cache_path="/tmp/games.html",
                error="",
            ),
        ]
        comparison = compare_roster_to_database(["Galaga", "Pac-Man"], pages)

        records = build_manifest_records([candidate], {1: probe}, {1: pages}, {1: comparison})

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].website_url, "https://example.test")
        self.assertEqual(records[0].roster_page_count, 2)
        self.assertEqual(records[0].extracted_name_count, 3)
        self.assertEqual(records[0].matched_db_game_count, 2)
        self.assertEqual(records[0].match_ratio, 1.0)
        self.assertEqual(records[0].best_roster_url, "https://example.test/games")

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

            candidates_with_missing_websites = load_candidates(
                conn,
                limit=10,
                min_game_count=2,
                include_missing_websites=True,
            )

            self.assertEqual([candidate.location_id for candidate in candidates_with_missing_websites], [4, 1])
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
