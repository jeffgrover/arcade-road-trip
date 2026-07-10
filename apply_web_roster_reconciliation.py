#!/usr/bin/env python3
"""Apply one reviewed owner-published roster reconciliation to DuckDB."""

from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from arcade_db import DEFAULT_DUCKDB, connect as duckdb_connect
from canonicalize_games import source_rank
from scan_arcade_web_rosters import normalize_game_name


OWNER_ROSTER_GAME_ID_START = -4000000000
REPORT_DIR = Path("reports")
STOPWORDS = {"a", "an", "and", "of", "the"}
MANUAL_GAME_RESOLUTIONS = {
    "rapid fire": "rapid fire|bally",
}


@dataclass(frozen=True)
class GameMatch:
    game_id: int
    name: str
    manufacturer: str
    location_count: int
    score: float
    reason: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def title_key(value: str) -> str:
    tokens = [token for token in normalize_game_name(value).split() if token not in STOPWORDS]
    return " ".join(tokens)


def strict_key(value: str) -> str:
    return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in value).split())


def compact(value: str) -> str:
    return strict_key(value).replace(" ", "")


def token_set(value: str) -> set[str]:
    return set(title_key(value).split())


def strict_token_set(value: str) -> set[str]:
    return {token for token in strict_key(value).split() if token not in STOPWORDS}


def similarity(left: str, right: str) -> tuple[float, str]:
    left_strict = strict_key(left)
    right_strict = strict_key(right)
    if left_strict == right_strict:
        return 1.0, "strict_exact"
    left_tokens = token_set(left)
    right_tokens = token_set(right)
    left_strict_tokens = strict_token_set(left)
    right_strict_tokens = strict_token_set(right)
    if "remake" in left_strict_tokens and ({"remake", "cgc", "chicago", "gaming"} & right_strict_tokens) and {"medieval", "madness"} <= right_strict_tokens:
        return 1.0, "remake_compatible"
    if title_key(left) == title_key(right):
        if left_strict_tokens != right_strict_tokens:
            return 0.97, "title_key_extra_qualifier"
        return 0.99, "title_key_exact"
    if compact(left) == compact(right):
        return 0.98, "compact_exact"
    if left_tokens and left_tokens == right_tokens:
        return 0.97, "token_set_exact"
    if left_tokens and left_tokens < right_tokens:
        return 0.95, "owner_tokens_subset"
    left_norm = normalize_game_name(left)
    right_norm = normalize_game_name(right)
    if left_norm == right_norm:
        return 0.94, "normalized_base_match"
    if len(left_strict) >= 4 and right_strict.startswith(left_strict):
        return 0.92, "owner_title_prefix"
    if len(right_strict) >= 4 and left_strict.startswith(right_strict):
        return 0.92, "db_title_prefix"
    if title_key(left) and title_key(left) in title_key(right):
        return 0.9, "owner_title_contained"
    return 0.0, ""


def ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS web_roster_reconciliation_actions (
            applied_at VARCHAR,
            location_id BIGINT,
            location_name VARCHAR,
            roster_url VARCHAR,
            action VARCHAR,
            status VARCHAR,
            website_name VARCHAR,
            game_id BIGINT,
            db_name VARCHAR,
            notes VARCHAR
        )
        """
    )


def load_games(conn: duckdb.DuckDBPyConnection) -> list[GameMatch]:
    rows = conn.execute(
        """
        SELECT
            g.game_id,
            g.name,
            COALESCE(g.manufacturer, '') AS manufacturer,
            COUNT(DISTINCT lg.location_id) AS location_count
        FROM games g
        LEFT JOIN location_games lg USING(game_id)
        GROUP BY g.game_id, g.name, g.manufacturer
        """
    ).fetchall()
    return [GameMatch(int(row[0]), str(row[1]), str(row[2] or ""), int(row[3] or 0), 0.0, "") for row in rows]


def resolve_game(games: list[GameMatch], name: str) -> GameMatch | None:
    manual_key = MANUAL_GAME_RESOLUTIONS.get(strict_key(name))
    matches: list[GameMatch] = []
    for game in games:
        score, reason = similarity(name, game.name)
        if manual_key == f"{strict_key(game.name)}|{strict_key(game.manufacturer)}":
            score, reason = 1.01, "manual_resolution"
        if score >= 0.9:
            matches.append(
                GameMatch(
                    game_id=game.game_id,
                    name=game.name,
                    manufacturer=game.manufacturer,
                    location_count=game.location_count,
                    score=score,
                    reason=reason,
                )
            )
    if not matches:
        return None
    return sorted(matches, key=lambda game: (-game.score, source_rank(game.game_id), -game.location_count, game.name.lower()))[0]


def common_cabinet_type(conn: duckdb.DuckDBPyConnection, game_id: int) -> str | None:
    row = conn.execute(
        """
        SELECT cabinet_type, COUNT(*) AS rows
        FROM location_games
        WHERE game_id = ? AND COALESCE(cabinet_type, '') <> ''
        GROUP BY cabinet_type
        ORDER BY rows DESC, cabinet_type
        LIMIT 1
        """,
        (game_id,),
    ).fetchone()
    return str(row[0]) if row else None


def next_owner_roster_game_id(conn: duckdb.DuckDBPyConnection) -> int:
    row = conn.execute(
        "SELECT MIN(game_id) FROM games WHERE game_id <= ?",
        (OWNER_ROSTER_GAME_ID_START,),
    ).fetchone()
    current_min = int(row[0]) if row and row[0] is not None else OWNER_ROSTER_GAME_ID_START + 1
    return min(OWNER_ROSTER_GAME_ID_START, current_min - 1)


def read_reconciliation(path: Path, location_id: int) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    for item in data.get("reconciliations", []):
        if int(item["location_id"]) == location_id:
            return item
    raise ValueError(f"location_id {location_id} was not found in {path}")


def backup_database(db_path: Path) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    backup_path = REPORT_DIR / f"{db_path.stem}_before_web_roster_{datetime.now().strftime('%Y%m%d_%H%M%S')}{db_path.suffix}"
    shutil.copy2(db_path, backup_path)
    return backup_path


def action_rows(reconciliation: dict[str, Any], action: str, status: str, website_name: str, game_id: int | None, db_name: str, notes: str, applied_at: str) -> tuple[Any, ...]:
    return (
        applied_at,
        int(reconciliation["location_id"]),
        reconciliation["name"],
        reconciliation.get("roster_url", ""),
        action,
        status,
        website_name,
        game_id,
        db_name,
        notes,
    )


def apply_reconciliation(conn: duckdb.DuckDBPyConnection, reconciliation: dict[str, Any], apply: bool) -> list[tuple[Any, ...]]:
    if apply:
        ensure_schema(conn)
    applied_at = now_iso()
    location_id = int(reconciliation["location_id"])
    games = load_games(conn)
    audit_rows: list[tuple[Any, ...]] = []

    for name in reconciliation.get("add_candidates", []):
        match = resolve_game(games, name)
        if match is None:
            game_id = next_owner_roster_game_id(conn) if apply else None
            audit_rows.append(action_rows(reconciliation, "add", "created_game" if apply else "would_create_game", name, game_id, name, "owner roster title not found globally", applied_at))
            if apply:
                conn.execute("INSERT INTO games(game_id, name, manufacturer) VALUES (?, ?, NULL)", (game_id, name))
                conn.execute(
                    """
                    INSERT INTO location_games (
                        location_id, game_id, cabinet_type, year, players,
                        controls_condition, screen_condition, cabinet_condition, fetched_at
                    )
                    VALUES (?, ?, NULL, NULL, NULL, NULL, NULL, NULL, ?)
                    """,
                    (location_id, game_id, applied_at),
                )
                games.append(GameMatch(game_id, name, "", 1, 1.0, "created_owner_roster_game"))
            continue
        cabinet_type = common_cabinet_type(conn, match.game_id)
        already_present = conn.execute(
            "SELECT 1 FROM location_games WHERE location_id = ? AND game_id = ?",
            (location_id, match.game_id),
        ).fetchone()
        if already_present:
            audit_rows.append(action_rows(reconciliation, "add", "already_present", name, match.game_id, match.name, f"{match.reason}; cabinet_type={cabinet_type or 'unknown'}", applied_at))
            continue
        audit_rows.append(action_rows(reconciliation, "add", "inserted" if apply else "would_insert", name, match.game_id, match.name, f"{match.reason}; cabinet_type={cabinet_type or 'unknown'}", applied_at))
        if apply:
            conn.execute(
                """
                INSERT INTO location_games (
                    location_id, game_id, cabinet_type, year, players,
                    controls_condition, screen_condition, cabinet_condition, fetched_at
                )
                SELECT ?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?
                WHERE NOT EXISTS (
                    SELECT 1 FROM location_games WHERE location_id = ? AND game_id = ?
                )
                """,
                (location_id, match.game_id, cabinet_type, applied_at, location_id, match.game_id),
            )

    for name in reconciliation.get("remove_candidates", []):
        row = conn.execute(
            """
            SELECT lg.game_id, g.name
            FROM location_games lg
            JOIN games g USING(game_id)
            WHERE lg.location_id = ? AND g.name = ?
            """,
            (location_id, name),
        ).fetchone()
        if not row:
            audit_rows.append(action_rows(reconciliation, "remove", "missing_current_row", "", None, name, "no exact current location row found", applied_at))
            continue
        game_id = int(row[0])
        audit_rows.append(action_rows(reconciliation, "remove", "deleted" if apply else "would_delete", "", game_id, str(row[1]), "owner roster omitted this current DB placement", applied_at))
        if apply:
            conn.execute("DELETE FROM location_games WHERE location_id = ? AND game_id = ?", (location_id, game_id))

    for candidate in reconciliation.get("canonical_candidates", []):
        audit_rows.append(action_rows(reconciliation, "canonical", "reviewed", candidate["website_name"], None, candidate["db_name"], f"similarity={candidate['similarity']}", applied_at))

    if apply and audit_rows:
        conn.executemany(
            """
            INSERT INTO web_roster_reconciliation_actions (
                applied_at, location_id, location_name, roster_url, action, status,
                website_name, game_id, db_name, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            audit_rows,
        )
        conn.execute(
            """
            UPDATE locations
            SET
                game_count = counts.game_count,
                unique_game_count = counts.unique_game_count
            FROM (
                SELECT
                    COUNT(*) AS game_count,
                    COUNT(DISTINCT game_id) AS unique_game_count
                FROM location_games
                WHERE location_id = ?
            ) counts
            WHERE location_id = ?
            """,
            (location_id, location_id),
        )
    return audit_rows


