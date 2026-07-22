import tempfile
import unittest
from pathlib import Path

import duckdb

from repair_google_maps_scan import repair_scan_damage


class RepairGoogleMapsScanTests(unittest.TestCase):
    def test_repair_restores_metadata_and_only_demotes_google_closures(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            baseline_path = Path(tmpdir) / "baseline.duckdb"
            current_path = Path(tmpdir) / "current.duckdb"
            with duckdb.connect(str(baseline_path)) as baseline:
                baseline.execute(
                    """
                    CREATE TABLE locations(
                        location_id BIGINT, name VARCHAR, website_url VARCHAR, street_address VARCHAR,
                        latitude DOUBLE, longitude DOUBLE
                    );
                    INSERT INTO locations VALUES
                        (1, 'Original Arcade', 'https://original.example', '1 Main', 28.1, -81.1),
                        (2, 'Manual Arcade', NULL, '2 Main', 29.2, -82.2);
                    """
                )
            with duckdb.connect(str(current_path)) as current:
                current.execute(
                    """
                    CREATE TABLE locations(
                        location_id BIGINT, name VARCHAR, website_url VARCHAR, street_address VARCHAR,
                        latitude DOUBLE, longitude DOUBLE, google_place_id VARCHAR, google_cid VARCHAR
                    );
                    INSERT INTO locations VALUES
                        (1, 'Original Arcade', 'https://maps.m.en.bad.es', 'Wrong', 40.0, -111.0, 'ChIJwrong', '0x1:0x2'),
                        (2, 'Manual Arcade', 'https://wrong.example', 'Also Wrong', 41.0, -112.0, 'ChIJother', NULL);
                    CREATE TABLE location_statuses(
                        location_id BIGINT, status VARCHAR, replacement_name VARCHAR,
                        confidence DOUBLE, verified_at VARCHAR, evidence VARCHAR, notes VARCHAR
                    );
                    INSERT INTO location_statuses VALUES
                        (1, 'closed', NULL, 0.98, 'old', 'google_maps_url', 'legacy'),
                        (2, 'closed', NULL, 1.0, 'old', 'manual', 'confirmed');
                    """
                )

                preview = repair_scan_damage(current, baseline_path)
                self.assertEqual(preview, {
                    "restored_locations": 2,
                    "cleared_place_ids": 2,
                    "cleared_bogus_websites": 1,
                    "preserved_closures": 0,
                    "demoted_closures": 1,
                })
                self.assertEqual(
                    current.execute("SELECT website_url FROM locations WHERE location_id = 1").fetchone()[0],
                    "https://maps.m.en.bad.es",
                )

                repair_scan_damage(
                    current,
                    baseline_path,
                    apply=True,
                    repaired_at="2026-07-22T12:00:00+00:00",
                )
                locations = current.execute(
                    """
                    SELECT location_id, website_url, street_address, latitude, longitude,
                           google_place_id, google_cid
                    FROM locations ORDER BY location_id
                    """
                ).fetchall()
                statuses = current.execute(
                    "SELECT location_id, status, evidence FROM location_statuses ORDER BY location_id"
                ).fetchall()
                current.execute(
                    """
                    CREATE TABLE location_verifications(
                        location_id BIGINT, provider VARCHAR, match_kind VARCHAR
                    );
                    INSERT INTO location_verifications
                    VALUES (1, 'google_maps_url', 'place_id_url_v2');
                    UPDATE locations SET
                        website_url = 'https://validated.example',
                        google_place_id = 'ChIJvalidated'
                    WHERE location_id = 1;
                    """
                )
                repair_scan_damage(current, baseline_path, apply=True)
                protected = current.execute(
                    "SELECT website_url, google_place_id FROM locations WHERE location_id = 1"
                ).fetchone()

        self.assertEqual(
            locations,
            [
                (1, "https://original.example", "1 Main", 28.1, -81.1, None, None),
                (2, None, "2 Main", 29.2, -82.2, None, None),
            ],
        )
        self.assertEqual(statuses, [(1, "needs_review", "google_maps_url"), (2, "closed", "manual")])
        self.assertEqual(protected, ("https://validated.example", "ChIJvalidated"))


if __name__ == "__main__":
    unittest.main()
