#!/usr/bin/env python3
"""Merge ZIv machine inventories into already-linked local locations.

This is a conservative second pass after location matching/importing. It skips
machines already present at a location by fuzzy name match, reuses existing game
rows when a global game match is strong, and creates ZIv-only game ids for
clearly new games.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from import_pinballmap_locations import ExistingGame, game_similarity
from import_ziv_locations import DEFAULT_CACHE, DEFAULT_DB, ZivDetail, fetch_ziv_detail, ziv_db_id
from validate_ziv_locations import ZivArcade, connect, ensure_schema, fetch_ziv_us_arcades
from us_states import add_state_selection_args, selected_states


LOCAL_DUPLICATE_THRESHOLD = 0.96
GLOBAL_GAME_THRESHOLD = 0.96
AMBIGUOUS_SHORT_NAMES = {
    "0",
    "24",
    "baby pac man",
    "batman",
    "galaxy",
    "ghostbusters",
    "hockey table",
    "lightning",
    "pac man",
    "paradise lost",
    "pool table",
    "rampage",
    "simpsons",
    "star wars",
    "the act",
    "the end",
    "the simpsons",
}


@dataclass(frozen=True)
class ExistingLocationGame:
    game_id: int
    name: str
    cabinet_type: Optional[str]


@dataclass(frozen=True)
class MachineDecision:
    location_id: int
    location_name: str
    city: str
    ziv_location_id: int
    ziv_location_name: str
    ziv_machine_id: int
    ziv_game_id: int
    ziv_game_name: str
    cabinet_type: Optional[str]
    local_game_id: int
    local_game_name: str
    confidence: float
    method: str
    insert_location_game: bool
    insert_game: bool


def ensure_machine_schema(conn: sqlite3.Connection) -> None:
    ensure_schema(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ziv_machine_links (
            location_id INTEGER NOT NULL REFERENCES locations(location_id) ON DELETE CASCADE,
            ziv_location_id INTEGER NOT NULL,
            ziv_machine_id INTEGER NOT NULL,
            ziv_game_id INTEGER NOT NULL,
            game_id INTEGER NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
            confidence REAL NOT NULL,
            method TEXT NOT NULL,
            linked_at TEXT NOT NULL,
            PRIMARY KEY (location_id, ziv_machine_id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ziv_machine_links_ziv_game
            ON ziv_machine_links(ziv_game_id)
        """
    )


def load_existing_games(conn: sqlite3.Connection) -> list[ExistingGame]:
    return [
        ExistingGame(game_id=int(row["game_id"]), name=row["name"], manufacturer=row["manufacturer"])
        for row in conn.execute("SELECT game_id, name, manufacturer FROM games")
    ]


def load_location_inventory(conn: sqlite3.Connection, location_id: int) -> list[ExistingLocationGame]:
    return [
        ExistingLocationGame(
            game_id=int(row["game_id"]),
            name=row["name"],
            cabinet_type=row["cabinet_type"],
        )
        for row in conn.execute(
            """
            SELECT g.game_id, g.name, lg.cabinet_type
            FROM location_games lg
            JOIN games g ON g.game_id = lg.game_id
            WHERE lg.location_id = ?
            """,
            (location_id,),
        )
    ]


def load_ziv_links(conn: sqlite3.Connection, states: list[str], include_ziv_only: bool) -> list[sqlite3.Row]:
    ziv_only_filter = "" if include_ziv_only else "AND l.location_id NOT BETWEEN -2999999999 AND -2000000000"
    placeholders = ",".join("?" for _ in states)
    return list(
        conn.execute(
            f"""
            SELECT l.location_id, l.name, l.city, l.state, z.ziv_location_id, z.method
            FROM ziv_location_links z
            JOIN locations l ON l.location_id = z.location_id
            LEFT JOIN location_statuses ls ON ls.location_id = l.location_id
            WHERE l.state IN ({placeholders})
              AND COALESCE(ls.status, 'active') NOT IN ('closed', 'replaced')
              {ziv_only_filter}
            ORDER BY l.state, l.city, l.name, z.ziv_location_id
            """,
            states,
        )
    )


def find_ziv_arcades(cache: Path, cache_hours: float, states: list[str]) -> dict[int, ZivArcade]:
    state_set = set(states)
    return {ziv.ziv_id: ziv for ziv in fetch_ziv_us_arcades(cache, cache_hours) if ziv.state in state_set}


def best_local_duplicate(machine_name: str, inventory: list[ExistingLocationGame]) -> tuple[Optional[ExistingLocationGame], float]:
    best = None
    best_score = 0.0
    for existing in inventory:
        score = safer_game_similarity(machine_name, existing.name)
        if score > best_score:
            best = existing
            best_score = score
    return best, best_score