def write_summary(rows: list[tuple[Any, ...]], report_dir: Path) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"web_roster_apply_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    lines = [
        "# Web Roster Apply Summary",
        "",
        "| action | status | website_name | game_id | db_name | notes |",
        "| --- | --- | --- | ---: | --- | --- |",
    ]
    for row in rows:
        _, _, _, _, action, status, website_name, game_id, db_name, notes = row
        safe = [str(value or "").replace("|", "\\|") for value in (action, status, website_name, game_id, db_name, notes)]
        lines.append("| " + " | ".join(safe) + " |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apply one reviewed web-roster reconciliation plan.")
    parser.add_argument("--reconciliation-report", type=Path, required=True)
    parser.add_argument("--location-id", type=int, required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_DUCKDB)
    parser.add_argument("--report-dir", type=Path, default=REPORT_DIR)
    parser.add_argument("--apply", action="store_true", help="Write database changes. Default is dry-run plus audit preview.")
    parser.add_argument("--backup", action="store_true", help="Copy the DB before applying.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    reconciliation = read_reconciliation(args.reconciliation_report, args.location_id)
    backup_path = backup_database(args.db) if args.apply and args.backup else None
    conn = duckdb_connect(args.db, read_only=not args.apply)
    rows = apply_reconciliation(conn, reconciliation, apply=args.apply)
    summary_path = write_summary(rows, args.report_dir)
    print(f"mode={'apply' if args.apply else 'dry-run'}")
    if backup_path:
        print(f"backup={backup_path}")
    print(f"wrote {summary_path}")
    print(f"actions={len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
