import unittest

from reconcile_web_rosters import (
    build_reconciliations,
    build_location_reconciliation,
    candidate_similarity,
    detect_review_status,
    is_noise_name,
    pair_canonical_candidates,
    select_manifest_records,
)


class WebRosterReconciliationTests(unittest.TestCase):
    def test_select_manifest_records_uses_likely_manifest_and_match_count(self):
        records = [
            {"location_id": "1", "likely_manifest": "False", "matched_db_game_count": "999", "game_count": "999"},
            {"location_id": "2", "likely_manifest": "True", "matched_db_game_count": "10", "game_count": "20"},
            {"location_id": "3", "likely_manifest": "True", "matched_db_game_count": "30", "game_count": "40"},
        ]

        selected = select_manifest_records(records, limit=2)

        self.assertEqual([record["location_id"] for record in selected], ["3", "2"])

    def test_select_manifest_records_can_filter_to_one_location(self):
        records = [
            {"location_id": "1", "likely_manifest": "True", "matched_db_game_count": "999", "game_count": "999"},
            {"location_id": "2", "likely_manifest": "True", "matched_db_game_count": "10", "game_count": "20"},
        ]

        selected = select_manifest_records(records, limit=0, location_id=2)

        self.assertEqual([record["location_id"] for record in selected], ["2"])

    def test_candidate_similarity_handles_short_roster_aliases(self):
        self.assertGreaterEqual(candidate_similarity("1943", "1943: The Battle Of Midway"), 0.9)
        self.assertGreaterEqual(candidate_similarity("LA Machine Guns", "L.A. Machineguns: Rage Of The Machines"), 0.9)
        self.assertLess(candidate_similarity("Pit", "Spitfire"), 0.9)

    def test_pair_canonical_candidates_matches_close_titles_once(self):
        canonical, used_website, used_db = pair_canonical_candidates(
            ["Bride of Pinbot", "Attack Mars"],
            ["Bride of Pinbot, The", "Attack from Mars"],
            threshold=0.70,
        )

        self.assertEqual(len(canonical), 2)
        self.assertEqual(used_website, {"Bride of Pinbot", "Attack Mars"})
        self.assertEqual(used_db, {"Bride of Pinbot, The", "Attack from Mars"})

    def test_is_noise_name_filters_site_chrome(self):
        self.assertTrue(is_noise_name("Pinball Repair"))
        self.assertTrue(is_noise_name("Games"))
        self.assertTrue(is_noise_name("freeplay@example.test"))
        self.assertTrue(is_noise_name("Upcoming Events & Tournaments"))
        self.assertTrue(is_noise_name("© 2014 Enigma Theme|Theme Developed By Weblizar Themes"))
        self.assertFalse(is_noise_name("Medieval Madness"))

    def test_build_location_reconciliation_buckets_candidates(self):
        manifest = {
            "location_id": "42",
            "name": "Example Arcade",
            "city": "Denver",
            "state": "CO",
            "game_count": "10",
            "match_ratio": "0.8",
            "best_roster_url": "https://example.test/games",
        }
        comparison = {
            "db_game_count": 10,
            "matched_db_games": ["Galaga"] * 8,
            "missing_db_games": ["Bride of Pinbot, The", "Missing Game"],
            "website_only_names": ["Bride of Pinbot", "New Game", "Another New Game", "Third New Game", "Pinball Repair"],
        }

        result = build_location_reconciliation(manifest, comparison, max_names=10)

        self.assertEqual(result.add_candidate_count, 3)
        self.assertEqual(result.remove_candidate_count, 1)
        self.assertEqual(result.canonical_candidate_count, 1)
        self.assertEqual(result.ignored_website_name_count, 1)
        self.assertEqual(result.review_status, "review_ready")
        self.assertEqual(result.add_candidates, ["New Game", "Another New Game", "Third New Game"])
        self.assertEqual(result.remove_candidates, ["Missing Game"])
        self.assertEqual(result.ignored_website_names, ["Pinball Repair"])
        self.assertEqual(result.canonical_candidates[0].website_name, "Bride of Pinbot")
        self.assertEqual(result.canonical_candidates[0].db_name, "Bride of Pinbot, The")

    def test_detect_review_status_flags_noisy_or_partial_rosters(self):
        ready = detect_review_status(
            match_ratio=0.9,
            db_game_count=100,
            add_candidate_count=10,
            remove_candidate_count=10,
            ignored_website_name_count=1,
            website_only_name_count=20,
        )
        self.assertEqual(ready[0], "review_ready")

        partial = detect_review_status(
            match_ratio=0.5,
            db_game_count=100,
            add_candidate_count=10,
            remove_candidate_count=10,
            ignored_website_name_count=1,
            website_only_name_count=20,
        )
        self.assertEqual(partial[0], "partial_or_stale")

        noisy = detect_review_status(
            match_ratio=0.9,
            db_game_count=100,
            add_candidate_count=10,
            remove_candidate_count=10,
            ignored_website_name_count=10,
            website_only_name_count=20,
        )
        self.assertEqual(noisy[0], "needs_parser")

    def test_build_reconciliations_can_filter_review_ready_records(self):
        manifest_records = [
            {
                "location_id": "1",
                "likely_manifest": "True",
                "matched_db_game_count": "90",
                "game_count": "100",
                "name": "Clean Arcade",
                "city": "Denver",
                "state": "CO",
                "match_ratio": "0.9",
                "best_roster_url": "https://clean.example/games",
            },
            {
                "location_id": "2",
                "likely_manifest": "True",
                "matched_db_game_count": "40",
                "game_count": "100",
                "name": "Partial Arcade",
                "city": "Denver",
                "state": "CO",
                "match_ratio": "0.4",
                "best_roster_url": "https://partial.example/games",
            },
        ]
        scan_report = {
            "comparisons": {
                "1": {
                    "db_game_count": 100,
                    "matched_db_games": ["Galaga"] * 90,
                    "missing_db_games": ["Missing"],
                    "website_only_names": ["New Game"],
                },
                "2": {
                    "db_game_count": 100,
                    "matched_db_games": ["Galaga"] * 40,
                    "missing_db_games": ["Missing"],
                    "website_only_names": ["New Game"],
                },
            }
        }

        results = build_reconciliations(manifest_records, scan_report, limit=0, max_names=10, review_ready_only=True)

        self.assertEqual([result.name for result in results], ["Clean Arcade"])

    def test_build_reconciliations_can_filter_to_one_location(self):
        manifest_records = [
            {
                "location_id": "1",
                "likely_manifest": "True",
                "matched_db_game_count": "90",
                "game_count": "100",
                "name": "Clean Arcade",
                "city": "Denver",
                "state": "CO",
                "match_ratio": "0.9",
                "best_roster_url": "https://clean.example/games",
            },
            {
                "location_id": "2",
                "likely_manifest": "True",
                "matched_db_game_count": "80",
                "game_count": "100",
                "name": "Other Arcade",
                "city": "Boulder",
                "state": "CO",
                "match_ratio": "0.8",
                "best_roster_url": "https://other.example/games",
            },
        ]
        scan_report = {
            "comparisons": {
                "1": {"db_game_count": 100, "matched_db_games": ["Galaga"] * 90, "missing_db_games": [], "website_only_names": []},
                "2": {"db_game_count": 100, "matched_db_games": ["Pac-Man"] * 80, "missing_db_games": [], "website_only_names": []},
            }
        }

        results = build_reconciliations(manifest_records, scan_report, limit=0, max_names=10, location_id=2)

        self.assertEqual([result.name for result in results], ["Other Arcade"])


if __name__ == "__main__":
    unittest.main()