def norm_game_name(value: str) -> str:
    value = re.sub(r"([a-z])([A-Z])", r"\1 \2", value)
    value = value.lower().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = re.sub(r"\b(pro|premium|le|limited edition|special|remake|se)\b", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    if value.startswith("the "):
        value = value[4:]
    if value.endswith(" the"):
        value = value[:-4]
    return value.strip()


def safer_game_similarity(left: str, right: str) -> float:
    left_norm = norm_game_name(left)
    right_norm = norm_game_name(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    if left_norm in AMBIGUOUS_SHORT_NAMES or right_norm in AMBIGUOUS_SHORT_NAMES:
        return 0.0

    left_tokens = set(left_norm.split())
    right_tokens = set(right_norm.split())
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = len(left_tokens & right_tokens)
    min_overlap = overlap / min(len(left_tokens), len(right_tokens))
    jaccard = overlap / len(left_tokens | right_tokens)

    if min(len(left_tokens), len(right_tokens)) == 1 and max(len(left_tokens), len(right_tokens)) > 2:
        return 0.0
    if (left_norm in right_norm or right_norm in left_norm) and min_overlap >= 0.9 and jaccard >= 0.8:
        return 0.94

    sequence = game_similarity(left, right)
    if min_overlap < 0.5 and sequence < 0.97:
        return 0.0
    if jaccard < 0.5 and sequence < 0.97:
        return 0.0
    return max(sequence, 0.65 * sequence + 0.35 * jaccard)


def best_global_game(machine_name: str, games: list[ExistingGame]) -> tuple[Optional[ExistingGame], float]:
    best = None
    best_score = 0.0
    for game in games:
        score = safer_game_similarity(machine_name, game.name)
        if score > best_score:
            best = game
            best_score = score
    return best, best_score


def already_linked(conn: sqlite3.Connection, location_id: int, ziv_machine_id: int) -> bool:
    return bool(
        conn.execute(
            """
            SELECT 1 FROM ziv_machine_links
            WHERE location_id = ? AND ziv_machine_id = ?
            """,
            (location_id, ziv_machine_id),
        ).fetchone()
    )


def build_plan(
    conn: sqlite3.Connection,
    cache: Path,
    cache_hours: float,
    states: list[str],
    include_ziv_only: bool,
    delay_seconds: float,
) -> list[MachineDecision]:
    links = load_ziv_links(conn, states, include_ziv_only)
    ziv_by_id = find_ziv_arcades(cache, cache_hours, states)
    games = load_existing_games(conn)
    decisions: list[MachineDecision] = []

    for link in links:
        ziv = ziv_by_id.get(int(link["ziv_location_id"]))
        if not ziv:
            continue
        detail: ZivDetail = fetch_ziv_detail(ziv)
        inventory = load_location_inventory(conn, int(link["location_id"]))
        for machine in detail.machines:
            if already_linked(conn, int(link["location_id"]), machine.machine_id):
                continue
            duplicate, duplicate_score = best_local_duplicate(machine.game_name, inventory)
            if duplicate and duplicate_score >= LOCAL_DUPLICATE_THRESHOLD:
                decisions.append(
                    MachineDecision(
                        location_id=int(link["location_id"]),
                        location_name=link["name"],
                        city=link["city"],
                        ziv_location_id=ziv.ziv_id,
                        ziv_location_name=ziv.name,
                        ziv_machine_id=machine.machine_id,
                        ziv_game_id=machine.game_id,
                        ziv_game_name=machine.game_name,
                        cabinet_type=machine.genre,
                        local_game_id=duplicate.game_id,
                        local_game_name=duplicate.name,
                        confidence=round(duplicate_score, 3),
                        method="duplicate_local_inventory",
                        insert_location_game=False,
                        insert_game=False,
                    )
                )
                continue

            global_game, global_score = best_global_game(machine.game_name, games)
            if global_game and global_score >= GLOBAL_GAME_THRESHOLD:
                game_id = global_game.game_id
                game_name = global_game.name
                confidence = round(global_score, 3)
                method = "matched_existing_game"
                insert_game = False
            else:
                game_id = ziv_db_id(machine.game_id)
                game_name = machine.game_name
                confidence = 1.0
                method = "ziv_only_game"
                insert_game = True
                games.append(ExistingGame(game_id=game_id, name=game_name, manufacturer=None))

            decisions.append(
                MachineDecision(
                    location_id=int(link["location_id"]),
                    location_name=link["name"],
                    city=link["city"],
                    ziv_location_id=ziv.ziv_id,
                    ziv_location_name=ziv.name,
                    ziv_machine_id=machine.machine_id,
                    ziv_game_id=machine.game_id,
                    ziv_game_name=machine.game_name,
                    cabinet_type=machine.genre,
                    local_game_id=game_id,
                    local_game_name=game_name,
                    confidence=confidence,
                    method=method,
                    insert_location_game=True,
                    insert_game=insert_game,
                )
            )
            inventory.append(ExistingLocationGame(game_id=game_id, name=game_name, cabinet_type=machine.genre))
        time.sleep(delay_seconds)
    return decisions


def apply_plan(conn: sqlite3.Connection, decisions: list[MachineDecision], checked_at: str) -> None:
    game_rows = {
        decision.local_game_id: (decision.local_game_id, decision.local_game_name)
        for decision in decisions
        if decision.insert_game
    }
    conn.executemany(
        """
        INSERT INTO games (game_id, name, manufacturer)
        VALUES (?, ?, NULL)
        ON CONFLICT(game_id) DO UPDATE SET name = excluded.name
        """,
        list(game_rows.values()),
    )
    conn.executemany(
        """
        INSERT INTO location_games (
            location_id, game_id, cabinet_type, year, players,
            controls_condition, screen_condition, cabinet_condition, fetched_at
        )
        VALUES (?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?)
        ON CONFLICT(location_id, game_id) DO UPDATE SET
            cabinet_type = COALESCE(location_games.cabinet_type, excluded.cabinet_type),
            fetched_at = excluded.fetched_at
        """,
        [
            (decision.location_id, decision.local_game_id, decision.cabinet_type, checked_at)
            for decision in decisions
            if decision.insert_location_game
        ],
    )
    conn.executemany(
        """
        INSERT INTO ziv_machine_links (
            location_id, ziv_location_id, ziv_machine_id, ziv_game_id,
            game_id, confidence, method, linked_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(location_id, ziv_machine_id) DO UPDATE SET
            ziv_location_id = excluded.ziv_location_id,
            ziv_game_id = excluded.ziv_game_id,
            game_id = excluded.game_id,
            confidence = excluded.confidence,
            method = excluded.method,
            linked_at = excluded.linked_at
        """,
        [
            (
                decision.location_id,
                decision.ziv_location_id,
                decision.ziv_machine_id,
                decision.ziv_game_id,
                decision.local_game_id,
                decision.confidence,
                decision.method,
                checked_at,
            )
            for decision in decisions
        ],
    )
    changed_locations = sorted({decision.location_id for decision in decisions if decision.insert_location_game})
    for location_id in changed_locations:
        conn.execute(
            """
            UPDATE locations
            SET game_count = (
                    SELECT COUNT(*) FROM location_games WHERE location_id = ?
                ),
                unique_game_count = (
                    SELECT COUNT(DISTINCT game_id) FROM location_games WHERE location_id = ?
                ),
                detail_fetched_at = ?
            WHERE location_id = ?
            """,
            (location_id, location_id, checked_at, location_id),
        )


def print_plan(decisions: list[MachineDecision], states: list[str], limit: int) -> None:
    inserts = [decision for decision in decisions if decision.insert_location_game]
    skips = [decision for decision in decisions if not decision.insert_location_game]
    new_games = [decision for decision in decisions if decision.insert_game]
    print(f"# ZIv Machine Merge Plan: {', '.join(states)}")
    print()
    print(f"- ZIv machine rows reviewed: {len(decisions)}")
    print(f"- Machine placements to insert: {len(inserts)}")
    print(f"- ZIv-only games to create: {len(new_games)}")
    print(f"- Machines skipped as already present: {len(skips)}")
    print()
    print("## Placements To Insert")
    print("| location | city | ZIv game | local game | method | confidence |")
    print("|---|---|---|---|---|---:|")
    for decision in inserts[:limit]:
        print(
            f"| {decision.location_name} | {decision.city} | {decision.ziv_game_name} | "
            f"{decision.local_game_name} | {decision.method} | {decision.confidence:.3f} |"
        )
    print()
    print("## Skipped As Already Present")
    print("| location | city | ZIv game | matched local game | confidence |")
    print("|---|---|---|---|---:|")
    for decision in skips[:limit]:
        print(
            f"| {decision.location_name} | {decision.city} | {decision.ziv_game_name} | "
            f"{decision.local_game_name} | {decision.confidence:.3f} |"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Merge ZIv machine inventories into linked local locations.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--cache-hours", type=float, default=24.0)
    add_state_selection_args(parser, default_state="UT")
    parser.add_argument("--include-ziv-only", action="store_true")
    parser.add_argument("--delay-seconds", type=float, default=0.25)
    parser.add_argument("--limit", type=int, default=80)
    parser.add_argument("--apply", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    conn = connect(args.db)
    try:
        ensure_machine_schema(conn)
        states = selected_states(args)
        decisions = build_plan(
            conn,
            args.cache,
            args.cache_hours,
            states,
            args.include_ziv_only,
            args.delay_seconds,
        )
        print_plan(decisions, states, args.limit)
        if args.apply:
            apply_plan(conn, decisions, checked_at)
            conn.commit()
            print()
            print("Applied.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
