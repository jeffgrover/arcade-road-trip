"""Canonical game identity and source-provenance helpers.

The canonical ``games.game_id`` is intentionally independent of source IDs.
Source identifiers belong in ``game_source_records`` and may point at an
existing canonical game when an import is confidently matched.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import duckdb

from arcade_db import execute_script, has_table, rows


SOURCE_OFFSETS = {"pinballmap": 1_000_000_000, "ziv": 2_000_000_000}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    execute_script(conn, """
        CREATE TABLE IF NOT EXISTS game_source_records (
            source VARCHAR NOT NULL,
            source_game_id BIGINT NOT NULL,
            game_id BIGINT NOT NULL,
            source_name VARCHAR,
            source_manufacturer VARCHAR,
            first_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (source, source_game_id)
        );
        CREATE INDEX IF NOT EXISTS idx_game_source_records_game
            ON game_source_records(game_id);
        ALTER TABLE game_source_records ADD COLUMN IF NOT EXISTS legacy_game_id BIGINT;
        CREATE TABLE IF NOT EXISTS game_identity_sequence (
            sequence_name VARCHAR PRIMARY KEY,
            next_game_id BIGINT NOT NULL
        );
    """)


def source_for_legacy_id(game_id: int) -> tuple[str, int] | None:
    if game_id > 0:
        return "aurcade", game_id
    for source, offset in SOURCE_OFFSETS.items():
        if -offset - 999_999_999 <= game_id <= -offset:
            return source, abs(game_id) - offset
    # Preserve locally-created or previously unclassified IDs as provenance
    # too. They are not eligible for source-based automatic merging.
    return "legacy", game_id


def canonical_id(conn: duckdb.DuckDBPyConnection, game_id: int) -> int:
    """Resolve old canonical links transitively while they still exist."""
    if not has_table(conn, "game_canonical_links"):
        return int(game_id)

    seen: set[int] = set()
    current = int(game_id)
    while current not in seen:
        seen.add(current)
        row = conn.execute(
            "SELECT canonical_game_id FROM game_canonical_links WHERE alias_game_id = ?",
            (current,),
        ).fetchone()
        if not row or int(row[0]) == current:
            return current
        current = int(row[0])
    raise ValueError(f"cycle in game canonical links involving {game_id}")


def source_game_id(conn: duckdb.DuckDBPyConnection, source: str, source_id: int) -> Optional[int]:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT game_id FROM game_source_records WHERE source = ? AND source_game_id = ?",
        (source, int(source_id)),
    ).fetchone()
    return int(row[0]) if row else None


def allocate_game_id(conn: duckdb.DuckDBPyConnection) -> int:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT next_game_id FROM game_identity_sequence WHERE sequence_name = 'games'"
    ).fetchone()
    if row:
        game_id = int(row[0])
        conn.execute(
            "UPDATE game_identity_sequence SET next_game_id = ? WHERE sequence_name = 'games'",
            (game_id + 1,),
        )
        return game_id
    max_positive = conn.execute("SELECT COALESCE(MAX(game_id), 0) FROM games WHERE game_id > 0").fetchone()[0]
    game_id = max(10_000_000, int(max_positive) + 1)
    conn.execute(
        "INSERT INTO game_identity_sequence(sequence_name, next_game_id) VALUES ('games', ?)",
        (game_id + 1,),
    )
    return game_id


def attach_source_record(
    conn: duckdb.DuckDBPyConnection,
    source: str,
    source_id: int,
    game_id: int,
    name: Optional[str] = None,
    manufacturer: Optional[str] = None,
    legacy_game_id: Optional[int] = None,
) -> int:
    """Attach a source row to a canonical game without creating an alias row."""
    ensure_schema(conn)
    timestamp = utc_now()
    existing = source_game_id(conn, source, source_id)
    if existing is not None and existing != game_id:
        raise ValueError(f"source record {source}:{source_id} already maps to game {existing}")
    conn.execute("""
        INSERT INTO game_source_records
            (source, source_game_id, game_id, source_name, source_manufacturer, legacy_game_id, first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (source, source_game_id) DO UPDATE SET
            game_id=excluded.game_id,
            source_name=COALESCE(excluded.source_name, game_source_records.source_name),
            source_manufacturer=COALESCE(excluded.source_manufacturer, game_source_records.source_manufacturer),
            legacy_game_id=COALESCE(excluded.legacy_game_id, game_source_records.legacy_game_id),
            last_seen_at=excluded.last_seen_at
    """, (source, int(source_id), int(game_id), name, manufacturer, legacy_game_id, timestamp, timestamp))
    return int(game_id)


def resolve_source_game(
    conn: duckdb.DuckDBPyConnection,
    source: str,
    source_id: int,
    name: str,
    manufacturer: Optional[str] = None,
    matched_game_id: Optional[int] = None,
) -> int:
    """Return an existing canonical ID or allocate a new positive internal ID."""
    ensure_schema(conn)
    existing = source_game_id(conn, source, source_id)
    if existing is not None:
        return existing
    game_id = canonical_id(conn, matched_game_id) if matched_game_id is not None else allocate_game_id(conn)
    if not conn.execute("SELECT 1 FROM games WHERE game_id = ?", (game_id,)).fetchone():
        conn.execute("INSERT INTO games(game_id, name, manufacturer) VALUES (?, ?, ?)", (game_id, name, manufacturer))
    else:
        conn.execute("""
            UPDATE games SET name=COALESCE(name, ?), manufacturer=COALESCE(manufacturer, ?)
            WHERE game_id = ?
        """, (name, manufacturer, game_id))
    return attach_source_record(conn, source, source_id, game_id, name, manufacturer)


def migrate_existing_records(conn: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Backfill provenance for current rows without deleting or repointing data."""
    ensure_schema(conn)
    migrated = 0
    skipped = 0
    for row in rows(conn, "SELECT game_id, name, manufacturer FROM games ORDER BY game_id"):
        legacy = source_for_legacy_id(int(row["game_id"]))
        if legacy is None:
            skipped += 1
            continue
        source, source_id = legacy
        target = canonical_id(conn, int(row["game_id"]))
        attach_source_record(
            conn, source, source_id, target, row["name"], row["manufacturer"],
            legacy_game_id=int(row["game_id"]),
        )
        migrated += 1
    conn.execute("""
        INSERT INTO game_identity_sequence(sequence_name, next_game_id)
        SELECT 'games', GREATEST(10000000, COALESCE(MAX(game_id), 0) + 1)
        FROM games WHERE game_id > 0
        ON CONFLICT (sequence_name) DO NOTHING
    """)
    return {"migrated": migrated, "skipped": skipped}
