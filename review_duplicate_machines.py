#!/usr/bin/env python3
"""Build and record a conservative, clustered duplicate-review queue.

This command is intentionally review-only.  It never rewrites placements or
deletes games.  Review decisions can be recorded separately and consumed by a
later, explicitly-approved consolidation command.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import duckdb

from arcade_db import DEFAULT_DUCKDB, execute_script, rows


VARIANT_WORDS = {
    "2p", "4p", "6p", "dx", "plus", "premium", "pro", "se", "le",
    "deluxe", "limited", "edition", "remix", "super", "world", "version",
}
AMBIGUOUS_NAMES = {
    "batman", "basketball", "black hole", "circus", "defender", "dragon",
    "galaxy", "godzilla", "hook", "pinball", "flipper", "rampage",
    "star wars", "street fighter ii", "the simpsons", "touchdown",
}


@dataclass(frozen=True)
class GameRecord:
    game_id: int
    name: str
    manufacturer: str
    placement_count: int
    active_placement_count: int
    cabinet_types: tuple[str, ...]
    years: tuple[int, ...]
    source: str


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize(value: str) -> str:
    value = (value or "").lower().replace("&", " and ")
    value = re.sub(r"\bac\s*/?\s*dc\b", "acdc", value)
    return " ".join(re.findall(r"[a-z0-9]+", value))


def compact(value: str) -> str:
    tokens = normalize(value).split()
    if tokens[:1] == ["the"]:
        tokens = tokens[1:]
    if tokens[-1:] == ["the"]:
        tokens = tokens[:-1]
    return "".join(tokens)


def source_for(game_id: int) -> str:
    if game_id > 0:
        return "aurcade"
    if -1_999_999_999 <= game_id <= -1_000_000_000:
        return "pinballmap"
    if -2_999_999_999 <= game_id <= -2_000_000_000:
        return "ziv"
    return "other"


def ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    execute_script(conn, """
        CREATE TABLE IF NOT EXISTS duplicate_review_candidates (
            candidate_id BIGINT PRIMARY KEY,
            cluster_key VARCHAR UNIQUE NOT NULL,
            match_score DOUBLE NOT NULL,
            affected_placements BIGINT NOT NULL,
            priority_score DOUBLE NOT NULL,
            status VARCHAR NOT NULL DEFAULT 'pending',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS duplicate_review_records (
            candidate_id BIGINT NOT NULL,
            game_id BIGINT NOT NULL,
            name VARCHAR NOT NULL,
            manufacturer VARCHAR,
            placement_count BIGINT NOT NULL,
            active_placement_count BIGINT NOT NULL,
            cabinet_types VARCHAR,
            years VARCHAR,
            source VARCHAR NOT NULL,
            PRIMARY KEY (candidate_id, game_id)
        );
        CREATE TABLE IF NOT EXISTS duplicate_review_decisions (
            candidate_id BIGINT NOT NULL,
            decision VARCHAR NOT NULL,
            notes VARCHAR,
            decided_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
    """)


def load_games(conn: duckdb.DuckDBPyConnection) -> list[GameRecord]:
    status_join = "LEFT JOIN location_statuses ls ON ls.location_id = lg.location_id"
    active = "COALESCE(ls.status, 'active') NOT IN ('closed', 'replaced')"
    data = rows(conn, f"""
        SELECT g.game_id, g.name, COALESCE(g.manufacturer, '') manufacturer,
               COUNT(lg.location_id) placement_count,
               COUNT(lg.location_id) FILTER (WHERE {active}) active_placement_count,
               string_agg(DISTINCT NULLIF(lower(lg.cabinet_type), ''), '|') cabinet_types,
               string_agg(DISTINCT CAST(lg.year AS VARCHAR), '|') years
        FROM games g
        LEFT JOIN location_games lg ON lg.game_id = g.game_id
        {status_join}
        GROUP BY g.game_id, g.name, g.manufacturer
    """)
    result = []
    for row in data:
        cabinet_types = tuple(sorted(x for x in (row["cabinet_types"] or "").split("|") if x))
        years = tuple(sorted(int(x) for x in (row["years"] or "").split("|") if x and x.isdigit()))
        result.append(GameRecord(
            game_id=int(row["game_id"]), name=str(row["name"] or ""),
            manufacturer=str(row["manufacturer"] or ""),
            placement_count=int(row["placement_count"] or 0),
            active_placement_count=int(row["active_placement_count"] or 0),
            cabinet_types=cabinet_types, years=years, source=source_for(int(row["game_id"])),
        ))
    return [game for game in result if game.name.strip()]


def compatible(left: GameRecord, right: GameRecord) -> bool:
    if left.cabinet_types and right.cabinet_types and not set(left.cabinet_types) & set(right.cabinet_types):
        return False
    left_maker, right_maker = normalize(left.manufacturer), normalize(right.manufacturer)
    return not left_maker or not right_maker or left_maker == right_maker


def pair_score(left: GameRecord, right: GameRecord) -> float:
    left_norm, right_norm = normalize(left.name), normalize(right.name)
    left_compact, right_compact = compact(left.name), compact(right.name)
    if not left_norm or not right_norm or not compatible(left, right):
        return 0.0
    title = 100.0 if left_compact == right_compact else 100 * difflib.SequenceMatcher(None, left_norm, right_norm).ratio()
    if title < 86:
        return 0.0
    score = title
    if left.manufacturer and right.manufacturer:
        score += 6 if normalize(left.manufacturer) == normalize(right.manufacturer) else -18
    if left.cabinet_types and right.cabinet_types:
        score += 6 if set(left.cabinet_types) & set(right.cabinet_types) else -20
    left_digits = re.findall(r"\d+", left_norm)
    right_digits = re.findall(r"\d+", right_norm)
    if left_digits != right_digits:
        score -= 22
    left_variants = set(left_norm.split()) & VARIANT_WORDS
    right_variants = set(right_norm.split()) & VARIANT_WORDS
    if left_variants != right_variants:
        score -= 16
    return max(0.0, min(100.0, score))


def candidate_groups(games: Iterable[GameRecord]) -> list[list[GameRecord]]:
    buckets: dict[str, list[GameRecord]] = {}
    for game in games:
        key = compact(game.name)
        if len(key) >= 4:
            buckets.setdefault(key, []).append(game)
    groups = []
    for key, cluster in buckets.items():
        if len(cluster) < 2:
            continue
        # Very short/generic titles are retained for review only when another
        # field provides disambiguation; never silently merge them.
        if normalize(next(iter(cluster)).name) in AMBIGUOUS_NAMES:
            continue
        pairs = [pair_score(left, right) for i, left in enumerate(cluster) for right in cluster[i + 1:]]
        if pairs and min(pairs) >= 72:
            groups.append(sorted(cluster, key=lambda g: (-g.active_placement_count, g.game_id)))
    return groups


def candidate_id(group: list[GameRecord]) -> int:
    key = ":".join(str(game.game_id) for game in group)
    digest = hashlib.sha1(key.encode()).hexdigest()[:15]
    return int(digest, 16) % 9_000_000_000_000_000_000


def write_candidates(conn: duckdb.DuckDBPyConnection, groups: list[list[GameRecord]]) -> None:
    timestamp = utc_now()
    for group in groups:
        cid = candidate_id(group)
        key = ":".join(str(game.game_id) for game in group)
        scores = [pair_score(left, right) for i, left in enumerate(group) for right in group[i + 1:]]
        match_score = round(min(scores), 1)
        affected = sum(game.active_placement_count for game in group)
        # Placement impact is deliberately unbounded: a high-confidence
        # duplicate affecting 800 locations should outrank one affecting 80.
        priority = round(match_score * (1 + affected ** 0.5), 2)
        conn.execute("""
            INSERT INTO duplicate_review_candidates
                (candidate_id, cluster_key, match_score, affected_placements, priority_score, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (candidate_id) DO UPDATE SET
                match_score=excluded.match_score,
                affected_placements=excluded.affected_placements,
                priority_score=excluded.priority_score,
                updated_at=excluded.updated_at
        """, (cid, key, match_score, affected, priority, timestamp))
        for game in group:
            conn.execute("""
                INSERT INTO duplicate_review_records
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (candidate_id, game_id) DO UPDATE SET
                    name=excluded.name, manufacturer=excluded.manufacturer,
                    placement_count=excluded.placement_count,
                    active_placement_count=excluded.active_placement_count,
                    cabinet_types=excluded.cabinet_types, years=excluded.years,
                    source=excluded.source
            """, (cid, game.game_id, game.name, game.manufacturer,
                  game.placement_count, game.active_placement_count,
                  ", ".join(game.cabinet_types), ", ".join(map(str, game.years)), game.source))
    conn.commit()


def print_queue(conn: duckdb.DuckDBPyConnection, limit: int) -> None:
    candidates = rows(conn, """
        SELECT candidate_id, match_score, affected_placements, priority_score, status
        FROM duplicate_review_candidates
        WHERE status = 'pending'
        ORDER BY priority_score DESC, candidate_id
        LIMIT ?
    """, (limit,))
    for candidate in candidates:
        records = rows(conn, """
            SELECT game_id, name, manufacturer, placement_count, active_placement_count,
                   cabinet_types, years, source
            FROM duplicate_review_records WHERE candidate_id = ? ORDER BY active_placement_count DESC, game_id
        """, (candidate["candidate_id"],))
        print(json.dumps({"candidate": candidate, "records": records}, ensure_ascii=False))


def record_decision(conn: duckdb.DuckDBPyConnection, candidate_id: int, decision: str, notes: str) -> None:
    decision = decision.upper()
    if decision not in {"M", "U", "N"}:
        raise ValueError("decision must be M (match), U (unsure), or N (not a match)")
    status = {"M": "confirmed_match", "U": "likely_duplicate", "N": "separate"}[decision]
    if not conn.execute(
        "SELECT 1 FROM duplicate_review_candidates WHERE candidate_id = ?", (candidate_id,)
    ).fetchone():
        raise ValueError(f"unknown candidate_id: {candidate_id}")
    timestamp = utc_now()
    conn.execute(
        "UPDATE duplicate_review_candidates SET status = ?, updated_at = ? WHERE candidate_id = ?",
        (status, timestamp, candidate_id),
    )
    conn.execute(
        "INSERT INTO duplicate_review_decisions(candidate_id, decision, notes, decided_at) VALUES (?, ?, ?, ?)",
        (candidate_id, decision, notes or None, timestamp),
    )
    conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DUCKDB)
    parser.add_argument("--refresh", action="store_true", help="Rebuild pending candidate metrics from games.")
    parser.add_argument("--next", type=int, default=1, help="Print this many pending candidates as JSON lines.")
    parser.add_argument("--decide", nargs=2, metavar=("CANDIDATE_ID", "M/U/N"),
                        help="Record a review decision for a candidate.")
    parser.add_argument("--notes", default="", help="Optional review notes for --decide.")
    args = parser.parse_args()
    with duckdb.connect(str(args.db), read_only=not (args.refresh or args.decide)) as conn:
        if args.refresh or args.decide:
            ensure_schema(conn)
        if args.decide:
            record_decision(conn, int(args.decide[0]), args.decide[1], args.notes)
        if args.refresh:
            write_candidates(conn, candidate_groups(load_games(conn)))
        print_queue(conn, args.next)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
