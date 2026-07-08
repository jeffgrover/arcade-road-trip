#!/usr/bin/env python3
"""DuckDB maintenance helpers for the Arcade Road Trip pipeline."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import duckdb

from arcade_db import DEFAULT_DUCKDB


def sql_literal(value: Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def force_checkpoint(db_path: Path) -> None:
    with duckdb.connect(str(db_path)) as conn:
        conn.execute("FORCE CHECKPOINT")


def compact_database(db_path: Path) -> Path:
    temp_path = db_path.with_name(f"{db_path.stem}.compact_tmp{db_path.suffix}")
    if temp_path.exists():
        temp_path.unlink()

    controller = duckdb.connect(":memory:")
    try:
        controller.execute(f"ATTACH {sql_literal(db_path)} AS source_db (READ_ONLY)")
        controller.execute(f"ATTACH {sql_literal(temp_path)} AS compacted_db")
        controller.execute("COPY FROM DATABASE source_db TO compacted_db")
        controller.execute("FORCE CHECKPOINT compacted_db")
        controller.execute("DETACH compacted_db")
        controller.execute("DETACH source_db")
    finally:
        controller.close()

    os.replace(temp_path, db_path)
    return db_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Checkpoint and optionally compact a DuckDB database.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DUCKDB)
    parser.add_argument("--compact", action="store_true", help="Rewrite the database through COPY FROM DATABASE.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    before = args.db.stat().st_size
    if args.compact:
        compact_database(args.db)
        action = "compacted"
    else:
        force_checkpoint(args.db)
        action = "checkpointed"
    after = args.db.stat().st_size
    print(f"{action} {args.db} ({before:,} -> {after:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
