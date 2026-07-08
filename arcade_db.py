"""Shared DuckDB helpers for Arcade Road Trip pipeline scripts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import duckdb


DEFAULT_DUCKDB = Path("arcade_roadtrip.duckdb")


def connect(db_path: Path = DEFAULT_DUCKDB, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(db_path), read_only=read_only)


def rows(conn: duckdb.DuckDBPyConnection, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    result = conn.execute(sql, list(params))
    columns = [description[0] for description in result.description]
    return [dict(zip(columns, row)) for row in result.fetchall()]


def has_table(conn: duckdb.DuckDBPyConnection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema = 'main' AND lower(table_name) = lower(?)",
        (table_name,),
    ).fetchone()
    return row is not None


def execute_script(conn: duckdb.DuckDBPyConnection, script: str) -> None:
    for statement in script.split(";"):
        statement = statement.strip()
        if statement:
            conn.execute(statement)
