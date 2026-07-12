#!/usr/bin/env python3
"""Build a DuckDB artifact from a base snapshot plus curation JSONL files."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import duckdb

from game_provenance import ensure_schema as ensure_game_provenance_schema


REPLACE_KEYS = {
    "game_source_records": ("source", "source_game_id"),
    "duplicate_review_candidates": ("candidate_id",),
    "duplicate_review_records": ("candidate_id", "game_id"),
    "location_statuses": ("location_id",),
    "pinballmap_location_links": ("location_id", "pinballmap_location_id"),
    "ziv_location_links": ("location_id", "ziv_location_id"),
}


def clone_database(base: Path, output: Path) -> None:
    if output.exists():
        output.unlink()
    controller = duckdb.connect(":memory:")
    try:
        controller.execute(f"ATTACH '{base.resolve()}' AS source_db (READ_ONLY)")
        controller.execute(f"ATTACH '{output.resolve()}' AS build_db")
        controller.execute("COPY FROM DATABASE source_db TO build_db")
        controller.execute("DETACH build_db")
        controller.execute("DETACH source_db")
    finally:
        controller.close()


def table_columns(conn: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    return [row[0] for row in conn.execute(f'DESCRIBE "{table}"').fetchall()]


def has_table(conn: duckdb.DuckDBPyConnection, table: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema='main' AND table_name=?",
        (table,),
    ).fetchone())


def load_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def apply_table(conn: duckdb.DuckDBPyConnection, table: str, path: Path) -> int:
    if not has_table(conn, table):
        return 0
    columns = table_columns(conn, table)
    records = load_rows(path)
    if not records:
        return 0
    keys = REPLACE_KEYS.get(table)
    insert_columns = [column for column in columns if column in records[0]]
    quoted_columns = ", ".join(f'"{column}"' for column in insert_columns)
    placeholders = ", ".join("?" for _ in insert_columns)
    statement = f'INSERT INTO "{table}" ({quoted_columns}) VALUES ({placeholders})'
    for record in records:
        if keys:
            predicates = " AND ".join(f'"{key}" = ?' for key in keys)
            conn.execute(f'DELETE FROM "{table}" WHERE {predicates}', tuple(record.get(key) for key in keys))
        conn.execute(statement, tuple(record.get(column) for column in insert_columns))
    return len(records)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-db", type=Path, default=Path("arcade_roadtrip.duckdb"))
    parser.add_argument("--output-db", type=Path, default=Path("build/arcade_roadtrip.duckdb"))
    parser.add_argument("--artifacts-dir", type=Path, default=Path("curation"))
    args = parser.parse_args()
    args.output_db.parent.mkdir(parents=True, exist_ok=True)
    clone_database(args.base_db, args.output_db)
    with duckdb.connect(str(args.output_db)) as conn:
        ensure_game_provenance_schema(conn)
        applied = {}
        for table in REPLACE_KEYS | {"duplicate_review_decisions", "location_verifications", "web_roster_reconciliation_actions"}:
            applied[table] = apply_table(conn, table, args.artifacts_dir / f"{table}.jsonl")
        conn.execute("FORCE CHECKPOINT")
    print(f"built {args.output_db}")
    for table, count in sorted(applied.items()):
        if count:
            print(f"applied {table}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
