#!/usr/bin/env python3
"""Create the canonical DuckDB database from the legacy SQLite snapshot."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa


DEFAULT_SQLITE = Path("aurcade_locations.sqlite")
DEFAULT_DUCKDB = Path("arcade_roadtrip.duckdb")
SQLITE_INTERNAL_PREFIXES = ("sqlite_",)


def quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def duckdb_type(sqlite_type: str) -> str:
    normalized = sqlite_type.upper()
    if "INT" in normalized:
        return "BIGINT"
    if any(token in normalized for token in ("REAL", "FLOA", "DOUB")):
        return "DOUBLE"
    if "BLOB" in normalized:
        return "BLOB"
    return "VARCHAR"


def sqlite_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
        ORDER BY name
        """
    ).fetchall()
    return [
        row[0]
        for row in rows
        if not any(row[0].startswith(prefix) for prefix in SQLITE_INTERNAL_PREFIXES)
    ]


def sqlite_columns(conn: sqlite3.Connection, table_name: str) -> list[tuple[str, str]]:
    rows = conn.execute(f"PRAGMA table_info({quote_ident(table_name)})").fetchall()
    return [(row[1], duckdb_type(row[2] or "")) for row in rows]


def sqlite_rows(conn: sqlite3.Connection, table_name: str) -> list[dict[str, Any]]:
    cursor = conn.execute(f"SELECT * FROM {quote_ident(table_name)}")
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def create_empty_table(conn: duckdb.DuckDBPyConnection, table_name: str, columns: list[tuple[str, str]]) -> None:
    column_sql = ", ".join(f"{quote_ident(name)} {column_type}" for name, column_type in columns)
    conn.execute(f"CREATE TABLE {quote_ident(table_name)} ({column_sql})")


def copy_table(
    sqlite_conn: sqlite3.Connection,
    duckdb_conn: duckdb.DuckDBPyConnection,
    table_name: str,
) -> int:
    rows = sqlite_rows(sqlite_conn, table_name)
    if rows:
        arrow_table = pa.Table.from_pylist(rows)
        view_name = f"source_{table_name}"
        duckdb_conn.register(view_name, arrow_table)
        duckdb_conn.execute(f"CREATE TABLE {quote_ident(table_name)} AS SELECT * FROM {quote_ident(view_name)}")
        duckdb_conn.unregister(view_name)
    else:
        create_empty_table(duckdb_conn, table_name, sqlite_columns(sqlite_conn, table_name))
    return len(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Migrate the legacy SQLite database into canonical DuckDB.")
    parser.add_argument("--sqlite", type=Path, default=DEFAULT_SQLITE, help="Legacy SQLite source database.")
    parser.add_argument("--duckdb", type=Path, default=DEFAULT_DUCKDB, help="DuckDB output database.")
    parser.add_argument("--replace", action="store_true", help="Replace an existing DuckDB output file.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.duckdb.exists():
        if not args.replace:
            raise SystemExit(f"{args.duckdb} already exists; pass --replace to overwrite it.")
        args.duckdb.unlink()

    sqlite_conn = sqlite3.connect(args.sqlite)
    try:
        duckdb_conn = duckdb.connect(str(args.duckdb))
        try:
            copied = []
            for table_name in sqlite_tables(sqlite_conn):
                row_count = copy_table(sqlite_conn, duckdb_conn, table_name)
                copied.append((table_name, row_count))
            duckdb_conn.execute("CHECKPOINT")
        finally:
            duckdb_conn.close()
    finally:
        sqlite_conn.close()

    for table_name, row_count in copied:
        print(f"copied {table_name}: {row_count} rows")
    print(f"wrote {args.duckdb}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
