#!/usr/bin/env python3
"""Backfill source provenance before duplicate-game consolidation."""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb

from arcade_db import DEFAULT_DUCKDB
from game_provenance import ensure_schema, migrate_existing_records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DUCKDB)
    args = parser.parse_args()
    with duckdb.connect(str(args.db)) as conn:
        ensure_schema(conn)
        result = migrate_existing_records(conn)
        conn.commit()
        print(f"source records migrated: {result['migrated']}")
        print(f"rows without recognized source namespace: {result['skipped']}")
        print("no games or placements deleted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
