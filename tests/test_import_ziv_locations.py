import unittest

import duckdb

from import_ziv_locations import ImportPlan, insert_locations
from scrape_aurcade_locations import ensure_schema as ensure_arcade_schema
from validate_ziv_locations import ensure_schema as ensure_ziv_schema


class ZivImportDuckDBWriteTests(unittest.TestCase):
    def setUp(self):
        self.conn = duckdb.connect(":memory:")
        ensure_arcade_schema(self.conn)
        ensure_ziv_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_empty_import_plan_is_noop(self):
        plan = ImportPlan(
            matched_ziv_ids=set(),
            override_links={},
            locations_to_insert=[],
            details={},
        )

        insert_locations(self.conn, plan, "2026-07-07T00:00:00+00:00")

        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM locations").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ziv_location_links").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM location_verifications").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
