#!/usr/bin/env python3
"""Read-only query helper for the local Aurcade/Pinball Map DuckDB database.

This is intentionally small and boring: it gives Codex a stable shell interface
for asking structured questions, while keeping the database read-only.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import math
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

import duckdb

from arcade_db import DEFAULT_DUCKDB, connect as duckdb_connect, has_table as duckdb_has_table


DEFAULT_DB = DEFAULT_DUCKDB
DEFAULT_LIMIT = 25
ACTIVE_STATUSES = ("active", "unverified", "uncertain", "matched", "needs_review")
LOCATION_ID_PATTERN = re.compile(r"\((-?\d+)\)")


@dataclass(frozen=True)
class QueryResult:
    title: str
    columns: list[str]
    rows: list[dict[str, Any]]
    notes: list[str]


def connect(db_path: Path) -> duckdb.DuckDBPyConnection:
    return duckdb_connect(db_path, read_only=True)


def rows_from_cursor(cursor: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    columns = [description[0] for description in cursor.description or []]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def run_query(
    conn: duckdb.DuckDBPyConnection,
    sql: str,
    params: Sequence[Any] = (),
    *,
    title: str,
    notes: Optional[list[str]] = None,
) -> QueryResult:
    cursor = conn.execute(sql, params)
    columns = [description[0] for description in cursor.description or []]
    return QueryResult(title=title, columns=columns, rows=rows_from_cursor(cursor), notes=notes or [])


def has_table(conn: duckdb.DuckDBPyConnection, table_name: str) -> bool:
    return duckdb_has_table(conn, table_name)


def location_status_join(conn: duckdb.DuckDBPyConnection, alias: str = "l") -> str:
    if not has_table(conn, "location_statuses"):
        return ""
    return f"LEFT JOIN location_statuses ls ON ls.location_id = {alias}.location_id"


def active_location_clause(conn: duckdb.DuckDBPyConnection, alias: str = "l", include_inactive: bool = False) -> str:
    if include_inactive or not has_table(conn, "location_statuses"):
        return "1=1"
    quoted = ",".join(f"'{status}'" for status in ACTIVE_STATUSES)
    return f"COALESCE(ls.status, 'active') IN ({quoted})"


def status_columns(conn: duckdb.DuckDBPyConnection) -> str:
    if not has_table(conn, "location_statuses"):
        return ""
    return ", COALESCE(ls.status, 'active') AS status, ls.replacement_name"


def game_identity_cte(conn: duckdb.DuckDBPyConnection) -> str:
    if has_table(conn, "game_canonical_links"):
        return """
        game_identity AS (
            SELECT
                g.game_id,
                COALESCE(gcl.canonical_game_id, g.game_id) AS canonical_game_id,
                COALESCE(cg.name, g.name) AS canonical_name,
                COALESCE(cg.manufacturer, g.manufacturer) AS canonical_manufacturer,
                g.name AS source_name,
                g.manufacturer AS source_manufacturer
            FROM games g
            LEFT JOIN game_canonical_links gcl ON gcl.alias_game_id = g.game_id
            LEFT JOIN games cg ON cg.game_id = gcl.canonical_game_id
        )
        """
    return """
    game_identity AS (
        SELECT
            g.game_id,
            g.game_id AS canonical_game_id,
            g.name AS canonical_name,
            g.manufacturer AS canonical_manufacturer,
            g.name AS source_name,
            g.manufacturer AS source_manufacturer
        FROM games g
    )
    """


def require_readonly_sql(sql: str) -> None:
    stripped = sql.strip().lower()
    allowed_prefixes = ("select", "with", "pragma")
    if not stripped.startswith(allowed_prefixes):
        raise ValueError("Raw SQL must start with SELECT, WITH, or PRAGMA.")
    if ";" in stripped.rstrip(";"):
        raise ValueError("Raw SQL accepts one statement at a time.")


def normalize(value: Optional[str]) -> str:
    if value is None:
        return ""
    return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in value).split())


def fuzzy_score(needle: str, candidate: str) -> float:
    needle_norm = normalize(needle)
    candidate_norm = normalize(candidate)
    if not needle_norm or not candidate_norm:
        return 0.0
    if needle_norm == candidate_norm:
        return 1.0
    if needle_norm in candidate_norm:
        return 0.95
    if candidate_norm in needle_norm:
        length_ratio = len(candidate_norm) / len(needle_norm)
        return min(0.9, 0.55 + 0.35 * length_ratio)
    return difflib.SequenceMatcher(None, needle_norm, candidate_norm).ratio()


def markdown_table(columns: Sequence[str], rows: Sequence[dict[str, Any]]) -> str:
    if not columns:
        return ""
    table_rows = [[stringify(row.get(column)) for column in columns] for row in rows]
    widths = [
        max(len(str(column)), *(len(row[index]) for row in table_rows)) if table_rows else len(str(column))
        for index, column in enumerate(columns)
    ]
    header = "| " + " | ".join(str(column).ljust(widths[index]) for index, column in enumerate(columns)) + " |"
    divider = "| " + " | ".join("-" * widths[index] for index, _ in enumerate(columns)) + " |"
    body = [
        "| " + " | ".join(row[index].ljust(widths[index]) for index, _ in enumerate(columns)) + " |"
        for row in table_rows
    ]
    return "\n".join([header, divider, *body])


def stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return str(value).replace("\n", " ")


def render(result: QueryResult, output_format: str) -> str:
    if output_format == "json":
        return json.dumps(
            {"title": result.title, "notes": result.notes, "rows": result.rows},
            indent=2,
            ensure_ascii=False,
        )
    if output_format == "csv":
        import io

        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=result.columns)
        writer.writeheader()
        writer.writerows(result.rows)
        return buffer.getvalue().rstrip()

    parts = [f"## {result.title}"]
    if result.notes:
        parts.extend(f"- {note}" for note in result.notes)
    if result.rows:
        parts.append(markdown_table(result.columns, result.rows))
    else:
        parts.append("_No rows._")
    return "\n\n".join(parts)


def summary(conn: duckdb.DuckDBPyConnection, include_inactive: bool = False) -> QueryResult:
    status_join = location_status_join(conn)
    active_clause = active_location_clause(conn, include_inactive=include_inactive)
    canonical_links_metric = (
        "UNION ALL SELECT 'game_canonical_links', COUNT(*) FROM game_canonical_links"
        if has_table(conn, "game_canonical_links")
        else ""
    )
    sql = """
    SELECT 'locations' AS metric, COUNT(*) AS value FROM locations
    UNION ALL SELECT 'active_locations', COUNT(*) FROM locations l {status_join} WHERE {active_clause}
    UNION ALL SELECT 'locations_ut', COUNT(*) FROM locations WHERE state = 'UT'
    UNION ALL SELECT 'active_locations_ut', COUNT(*) FROM locations l {status_join} WHERE l.state = 'UT' AND {active_clause}
    UNION ALL SELECT 'pinballmap_only_locations', COUNT(*) FROM locations WHERE location_id BETWEEN -1999999999 AND -1000000000
    UNION ALL SELECT 'ziv_only_locations', COUNT(*) FROM locations WHERE location_id BETWEEN -2999999999 AND -2000000000
    UNION ALL SELECT 'games', COUNT(*) FROM games
    UNION ALL SELECT 'pinballmap_only_games', COUNT(*) FROM games WHERE game_id BETWEEN -1999999999 AND -1000000000
    UNION ALL SELECT 'ziv_only_games', COUNT(*) FROM games WHERE game_id BETWEEN -2999999999 AND -2000000000
    UNION ALL SELECT 'location_games', COUNT(*) FROM location_games
    UNION ALL SELECT 'active_location_games', COUNT(*) FROM location_games lg JOIN locations l ON l.location_id = lg.location_id {status_join} WHERE {active_clause}
    UNION ALL SELECT 'pinball_rows', COUNT(*) FROM location_games WHERE cabinet_type = 'Pinball'
    UNION ALL SELECT 'active_pinball_rows', COUNT(*) FROM location_games lg JOIN locations l ON l.location_id = lg.location_id {status_join} WHERE lg.cabinet_type = 'Pinball' AND {active_clause}
    {canonical_links_metric}
    """.format(
        status_join=status_join,
        active_clause=active_clause,
        canonical_links_metric=canonical_links_metric,
    )
    return run_query(conn, sql, title="Database Summary")


def city_summary(conn: duckdb.DuckDBPyConnection, state: str, limit: int, include_inactive: bool = False) -> QueryResult:
    status_join = location_status_join(conn)
    active_clause = active_location_clause(conn, include_inactive=include_inactive)
    sql = f"""
    SELECT
        COALESCE(l.city, '') AS city,
        l.state,
        COUNT(*) AS locations,
        SUM(COALESCE(l.game_count, 0)) AS listed_games,
        COUNT(CASE WHEN l.location_id < 0 THEN 1 END) AS pinballmap_only_locations
    FROM locations l
    {status_join}
    WHERE l.state = ?
      AND {active_clause}
    GROUP BY COALESCE(l.city, ''), l.state
    ORDER BY listed_games DESC, locations DESC, l.city
    LIMIT ?
    """
    return run_query(conn, sql, (state, limit), title=f"City Summary: {state}")


def search_locations(conn: duckdb.DuckDBPyConnection, query: str, limit: int, include_inactive: bool = False) -> QueryResult:
    status_join = location_status_join(conn)
    active_clause = active_location_clause(conn, include_inactive=include_inactive)
    extra_columns = status_columns(conn)
    sql = f"""
    SELECT
        l.location_id, l.name, l.type, l.city, l.state, l.street_address, l.postal_code,
        l.game_count, l.source_url
        {extra_columns}
    FROM locations l
    {status_join}
    WHERE {active_clause}
      AND (
          lower(l.name) LIKE lower(?)
       OR lower(COALESCE(l.city, '')) LIKE lower(?)
       OR lower(COALESCE(l.street_address, '')) LIKE lower(?)
      )
    ORDER BY
        CASE WHEN lower(l.name) = lower(?) THEN 0 ELSE 1 END,
        l.game_count DESC,
        l.name
    LIMIT ?
    """
    like = f"%{query}%"
    result = run_query(conn, sql, (like, like, like, query, limit), title=f"Locations Matching: {query!r}")
    if len(result.rows) < limit:
        result = add_fuzzy_location_rows(conn, query, result, limit, include_inactive)
    return result


def add_fuzzy_location_rows(
    conn: duckdb.DuckDBPyConnection, query: str, result: QueryResult, limit: int, include_inactive: bool = False
) -> QueryResult:
    seen = {row["location_id"] for row in result.rows}
    status_join = location_status_join(conn)
    active_clause = active_location_clause(conn, include_inactive=include_inactive)
    extra_columns = status_columns(conn)
    candidates = rows_from_cursor(
        conn.execute(
            f"""
            SELECT l.location_id, l.name, l.type, l.city, l.state, l.street_address, l.postal_code,
                   l.game_count, l.source_url
                   {extra_columns}
            FROM locations l
            {status_join}
            WHERE {active_clause}
            """
        )
    )
    fuzzy = []
    for row in candidates:
        if row["location_id"] in seen:
            continue
        score = max(
            fuzzy_score(query, row.get("name") or ""),
            fuzzy_score(query, " ".join(str(row.get(part) or "") for part in ("name", "city", "street_address"))),
        )
        if score >= 0.62:
            fuzzy.append((score, row))
    fuzzy.sort(key=lambda item: (-item[0], -(item[1].get("game_count") or 0), item[1].get("name") or ""))
    rows = [*result.rows, *(row for _, row in fuzzy[: max(0, limit - len(result.rows))])]
    return QueryResult(result.title, result.columns, rows, result.notes)


def search_games(conn: duckdb.DuckDBPyConnection, query: str, limit: int, include_inactive: bool = False) -> QueryResult:
    status_join = location_status_join(conn)
    active_clause = active_location_clause(conn, include_inactive=include_inactive)
    sql = f"""
    WITH {game_identity_cte(conn)}
    SELECT
        gi.canonical_game_id AS game_id,
        gi.canonical_name AS name,
        gi.canonical_manufacturer AS manufacturer,
        COUNT(DISTINCT gi.game_id) AS source_game_rows,
        COUNT(CASE WHEN {active_clause} THEN lg.location_id END) AS location_count,
        COUNT(CASE WHEN l.state = 'UT' AND {active_clause} THEN 1 END) AS ut_location_count
    FROM game_identity gi
    LEFT JOIN location_games lg ON lg.game_id = gi.game_id
    LEFT JOIN locations l ON l.location_id = lg.location_id
    {status_join}
    WHERE lower(gi.source_name) LIKE lower(?)
       OR lower(gi.canonical_name) LIKE lower(?)
       OR lower(COALESCE(gi.source_manufacturer, '')) LIKE lower(?)
       OR lower(COALESCE(gi.canonical_manufacturer, '')) LIKE lower(?)
    GROUP BY gi.canonical_game_id, gi.canonical_name, gi.canonical_manufacturer
    ORDER BY ut_location_count DESC, location_count DESC, gi.canonical_name
    LIMIT ?
    """
    like = f"%{query}%"
    result = run_query(conn, sql, (like, like, like, like, limit), title=f"Games Matching: {query!r}")
    if len(result.rows) < limit:
        result = add_fuzzy_game_rows(conn, query, result, limit, include_inactive)
    return result


def add_fuzzy_game_rows(
    conn: duckdb.DuckDBPyConnection, query: str, result: QueryResult, limit: int, include_inactive: bool = False
) -> QueryResult:
    seen = {row["game_id"] for row in result.rows}
    status_join = location_status_join(conn)
    active_clause = active_location_clause(conn, include_inactive=include_inactive)
    candidates = rows_from_cursor(
        conn.execute(
            f"""
            WITH {game_identity_cte(conn)}
            SELECT
                gi.canonical_game_id AS game_id,
                gi.canonical_name AS name,
                gi.canonical_manufacturer AS manufacturer,
                COUNT(DISTINCT gi.game_id) AS source_game_rows,
                COUNT(CASE WHEN {active_clause} THEN lg.location_id END) AS location_count,
                COUNT(CASE WHEN l.state = 'UT' AND {active_clause} THEN 1 END) AS ut_location_count
            FROM game_identity gi
            LEFT JOIN location_games lg ON lg.game_id = gi.game_id
            LEFT JOIN locations l ON l.location_id = lg.location_id
            {status_join}
            GROUP BY gi.canonical_game_id, gi.canonical_name, gi.canonical_manufacturer
            """
        )
    )
    fuzzy = []
    for row in candidates:
        if row["game_id"] in seen:
            continue
        score = max(
            fuzzy_score(query, row.get("name") or ""),
            fuzzy_score(query, " ".join(str(row.get(part) or "") for part in ("name", "manufacturer"))),
        )
        if score >= 0.68:
            fuzzy.append((score, row))
    fuzzy.sort(
        key=lambda item: (
            -item[0],
            -(item[1].get("ut_location_count") or 0),
            -(item[1].get("location_count") or 0),
            item[1].get("name") or "",
        )
    )
    rows = [*result.rows, *(row for _, row in fuzzy[: max(0, limit - len(result.rows))])]
    return QueryResult(result.title, result.columns, rows, result.notes)


def where_to_play(
    conn: duckdb.DuckDBPyConnection, query: str, state: Optional[str], limit: int, include_inactive: bool = False
) -> QueryResult:
    game_matches = search_games(conn, query, 8, include_inactive).rows
    if not game_matches:
        return QueryResult(f"Where To Play: {query!r}", [], [], ["No matching games found."])
    game_ids = [row["game_id"] for row in game_matches]
    placeholders = ",".join("?" for _ in game_ids)
    state_clause = "AND l.state = ?" if state else ""
    params: list[Any] = [*game_ids]
    if state:
        params.append(state)
    params.append(limit)
    status_join = location_status_join(conn)
    active_clause = active_location_clause(conn, include_inactive=include_inactive)
    extra_columns = status_columns(conn)
    sql = f"""
    WITH {game_identity_cte(conn)}
    SELECT
        l.location_id, l.name AS location, l.city, l.state, l.street_address,
        gi.canonical_name AS game, gi.canonical_manufacturer AS manufacturer,
        gi.source_name AS source_game, lg.year, lg.cabinet_type,
        l.source_url
        {extra_columns}
    FROM location_games lg
    JOIN locations l ON l.location_id = lg.location_id
    JOIN game_identity gi ON gi.game_id = lg.game_id
    {status_join}
    WHERE gi.canonical_game_id IN ({placeholders})
    AND {active_clause}
    {state_clause}
    ORDER BY l.state, l.city, l.name, gi.canonical_name
    LIMIT ?
    """
    notes = [
        "Game candidates: "
        + ", ".join(f"{row['name']} ({row['game_id']})" for row in game_matches[:5])
    ]
    return run_query(conn, sql, params, title=f"Where To Play: {query!r}", notes=notes)


def inventory(conn: duckdb.DuckDBPyConnection, query: str, limit: int, include_inactive: bool = False) -> QueryResult:
    location = best_location(conn, query, include_inactive)
    if location is None:
        return QueryResult(f"Inventory: {query!r}", [], [], ["No matching location found."])
    sql = """
    SELECT
        g.name AS game, g.manufacturer, lg.year, lg.cabinet_type, g.game_id
    FROM location_games lg
    JOIN games g ON g.game_id = lg.game_id
    WHERE lg.location_id = ?
    ORDER BY
        CASE WHEN lg.cabinet_type = 'Pinball' THEN 0 ELSE 1 END,
        g.name
    LIMIT ?
    """
    notes = [location_note(location)]
    return run_query(conn, sql, (location["location_id"], limit), title=f"Inventory: {location['name']}", notes=notes)


def best_location(conn: duckdb.DuckDBPyConnection, query: str, include_inactive: bool = False) -> Optional[dict[str, Any]]:
    candidates = search_locations(conn, query, 10, include_inactive).rows
    if not candidates:
        return None
    best = max(
        candidates,
        key=lambda row: (
            fuzzy_score(query, row.get("name") or ""),
            row.get("game_count") or 0,
        ),
    )
    best_score = fuzzy_score(query, best.get("name") or "")
    if not include_inactive and has_table(conn, "location_statuses"):
        inactive_candidates = [
            row
            for row in search_locations(conn, query, 10, include_inactive=True).rows
            if row.get("status") not in ACTIVE_STATUSES
        ]
        if inactive_candidates:
            inactive_best = max(
                inactive_candidates,
                key=lambda row: (
                    fuzzy_score(query, row.get("name") or ""),
                    row.get("game_count") or 0,
                ),
            )
            if fuzzy_score(query, inactive_best.get("name") or "") > best_score + 0.08:
                return None
    if best_score < 0.72:
        return None
    return best


def location_note(location: dict[str, Any]) -> str:
    parts = [
        f"{location['name']} ({location['location_id']})",
        ", ".join(part for part in [location.get("city"), location.get("state")] if part),
    ]
    if location.get("street_address"):
        parts.append(location["street_address"])
    return "Location: " + " - ".join(part for part in parts if part)


def compare_locations(
    conn: duckdb.DuckDBPyConnection,
    left_query: str,
    right_query: str,
    limit: int,
    include_inactive: bool = False,
) -> QueryResult:
    left = best_location(conn, left_query, include_inactive)
    right = best_location(conn, right_query, include_inactive)
    if left is None or right is None:
        return QueryResult("Compare Locations", [], [], ["Could not resolve both locations."])
    sql = """
    WITH {game_identity},
    left_games AS (
        SELECT gi.canonical_game_id AS game_id, gi.canonical_name AS name, gi.canonical_manufacturer AS manufacturer
        FROM location_games lg
        JOIN game_identity gi ON gi.game_id = lg.game_id
        WHERE lg.location_id = ?
    ),
    right_games AS (
        SELECT gi.canonical_game_id AS game_id, gi.canonical_name AS name, gi.canonical_manufacturer AS manufacturer
        FROM location_games lg
        JOIN game_identity gi ON gi.game_id = lg.game_id
        WHERE lg.location_id = ?
    )
    SELECT 'shared' AS bucket, name, manufacturer, game_id
    FROM left_games
    WHERE game_id IN (SELECT game_id FROM right_games)
    UNION ALL
    SELECT 'only_left' AS bucket, name, manufacturer, game_id
    FROM left_games
    WHERE game_id NOT IN (SELECT game_id FROM right_games)
    UNION ALL
    SELECT 'only_right' AS bucket, name, manufacturer, game_id
    FROM right_games
    WHERE game_id NOT IN (SELECT game_id FROM left_games)
    ORDER BY bucket, name
    LIMIT ?
    """.format(game_identity=game_identity_cte(conn).strip())
    notes = [f"Left: {location_note(left)}", f"Right: {location_note(right)}"]
    return run_query(conn, sql, (left["location_id"], right["location_id"], limit), title="Compare Locations", notes=notes)


def rare_games(
    conn: duckdb.DuckDBPyConnection,
    state: Optional[str],
    max_locations: int,
    limit: int,
    include_inactive: bool = False,
) -> QueryResult:
    status_join = location_status_join(conn)
    active_clause = active_location_clause(conn, include_inactive=include_inactive)
    state_clause = "AND l.state = ?" if state else ""
    params: list[Any] = []
    if state:
        params.append(state)
    params.extend([max_locations, limit])
    sql = f"""
    WITH {game_identity_cte(conn)},
    scoped AS (
        SELECT gi.canonical_game_id AS game_id, gi.canonical_name AS name,
               gi.canonical_manufacturer AS manufacturer, l.location_id
        FROM game_identity gi
        JOIN location_games lg ON lg.game_id = gi.game_id
        JOIN locations l ON l.location_id = lg.location_id
        {status_join}
        WHERE {active_clause}
        {state_clause}
    )
    SELECT
        game_id, name, manufacturer,
        COUNT(DISTINCT location_id) AS location_count
    FROM scoped
    GROUP BY game_id, name, manufacturer
    HAVING COUNT(DISTINCT location_id) <= ?
    ORDER BY location_count, name
    LIMIT ?
    """
    scope = state or "all states"
    return run_query(conn, sql, params, title=f"Rare Games: {scope}")


def nearby(
    conn: duckdb.DuckDBPyConnection,
    lat: float,
    lon: float,
    miles: float,
    limit: int,
    include_inactive: bool = False,
) -> QueryResult:
    status_join = location_status_join(conn)
    active_clause = active_location_clause(conn, include_inactive=include_inactive)
    extra_columns = status_columns(conn)
    rows = rows_from_cursor(
        conn.execute(
            f"""
            SELECT l.location_id, l.name, l.type, l.city, l.state, l.street_address,
                   l.latitude, l.longitude, l.game_count, l.source_url
                   {extra_columns}
            FROM locations l
            {status_join}
            WHERE l.latitude IS NOT NULL AND l.longitude IS NOT NULL
              AND {active_clause}
            """
        )
    )
    nearby_rows = []
    for row in rows:
        distance = haversine_miles(lat, lon, float(row["latitude"]), float(row["longitude"]))
        if distance <= miles:
            row["miles"] = round(distance, 2)
            nearby_rows.append(row)
    nearby_rows.sort(key=lambda row: (row["miles"], -(row.get("game_count") or 0), row["name"]))
    columns = [
        "miles",
        "location_id",
        "name",
        "type",
        "city",
        "state",
        "street_address",
        "game_count",
        "source_url",
    ]
    if has_table(conn, "location_statuses"):
        columns.extend(["status", "replacement_name"])
    return QueryResult(
        f"Nearby Locations Within {miles:g} Miles",
        columns,
        nearby_rows[:limit],
        [f"Origin: {lat}, {lon}"],
    )


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_miles = 3958.7613
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return radius_miles * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def raw_sql(conn: duckdb.DuckDBPyConnection, sql: str, limit: Optional[int]) -> QueryResult:
    require_readonly_sql(sql)
    wrapped_sql = sql.strip().rstrip(";")
    if limit is not None and wrapped_sql.lower().startswith(("select", "with")):
        wrapped_sql = f"SELECT * FROM ({wrapped_sql}) AS raw_query LIMIT ?"
        return run_query(conn, wrapped_sql, (limit,), title="Raw SQL")
    return run_query(conn, wrapped_sql, title="Raw SQL")


def extract_location_ids(result: QueryResult) -> list[int]:
    ids: set[int] = set()
    for row in result.rows:
        value = row.get("location_id")
        if isinstance(value, int):
            ids.add(value)
        elif isinstance(value, str):
            try:
                ids.add(int(value))
            except ValueError:
                pass
    for note in result.notes:
        if not note.startswith(("Location:", "Left:", "Right:")):
            continue
        for match in LOCATION_ID_PATTERN.finditer(note):
            ids.add(int(match.group(1)))
    return sorted(ids)


def stale_cutoff(days: Optional[int]) -> Optional[str]:
    if days is None:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return cutoff.replace(microsecond=0).isoformat()


def location_ids_needing_verification(
    conn: duckdb.DuckDBPyConnection,
    location_ids: Sequence[int],
    stale_days: Optional[int],
) -> list[int]:
    if not location_ids:
        return []
    if not has_table(conn, "location_verifications"):
        return list(location_ids)
    placeholders = ",".join("?" for _ in location_ids)
    rows = rows_from_cursor(
        conn.execute(
            f"""
            SELECT location_id, MAX(checked_at) AS checked_at
            FROM location_verifications
            WHERE location_id IN ({placeholders})
            GROUP BY location_id
            """,
            location_ids,
        )
    )
    latest_by_id = {row["location_id"]: row["checked_at"] for row in rows}
    cutoff = stale_cutoff(stale_days)
    needs = []
    for location_id in location_ids:
        checked_at = latest_by_id.get(location_id)
        if checked_at is None:
            needs.append(location_id)
        elif cutoff is not None and checked_at < cutoff:
            needs.append(location_id)
    return needs


def should_lazy_verify(args: argparse.Namespace) -> bool:
    if args.command in {"sql", "summary", "verification-report", "inactive"}:
        return False
    return bool(args.verify_missing or args.verify_stale_days is not None)


def verify_locations_for_result(
    conn: duckdb.DuckDBPyConnection,
    args: argparse.Namespace,
    result: QueryResult,
) -> tuple[bool, list[str]]:
    if not should_lazy_verify(args):
        return False, []
    if args.db.suffix == ".duckdb":
        raise RuntimeError(
            "lazy OSM verification still writes through the legacy SQLite verifier; "
            "run sync_arcade_data.py validation or port verify_locations_osm.py before "
            "using --verify-missing/--verify-stale-days against DuckDB"
        )
    location_ids = extract_location_ids(result)
    if args.verify_limit is not None:
        location_ids = location_ids[: args.verify_limit]
    needs = location_ids_needing_verification(conn, location_ids, args.verify_stale_days)
    if not needs:
        if location_ids:
            return False, [f"Verification cache hit for {len(location_ids)} location(s)."]
        return False, ["No location ids were returned, so lazy verification did not run."]

    script_path = Path(__file__).with_name("verify_locations_osm.py")
    command = [
        sys.executable,
        str(script_path),
        "--db",
        str(args.db),
        "--apply",
        "--delay-seconds",
        str(args.verify_delay_seconds),
    ]
    if args.include_inactive:
        command.append("--include-inactive")
    for location_id in needs:
        command.extend(["--location-id", str(location_id)])
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        raise RuntimeError(f"lazy verification failed: {message}")
    verifier_lines = [
        line.strip()
        for line in completed.stdout.splitlines()
        if line.strip() and not line.startswith("checking ")
    ]
    notes = [f"Lazy verification checked {len(needs)} location(s); cache had {len(location_ids) - len(needs)} hit(s)."]
    notes.extend(f"Verification: {line}" for line in verifier_lines[:5])
    if len(verifier_lines) > 5:
        notes.append(f"Verification: ... {len(verifier_lines) - 5} more result(s).")
    return True, notes


def inactive_locations(conn: duckdb.DuckDBPyConnection, state: Optional[str], limit: int) -> QueryResult:
    if not has_table(conn, "location_statuses"):
        return QueryResult("Inactive Locations", [], [], ["location_statuses table does not exist yet."])
    state_clause = "AND l.state = ?" if state else ""
    params: list[Any] = []
    if state:
        params.append(state)
    params.append(limit)
    sql = f"""
    SELECT
        l.location_id, l.name, l.city, l.state, l.street_address,
        l.game_count, ls.status, ls.replacement_name, ls.confidence,
        ls.verified_at, ls.evidence
    FROM location_statuses ls
    JOIN locations l ON l.location_id = ls.location_id
    WHERE ls.status NOT IN ({",".join("?" for _ in ACTIVE_STATUSES)})
      {state_clause}
    ORDER BY l.state, l.city, l.name
    LIMIT ?
    """
    params = [*ACTIVE_STATUSES, *params]
    return run_query(conn, sql, params, title="Inactive Locations")


def verification_report(conn: duckdb.DuckDBPyConnection, state: Optional[str], limit: int) -> QueryResult:
    if not has_table(conn, "location_verifications"):
        return QueryResult("Location Verification Report", [], [], ["location_verifications table does not exist yet."])
    state_clause = "WHERE l.state = ?" if state else ""
    params: list[Any] = []
    if state:
        params.append(state)
    params.append(limit)
    sql = f"""
    WITH latest AS (
        SELECT
            lv.*,
            ROW_NUMBER() OVER (
                PARTITION BY lv.location_id
                ORDER BY lv.checked_at DESC, lv.verification_id DESC
            ) AS rn
        FROM location_verifications lv
    )
    SELECT
        l.location_id, l.name, l.city, l.state, l.street_address,
        COALESCE(ls.status, latest.status) AS status,
        latest.provider, latest.matched_name, latest.match_kind,
        latest.distance_miles, latest.confidence, latest.checked_at,
        ls.replacement_name
    FROM latest
    JOIN locations l ON l.location_id = latest.location_id
    LEFT JOIN location_statuses ls ON ls.location_id = l.location_id
    {state_clause}
      {"AND" if state else "WHERE"} latest.rn = 1
    ORDER BY
        CASE COALESCE(ls.status, latest.status)
            WHEN 'replaced' THEN 0
            WHEN 'closed' THEN 1
            WHEN 'possible_replaced' THEN 2
            WHEN 'not_found' THEN 3
            WHEN 'needs_review' THEN 4
            ELSE 5
        END,
        latest.confidence DESC,
        l.name
    LIMIT ?
    """
    return run_query(conn, sql, params, title="Location Verification Report")


def source_coverage(conn: duckdb.DuckDBPyConnection, state: Optional[str], limit: int) -> QueryResult:
    state_clause = "WHERE l.state = ?" if state else ""
    params: list[Any] = []
    if state:
        params.append(state)
    params.append(limit)
    sql = f"""
    SELECT
        l.state,
        COUNT(DISTINCT l.location_id) AS locations,
        COUNT(DISTINCT CASE WHEN l.location_id > 0 THEN l.location_id END) AS aurcade_locations,
        COUNT(DISTINCT CASE WHEN l.location_id BETWEEN -1999999999 AND -1000000000 THEN l.location_id END) AS pinballmap_only_locations,
        COUNT(DISTINCT CASE WHEN l.location_id BETWEEN -2999999999 AND -2000000000 THEN l.location_id END) AS ziv_only_locations,
        COUNT(DISTINCT pll.location_id) AS pinballmap_linked_locations,
        COUNT(DISTINCT zll.location_id) AS ziv_linked_locations,
        SUM(COALESCE(l.game_count, 0)) AS listed_game_count
    FROM locations l
    LEFT JOIN pinballmap_location_links pll ON pll.location_id = l.location_id
    LEFT JOIN ziv_location_links zll ON zll.location_id = l.location_id
    {state_clause}
    GROUP BY l.state
    ORDER BY locations DESC, l.state
    LIMIT ?
    """
    return run_query(conn, sql, params, title="Source Coverage")


def duplicate_leads(conn: duckdb.DuckDBPyConnection, state: Optional[str], limit: int) -> QueryResult:
    state_clause = "AND a.state = ?" if state else ""
    params: list[Any] = []
    if state:
        params.append(state)
    params.append(limit)
    sql = f"""
    SELECT
        a.state,
        a.city,
        a.location_id AS left_id,
        a.name AS left_name,
        a.street_address AS left_address,
        b.location_id AS right_id,
        b.name AS right_name,
        b.street_address AS right_address
    FROM locations a
    JOIN locations b ON b.location_id > a.location_id
        AND COALESCE(a.state, '') = COALESCE(b.state, '')
        AND COALESCE(a.city, '') = COALESCE(b.city, '')
        AND (
            lower(a.name) = lower(b.name)
            OR (
                a.street_address IS NOT NULL
                AND b.street_address IS NOT NULL
                AND lower(a.street_address) = lower(b.street_address)
            )
        )
    WHERE COALESCE(a.state, '') != ''
      {state_clause}
    ORDER BY a.state, a.city, a.name
    LIMIT ?
    """
    return run_query(conn, sql, params, title="Possible Duplicate Locations")


def review_queue(conn: duckdb.DuckDBPyConnection, state: Optional[str], limit: int) -> QueryResult:
    state_clause = "AND l.state = ?" if state else ""
    params: list[Any] = []
    if state:
        params.append(state)
    params.append(limit)
    sql = f"""
    WITH latest AS (
        SELECT
            lv.*,
            ROW_NUMBER() OVER (
                PARTITION BY lv.location_id, lv.provider
                ORDER BY lv.checked_at DESC, lv.verification_id DESC
            ) AS rn
        FROM location_verifications lv
    )
    SELECT
        l.location_id,
        l.name,
        l.city,
        l.state,
        l.game_count,
        COALESCE(ls.status, 'active') AS status,
        MAX(latest.checked_at) AS last_checked_at,
        GROUP_CONCAT(DISTINCT latest.provider) AS verification_sources
    FROM locations l
    LEFT JOIN location_statuses ls ON ls.location_id = l.location_id
    LEFT JOIN latest ON latest.location_id = l.location_id AND latest.rn = 1
    WHERE COALESCE(ls.status, 'active') NOT IN ('closed', 'replaced')
      {state_clause}
    GROUP BY l.location_id, l.name, l.city, l.state, l.game_count, COALESCE(ls.status, 'active')
    HAVING MAX(latest.checked_at) IS NULL OR COALESCE(l.game_count, 0) >= 25
    ORDER BY
        CASE WHEN last_checked_at IS NULL THEN 0 ELSE 1 END,
        COALESCE(l.game_count, 0) DESC,
        l.state,
        l.city,
        l.name
    LIMIT ?
    """
    return run_query(conn, sql, params, title="Review Queue")


def game_aliases(conn: duckdb.DuckDBPyConnection, query: Optional[str], limit: int) -> QueryResult:
    if not has_table(conn, "game_canonical_links"):
        return QueryResult("Game Canonical Links", [], [], ["game_canonical_links table does not exist yet."])
    query_clause = ""
    params: list[Any] = []
    if query:
        query_clause = """
        WHERE lower(alias.name) LIKE lower(?)
           OR lower(canonical.name) LIKE lower(?)
           OR lower(COALESCE(alias.manufacturer, '')) LIKE lower(?)
           OR lower(COALESCE(canonical.manufacturer, '')) LIKE lower(?)
        """
        like = f"%{query}%"
        params.extend([like, like, like, like])
    params.append(limit)
    sql = f"""
    SELECT
        gcl.alias_game_id,
        alias.name AS alias_name,
        gcl.canonical_game_id,
        canonical.name AS canonical_name,
        gcl.confidence,
        gcl.reason,
        gcl.source,
        gcl.updated_at
    FROM game_canonical_links gcl
    JOIN games alias ON alias.game_id = gcl.alias_game_id
    JOIN games canonical ON canonical.game_id = gcl.canonical_game_id
    {query_clause}
    ORDER BY gcl.confidence DESC, canonical.name, alias.name
    LIMIT ?
    """
    return run_query(conn, sql, params, title="Game Canonical Links")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Query the local arcade DuckDB database.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="DuckDB database path.")
    parser.add_argument(
        "--format",
        choices=("markdown", "json", "csv"),
        default="markdown",
        help="Output format.",
    )
    parser.add_argument(
        "--include-inactive",
        action="store_true",
        help="Include locations marked closed/replaced in canned query commands.",
    )
    parser.add_argument(
        "--verify-missing",
        action="store_true",
        help="For location-returning canned queries, verify returned locations that have no cached verification.",
    )
    parser.add_argument(
        "--verify-stale-days",
        type=int,
        help="Verify returned locations whose latest cached verification is older than this many days.",
    )
    parser.add_argument(
        "--verify-limit",
        type=int,
        default=10,
        help="Maximum returned location ids to lazily verify in one query.",
    )
    parser.add_argument(
        "--verify-delay-seconds",
        type=float,
        default=1.1,
        help="Delay between Nominatim probes when lazy verification runs.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("summary", help="Show high-level database counts.")

    city_parser = subparsers.add_parser("city-summary", help="Summarize locations by city.")
    city_parser.add_argument("--state", default="UT")
    city_parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)

    sql_parser = subparsers.add_parser("sql", help="Run one read-only SQL statement.")
    sql_parser.add_argument("sql")
    sql_parser.add_argument("--limit", type=int, default=100)

    locations_parser = subparsers.add_parser("locations", help="Search locations.")
    locations_parser.add_argument("query")
    locations_parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)

    games_parser = subparsers.add_parser("games", help="Search games/machines.")
    games_parser.add_argument("query")
    games_parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)

    where_parser = subparsers.add_parser("where", help="Find locations with a game.")
    where_parser.add_argument("game")
    where_parser.add_argument("--state", default="UT")
    where_parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)

    inventory_parser = subparsers.add_parser("inventory", help="List one location's inventory.")
    inventory_parser.add_argument("location")
    inventory_parser.add_argument("--limit", type=int, default=200)

    compare_parser = subparsers.add_parser("compare-locations", help="Compare two inventories.")
    compare_parser.add_argument("left")
    compare_parser.add_argument("right")
    compare_parser.add_argument("--limit", type=int, default=200)

    rare_parser = subparsers.add_parser("rare", help="Find games present at few locations.")
    rare_parser.add_argument("--state", default="UT")
    rare_parser.add_argument("--max-locations", type=int, default=1)
    rare_parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)

    nearby_parser = subparsers.add_parser("nearby", help="Find locations near a lat/lon.")
    nearby_parser.add_argument("--lat", type=float, required=True)
    nearby_parser.add_argument("--lon", type=float, required=True)
    nearby_parser.add_argument("--miles", type=float, default=20)
    nearby_parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)

    inactive_parser = subparsers.add_parser("inactive", help="List locations marked inactive/replaced/closed.")
    inactive_parser.add_argument("--state", default="UT")
    inactive_parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)

    verification_parser = subparsers.add_parser("verification-report", help="Show latest external verification results.")
    verification_parser.add_argument("--state", default="UT")
    verification_parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)

    coverage_parser = subparsers.add_parser("source-coverage", help="Summarize source provenance by state.")
    coverage_parser.add_argument("--state")
    coverage_parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)

    duplicate_parser = subparsers.add_parser("duplicate-leads", help="List likely duplicate locations.")
    duplicate_parser.add_argument("--state")
    duplicate_parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)

    review_parser = subparsers.add_parser("review-queue", help="List locations needing source/status review.")
    review_parser.add_argument("--state")
    review_parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)

    aliases_parser = subparsers.add_parser("game-aliases", help="List canonical game links.")
    aliases_parser.add_argument("query", nargs="?")
    aliases_parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)

    return parser


def normalize_argv(argv: Optional[Sequence[str]]) -> Optional[list[str]]:
    """Let global flags work before or after subcommands.

    argparse normally requires global flags before the subcommand. That is a
    little fussy for an exploratory helper, so move the known global options to
    the front before parsing.
    """
    if argv is None:
        argv = sys.argv[1:]
    items = list(argv)
    front: list[str] = []
    rest: list[str] = []
    index = 0
    global_options = {"--db", "--format", "--verify-stale-days", "--verify-limit", "--verify-delay-seconds"}
    global_flags = {"--include-inactive", "--verify-missing"}
    while index < len(items):
        item = items[index]
        if item in global_options and index + 1 < len(items):
            front.extend([item, items[index + 1]])
            index += 2
        elif item in global_flags:
            front.append(item)
            index += 1
        else:
            rest.append(item)
            index += 1
    return [*front, *rest]


def dispatch(conn: duckdb.DuckDBPyConnection, args: argparse.Namespace) -> QueryResult:
    if args.command == "summary":
        return summary(conn, args.include_inactive)
    if args.command == "city-summary":
        return city_summary(conn, args.state, args.limit, args.include_inactive)
    if args.command == "sql":
        return raw_sql(conn, args.sql, args.limit)
    if args.command == "locations":
        return search_locations(conn, args.query, args.limit, args.include_inactive)
    if args.command == "games":
        return search_games(conn, args.query, args.limit, args.include_inactive)
    if args.command == "where":
        return where_to_play(conn, args.game, args.state, args.limit, args.include_inactive)
    if args.command == "inventory":
        return inventory(conn, args.location, args.limit, args.include_inactive)
    if args.command == "compare-locations":
        return compare_locations(conn, args.left, args.right, args.limit, args.include_inactive)
    if args.command == "rare":
        return rare_games(conn, args.state, args.max_locations, args.limit, args.include_inactive)
    if args.command == "nearby":
        return nearby(conn, args.lat, args.lon, args.miles, args.limit, args.include_inactive)
    if args.command == "inactive":
        return inactive_locations(conn, args.state, args.limit)
    if args.command == "verification-report":
        return verification_report(conn, args.state, args.limit)
    if args.command == "source-coverage":
        return source_coverage(conn, args.state, args.limit)
    if args.command == "duplicate-leads":
        return duplicate_leads(conn, args.state, args.limit)
    if args.command == "review-queue":
        return review_queue(conn, args.state, args.limit)
    if args.command == "game-aliases":
        return game_aliases(conn, args.query, args.limit)
    raise ValueError(f"Unknown command: {args.command}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(normalize_argv(argv))
    try:
        conn = connect(args.db)
        try:
            result = dispatch(conn, args)
            verified, verification_notes = verify_locations_for_result(conn, args, result)
        finally:
            conn.close()
        if verified:
            conn = connect(args.db)
            try:
                result = dispatch(conn, args)
            finally:
                conn.close()
        if verification_notes:
            result = QueryResult(result.title, result.columns, result.rows, [*result.notes, *verification_notes])
        print(render(result, args.format))
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
