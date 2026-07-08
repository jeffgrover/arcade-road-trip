import random
import unittest

import duckdb

from scan_google_maps_closures import (
    ClosureScan,
    PageSignals,
    PlaceMetadata,
    build_maps_search_url,
    ensure_schema,
    extract_place_metadata,
    failed_scan,
    load_scan_candidates,
    location_query,
    next_delay,
    parse_closure_signal,
    record_scan,
    scan_from_signals,
)


class GoogleMapsClosureScanTests(unittest.TestCase):
    def test_build_maps_search_url_uses_official_search_scheme(self):
        url = build_maps_search_url("Disney Quest Orlando FL")

        self.assertEqual(
            url,
            "https://www.google.com/maps/search/?api=1&query=Disney+Quest+Orlando+FL",
        )

    def test_location_query_includes_name_address_and_city(self):
        query = location_query(
            {
                "name": "Disney Quest",
                "street_address": "1486 East Buena Vista Dr",
                "city": "Lake Buena Vista",
                "state": "FL",
                "postal_code": "32830",
            }
        )

        self.assertEqual(query, "Disney Quest 1486 East Buena Vista Dr Lake Buena Vista FL 32830")

    def test_parse_permanently_closed_signal(self):
        scan = parse_closure_signal(
            "Disney Quest Orlando FL",
            "https://example.test",
            "Google Maps Disney Quest Permanently closed Directions Suggest an edit",
        )

        self.assertEqual(scan.status, "closed")
        self.assertGreaterEqual(scan.confidence, 0.9)
        self.assertIn("permanent-closure", scan.notes)

    def test_duplicated_permanent_closure_signals_raise_confidence(self):
        scan = scan_from_signals(
            "Disney Quest Orlando FL",
            "https://example.test",
            PageSignals(
                body_text="Disney Quest Permanently closed Directions",
                title="Disney Quest - Google Maps",
                aria_labels=("Permanently closed", "Directions to Disney Quest"),
                meta_text="Permanently closed",
            ),
        )

        self.assertEqual(scan.status, "closed")
        self.assertEqual(scan.confidence, 0.98)
        self.assertGreaterEqual(scan.signal_counts["permanent_closure"], 2)

    def test_extract_place_metadata_from_rendered_signals(self):
        metadata = extract_place_metadata(
            PageSignals(
                body_text="Arcade Monsters Overview",
                title="Arcade Monsters Oviedo - Google Maps",
                aria_labels=(
                    "Address: 15 Alafaya Woods Blvd Ste 117, Oviedo, FL 32765 ",
                    "Website: arcademonsters.com ",
                ),
                links=(("Website: arcademonsters.com ", "arcademonsters.com", "http://www.arcademonsters.com/"),),
                app_state='[["ChIJ10tg0Alp54gRjfWEElDkRRw"],"!1s0x88e76909d0604bd7:0x1c45e4501284f58d",28.651782400000002,-81.2069116]',
            )
        )

        self.assertEqual(metadata.google_place_id, "ChIJ10tg0Alp54gRjfWEElDkRRw")
        self.assertEqual(metadata.google_cid, "0x88e76909d0604bd7:0x1c45e4501284f58d")
        self.assertEqual(metadata.website_url, "http://www.arcademonsters.com/")
        self.assertEqual(metadata.address, "15 Alafaya Woods Blvd Ste 117, Oviedo, FL 32765")
        self.assertAlmostEqual(metadata.latitude, 28.6517824)
        self.assertAlmostEqual(metadata.longitude, -81.2069116)

    def test_place_name_prefers_maps_title(self):
        scan = scan_from_signals(
            "Arcade Monsters Oviedo FL",
            "https://example.test",
            PageSignals(
                body_text="Call phone number Address: 15 Alafaya Woods Blvd Ste 117 Directions Reviews",
                title="Arcade Monsters Oviedo - Google Maps",
            ),
        )

        self.assertEqual(scan.matched_name, "Arcade Monsters Oviedo")

    def test_extract_website_from_body_text_when_link_is_missing(self):
        metadata = extract_place_metadata(
            PageSignals(
                body_text="Arcade Monsters Oviedo Open arcademonsters.com Directions Reviews",
                title="Arcade Monsters Oviedo - Google Maps",
            )
        )

        self.assertEqual(metadata.website_url, "https://arcademonsters.com")

    def test_parse_temporarily_closed_signal_needs_review(self):
        scan = parse_closure_signal(
            "Arcade Example",
            "https://example.test",
            "Google Maps Arcade Example Temporarily closed Website Directions",
        )

        self.assertEqual(scan.status, "needs_review")
        self.assertIn("temporary-closure", scan.notes)

    def test_hours_closed_does_not_mark_closed(self):
        scan = parse_closure_signal(
            "Arcade Monsters Oviedo FL",
            "https://example.test",
            "Google Maps Arcade Monsters Closed ⋅ Opens 12 PM Website Directions Reviews",
        )

        self.assertEqual(scan.status, "matched")

    def test_next_delay_uses_range(self):
        delay = next_delay(10, 20, random.Random(7))

        self.assertGreaterEqual(delay, 10)
        self.assertLessEqual(delay, 20)

    def test_failed_scan_records_error_without_closure_signal(self):
        scan = failed_scan("Disney Quest Orlando FL", TimeoutError("slow page"))

        self.assertEqual(scan.status, "scan_error")
        self.assertEqual(scan.confidence, 0.0)
        self.assertEqual(scan.signal_counts["permanent_closure"], 0)
        self.assertIn("TimeoutError", scan.notes)

    def test_load_scan_candidates_skips_recent_google_scans_and_closed_locations(self):
        conn = duckdb.connect(":memory:")
        try:
            ensure_schema(conn)
            conn.execute(
                """
                CREATE TABLE locations (
                    location_id BIGINT, name VARCHAR, street_address VARCHAR, city VARCHAR,
                    state VARCHAR, postal_code VARCHAR, game_count INTEGER
                )
                """
            )
            conn.execute(
                """
                INSERT INTO locations VALUES
                    (1, 'Old Arcade', '1 Main', 'Orlando', 'FL', '32830', 10),
                    (2, 'Recent Arcade', '2 Main', 'Orlando', 'FL', '32830', 10),
                    (3, 'Closed Arcade', '3 Main', 'Orlando', 'FL', '32830', 10)
                """
            )
            conn.execute(
                """
                INSERT INTO location_verifications (
                    verification_id, location_id, checked_at, provider, status
                )
                VALUES (1, 2, '2099-01-01T00:00:00+00:00', 'google_maps_url', 'matched')
                """
            )
            conn.execute(
                """
                INSERT INTO location_statuses (
                    location_id, status, verified_at
                )
                VALUES (3, 'closed', '2026-07-08T00:00:00+00:00')
                """
            )

            candidates = load_scan_candidates(
                conn,
                state="FL",
                limit=10,
                min_game_count=1,
                stale_days=180,
                include_inactive=False,
            )
        finally:
            conn.close()

        self.assertEqual([row["location_id"] for row in candidates], [1])

    def test_record_scan_can_mark_explicit_closed_status(self):
        conn = duckdb.connect(":memory:")
        try:
            ensure_schema(conn)
            scan = ClosureScan(
                query="Disney Quest Orlando FL",
                url="https://example.test",
                status="closed",
                confidence=0.95,
                matched_name="Disney Quest",
                metadata=PlaceMetadata(
                    google_place_id="ChIJclosed",
                    google_cid="0x1:0x2",
                    website_url="https://disneyquest.example",
                    address="1486 East Buena Vista Dr, Lake Buena Vista, FL 32830",
                    latitude=28.3701,
                    longitude=-81.5163,
                ),
                notes="Google Maps rendered explicit permanent-closure signal(s): 2.",
                raw_text="Permanently closed",
                signal_counts={"permanent_closure": 2, "temporary_closure": 0, "place_cues": 2},
            )
            conn.execute("CREATE TABLE locations(location_id BIGINT, google_place_id VARCHAR, google_cid VARCHAR, website_url VARCHAR, street_address VARCHAR, latitude DOUBLE, longitude DOUBLE)")
            conn.execute("INSERT INTO locations(location_id) VALUES (214)")

            record_scan(conn, 214, scan, "2026-07-08T00:00:00+00:00", apply_status=True)

            verification = conn.execute(
                "SELECT provider, status, evidence_url, matched_address, matched_latitude, matched_longitude FROM location_verifications WHERE location_id = 214"
            ).fetchone()
            status = conn.execute(
                "SELECT status, evidence FROM location_statuses WHERE location_id = 214"
            ).fetchone()
            location = conn.execute(
                "SELECT google_place_id, google_cid, website_url, street_address, latitude, longitude FROM locations WHERE location_id = 214"
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(
            verification,
            (
                "google_maps_url",
                "closed",
                "https://example.test",
                "1486 East Buena Vista Dr, Lake Buena Vista, FL 32830",
                28.3701,
                -81.5163,
            ),
        )
        self.assertEqual(status, ("closed", "google_maps_url"))
        self.assertEqual(
            location,
            (
                "ChIJclosed",
                "0x1:0x2",
                "https://disneyquest.example",
                "1486 East Buena Vista Dr, Lake Buena Vista, FL 32830",
                28.3701,
                -81.5163,
            ),
        )

    def test_record_scan_does_not_overwrite_existing_details_by_default(self):
        conn = duckdb.connect(":memory:")
        try:
            ensure_schema(conn)
            conn.execute(
                """
                CREATE TABLE locations(
                    location_id BIGINT, google_place_id VARCHAR, website_url VARCHAR,
                    street_address VARCHAR, latitude DOUBLE, longitude DOUBLE
                )
                """
            )
            conn.execute(
                """
                INSERT INTO locations VALUES (
                    1, 'ExistingPlace', 'https://existing.example',
                    'Old Address', 1.0, 2.0
                )
                """
            )
            scan = ClosureScan(
                query="Open Arcade",
                url="https://example.test",
                status="matched",
                confidence=0.65,
                matched_name="Open Arcade",
                metadata=PlaceMetadata(
                    google_place_id="NewPlace",
                    website_url="https://new.example",
                    address="New Address",
                    latitude=3.0,
                    longitude=4.0,
                ),
                notes="Matched",
                raw_text="",
                signal_counts={"permanent_closure": 0, "temporary_closure": 0, "place_cues": 2},
            )

            record_scan(conn, 1, scan, "2026-07-08T00:00:00+00:00", apply_status=True)
            row = conn.execute(
                "SELECT google_place_id, website_url, street_address, latitude, longitude FROM locations WHERE location_id = 1"
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(row, ("ExistingPlace", "https://existing.example", "Old Address", 1.0, 2.0))


if __name__ == "__main__":
    unittest.main()
