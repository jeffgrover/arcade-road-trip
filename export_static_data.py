#!/usr/bin/env python3
"""Export browser-queryable Arcade Road Trip Parquet snapshots.

These builders are shared by the one-file static atlas generator. Running this
module directly writes the intermediate Parquet bundle for inspection.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import duckdb

from us_states import CONTINENTAL_US_STATES


DEFAULT_DB = Path("arcade_roadtrip.duckdb")
DEFAULT_OUTPUT_DIR = Path("static/data")
ACTIVE_STATUSES = ("active", "unverified", "uncertain", "matched", "needs_review")


def connect(db_path: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(db_path), read_only=True)


def has_table(conn: duckdb.DuckDBPyConnection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE lower(table_name) = lower(?)",
        (table_name,),
    ).fetchone()
    return row is not None


def game_identity_cte(conn: duckdb.DuckDBPyConnection) -> str:
    if has_table(conn, "game_canonical_links"):
        return """
        game_identity AS (
            SELECT
                g.game_id,
                g.name,
                COALESCE(gcl.canonical_game_id, g.game_id) AS canonical_game_id,
                COALESCE(cg.name, g.name) AS canonical_name
            FROM games g
            LEFT JOIN game_canonical_links gcl ON gcl.alias_game_id = g.game_id
            LEFT JOIN games cg ON cg.game_id = gcl.canonical_game_id
        )
        """
    return """
    game_identity AS (
        SELECT g.game_id, g.name, g.game_id AS canonical_game_id, g.name AS canonical_name
        FROM games g
    )
    """


def placeholders(values: Iterable[Any]) -> str:
    return ",".join("?" for _ in values)


def rows(conn: duckdb.DuckDBPyConnection, sql: str, params: Iterable[Any]) -> list[dict[str, Any]]:
    result = conn.execute(sql, list(params))
    columns = [description[0] for description in result.description]
    return [dict(zip(columns, row)) for row in result.fetchall()]


def load_route_locations(conn: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    status_sql = placeholders(ACTIVE_STATUSES)
    state_sql = placeholders(CONTINENTAL_US_STATES)
    params = [*ACTIVE_STATUSES, *CONTINENTAL_US_STATES]
    sql = f"""
    WITH {game_identity_cte(conn)},
    active_locations AS (
        SELECT
            l.location_id,
            l.name,
            COALESCE(l.city, '') AS city,
            COALESCE(l.state, '') AS state,
            COALESCE(l.street_address, '') AS street_address,
            l.latitude,
            l.longitude,
            COALESCE(ls.status, 'active') AS status
        FROM locations l
        LEFT JOIN location_statuses ls ON ls.location_id = l.location_id
        WHERE COALESCE(ls.status, 'active') IN ({status_sql})
          AND l.state IN ({state_sql})
          AND l.latitude IS NOT NULL
          AND l.longitude IS NOT NULL
    )
    SELECT
        al.location_id,
        al.name,
        al.city,
        al.state,
        al.street_address,
        CAST(al.latitude AS REAL) AS latitude,
        CAST(al.longitude AS REAL) AS longitude,
        al.status,
        COUNT(lg.game_id) AS game_count,
        COUNT(DISTINCT gi.canonical_game_id) AS unique_game_count,
        SUM(CASE WHEN lg.cabinet_type = 'Pinball' THEN 1 ELSE 0 END) AS pinball_games,
        SUM(CASE WHEN lower(COALESCE(lg.cabinet_type, '')) IN ('music game', 'rhythm')
                  OR lower(gi.name) LIKE '%dance%'
                  OR lower(gi.name) LIKE '%pump it up%'
                  OR lower(gi.name) LIKE '%sound voltex%'
                 THEN 1 ELSE 0 END) AS rhythm_games,
        TRIM(
            (CASE WHEN pll.location_id IS NOT NULL THEN 'Pinball Map ' ELSE '' END) ||
            (CASE WHEN zll.location_id IS NOT NULL THEN 'ZIv ' ELSE '' END)
        ) AS source_tags
    FROM active_locations al
    LEFT JOIN location_games lg ON lg.location_id = al.location_id
    LEFT JOIN game_identity gi ON gi.game_id = lg.game_id
    LEFT JOIN pinballmap_location_links pll ON pll.location_id = al.location_id
    LEFT JOIN ziv_location_links zll ON zll.location_id = al.location_id
    GROUP BY
        al.location_id,
        al.name,
        al.city,
        al.state,
        al.street_address,
        al.latitude,
        al.longitude,
        al.status,
        pll.location_id,
        zll.location_id
    HAVING COUNT(lg.game_id) > 0
    """
    return rows(conn, sql, params)


def load_location_games(conn: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    status_sql = placeholders(ACTIVE_STATUSES)
    state_sql = placeholders(CONTINENTAL_US_STATES)
    params = [
        *ACTIVE_STATUSES,
        *CONTINENTAL_US_STATES,
        *ACTIVE_STATUSES,
        *CONTINENTAL_US_STATES,
        *ACTIVE_STATUSES,
        *CONTINENTAL_US_STATES,
    ]
    sql = f"""
    WITH {game_identity_cte(conn)},
    active_locations AS (
        SELECT l.location_id, l.state
        FROM locations l
        LEFT JOIN location_statuses ls ON ls.location_id = l.location_id
        WHERE COALESCE(ls.status, 'active') IN ({status_sql})
          AND l.state IN ({state_sql})
    ),
    us_counts AS (
        SELECT gi.canonical_game_id, COUNT(DISTINCT lg.location_id) AS us_location_count
        FROM location_games lg
        JOIN game_identity gi ON gi.game_id = lg.game_id
        JOIN locations l ON l.location_id = lg.location_id
        LEFT JOIN location_statuses ls ON ls.location_id = l.location_id
        WHERE COALESCE(ls.status, 'active') IN ({status_sql})
          AND l.state IN ({state_sql})
        GROUP BY gi.canonical_game_id
    ),
    state_counts AS (
        SELECT gi.canonical_game_id, l.state, COUNT(DISTINCT lg.location_id) AS state_location_count
        FROM location_games lg
        JOIN game_identity gi ON gi.game_id = lg.game_id
        JOIN locations l ON l.location_id = lg.location_id
        LEFT JOIN location_statuses ls ON ls.location_id = l.location_id
        WHERE COALESCE(ls.status, 'active') IN ({status_sql})
          AND l.state IN ({state_sql})
        GROUP BY gi.canonical_game_id, l.state
    )
    SELECT
        lg.location_id,
        al.state AS location_state,
        lg.game_id,
        gi.name,
        gi.canonical_game_id,
        gi.canonical_name,
        COALESCE(lg.cabinet_type, '') AS cabinet_type,
        COALESCE(uc.us_location_count, 0) AS us_location_count,
        COALESCE(sc.state_location_count, 0) AS state_location_count,
        CASE WHEN COALESCE(uc.us_location_count, 0) < 10 THEN 1 ELSE 0 END AS rare_us,
        CASE WHEN COALESCE(sc.state_location_count, 0) = 1 THEN 1 ELSE 0 END AS unique_state
    FROM location_games lg
    JOIN active_locations al ON al.location_id = lg.location_id
    JOIN game_identity gi ON gi.game_id = lg.game_id
    LEFT JOIN us_counts uc ON uc.canonical_game_id = gi.canonical_game_id
    LEFT JOIN state_counts sc ON sc.canonical_game_id = gi.canonical_game_id AND sc.state = al.state
    """
    return rows(conn, sql, params)


def write_parquet(records: list[dict[str, Any]], output_path: Path) -> None:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise SystemExit(
            "Missing dependency: pyarrow. Run `python3 -m pip install -r requirements.txt` "
            "or install pyarrow in your active environment."
        ) from exc

    table = pa.Table.from_pylist(records)
    pq.write_table(table, output_path, compression="zstd")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export static DuckDB-WASM Parquet data.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with connect(args.db) as conn:
        route_locations = load_route_locations(conn)
        location_games = load_location_games(conn)

    route_path = args.output_dir / "route_locations.parquet"
    games_path = args.output_dir / "location_games.parquet"
    write_parquet(route_locations, route_path)
    write_parquet(location_games, games_path)

    manifest = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "files": {
            "route_locations": route_path.name,
            "location_games": games_path.name,
        },
        "counts": {
            "route_locations": len(route_locations),
            "location_games": len(location_games),
        },
    }
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"wrote {route_path} ({len(route_locations)} locations)")
    print(f"wrote {games_path} ({len(location_games)} game placements)")
    print(f"wrote {args.output_dir / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
