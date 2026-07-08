import unittest

import duckdb

from validate_pinballmap_locations import ensure_schema, record_validation


class PinballMapValidationWriteTests(unittest.TestCase):
    def setUp(self):
        self.conn = duckdb.connect(":memory:")
        ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_fresh_validation_preserves_non_pinballmap_notes(self):
        self.conn.execute(
            """
            INSERT INTO location_statuses (
                location_id, status, replacement_name, confidence, verified_at, evidence, notes
            )
            VALUES (42, 'matched', NULL, 0.9, 'before', 'ziv', 'User confirmed ZIv match')
            """
        )

        record_validation(
            self.conn,
            location_id=42,
            pinballmap_id=123,
            data={"id": 123, "name": "Arcade", "street": "1 Main", "city": "SLC", "state": "UT", "zip": "84000"},
            status="fresh_pinballmap",
            confidence=0.99,
            notes="Generated Pinball Map note",
            checked_at="2026-07-07T00:00:00+00:00",
            apply_status=True,
        )

        row = self.conn.execute(
            "SELECT status, confidence, verified_at, evidence, notes FROM location_statuses WHERE location_id = 42"
        ).fetchone()

        self.assertEqual(row, ("matched", 0.99, "2026-07-07T00:00:00+00:00", "pinballmap", "User confirmed ZIv match"))


if __name__ == "__main__":
    unittest.main()
