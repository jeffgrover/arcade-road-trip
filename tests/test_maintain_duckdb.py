import tempfile
import unittest
from pathlib import Path

import duckdb

from maintain_duckdb import compact_database, force_checkpoint


class MaintainDuckDBTests(unittest.TestCase):
    def test_force_checkpoint_and_compact_preserve_data(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.duckdb"
            with duckdb.connect(str(db_path)) as conn:
                conn.execute("CREATE TABLE items(id INTEGER, name VARCHAR)")
                conn.execute("INSERT INTO items VALUES (1, 'alpha'), (2, 'beta')")

            force_checkpoint(db_path)
            compact_database(db_path)

            with duckdb.connect(str(db_path), read_only=True) as conn:
                rows = conn.execute("SELECT * FROM items ORDER BY id").fetchall()

        self.assertEqual(rows, [(1, "alpha"), (2, "beta")])


if __name__ == "__main__":
    unittest.main()
