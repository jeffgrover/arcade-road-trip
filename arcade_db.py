"""Shared DuckDB helpers for Arcade Road Trip pipeline scripts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import duckdb

from us_states import CONTINENTAL_US_STATES


DEFAULT_DUCKDB = Path("arcade_roadtrip.duckdb")
ACTIVE_LOCATION_STATUSES = ("active", "unverified", "uncertain", "matched", "needs_review")
INACTIVE_LOCATION_STATUSES = ("closed", "replaced")


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


def placeholders(values: Iterable[Any]) -> str:
    return ",".join("?" for _ in values)


def active_location_status_clause(status_sql: str = "COALESCE(ls.status, 'active')") -> str:
    return f"{status_sql} IN ({placeholders(ACTIVE_LOCATION_STATUSES)})"


def continental_us_state_clause(state_sql: str = "l.state") -> str:
    return f"upper({state_sql}) IN ({placeholders(CONTINENTAL_US_STATES)})"


def sql_string_list(values: Iterable[str]) -> str:
    return ",".join("'" + value.replace("'", "''") + "'" for value in values)


def continental_us_state_literal_clause(state_sql: str = "l.state") -> str:
    return f"upper({state_sql}) IN ({sql_string_list(CONTINENTAL_US_STATES)})"


def active_atlas_location_clause(
    status_sql: str = "COALESCE(ls.status, 'active')",
    state_sql: str = "l.state",
) -> str:
    return f"{active_location_status_clause(status_sql)} AND {continental_us_state_clause(state_sql)}"


def inactive_location_status_clause(status_sql: str = "COALESCE(ls.status, 'active')") -> str:
    return f"{status_sql} IN ({placeholders(INACTIVE_LOCATION_STATUSES)})"


def active_location_status_params() -> tuple[str, ...]:
    return ACTIVE_LOCATION_STATUSES


def continental_us_state_params() -> tuple[str, ...]:
    return CONTINENTAL_US_STATES


def active_atlas_location_params() -> tuple[str, ...]:
    return (*ACTIVE_LOCATION_STATUSES, *CONTINENTAL_US_STATES)


def inactive_location_status_params() -> tuple[str, ...]:
    return INACTIVE_LOCATION_STATUSES
