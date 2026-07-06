#!/usr/bin/env python3
"""Import Zenius -I- vanisher locations into the Aurcade-compatible schema.

This importer is intentionally conservative. It imports only locations that do
not already match an Aurcade/Pinball Map/local row, and it stores ZIv ids in a
separate negative-id namespace.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from validate_ziv_locations import (
    USER_AGENT,
    ZIV_API,
    ZivArcade,
    best_matches,
    connect,
    ensure_schema,
    fetch_ziv_us_arcades,
    load_local_locations,
)


DEFAULT_DB = Path("aurcade_locations.sqlite")
DEFAULT_CACHE = Path("ziv_us_arcades_cache.json")
ZIV_ID_OFFSET = 2_000_000_000
ZIV_SOURCE_URL = "https://zenius-i-vanisher.com/v5.2/arcade.php?id={ziv_id}"

ZIV_LOCATION_ID_OVERRIDES = {
    # ZIv city is Salt Lake City, but this is the existing Sandy Nickelcade.
    1783: 1569,
    # ZIv has a second less-complete Arcade Galactic row; link it to the
    # existing West Valley City Arcade Galactic instead of inserting a duplicate.
    6007: 120,
}


@dataclass(frozen=True)
class ZivMachine:
    machine_id: int
    game_id: int
    game_name: str
    genre: Optional[str]
    condition: Optional[int]


@dataclass(frozen=True)
class ZivDetail:
    arcade: ZivArcade
    machines: list[ZivMachine]
    raw: dict[str, Any]


@dataclass
class ImportPlan:
    matched_ziv_ids: set[int]
    override_links: dict[int, int]
    locations_to_insert: list[ZivArcade]
    details: dict[int, ZivDetail]


def ziv_db_id(source_id: int) -> int:
    return -(ZIV_ID_OFFSET + source_id)


def clean_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def fetch_ziv_detail(ziv: ZivArcade) -> ZivDetail:
    params = urllib.parse.urlencode(
        {
            "action": "query",
            "country": "United States of America",
            "name": ziv.name,
            "skip_pictures": "1",
            "skip_visitors": "1",
            "skip_comments": "1",
        }
    )
    request = urllib.request.Request(
        f"{ZIV_API}?{params}",
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))
    matches = [row for row in data.get("arcades", []) if int(row.get("id") or 0) == ziv.ziv_id]
    row = matches[0] if matches else ziv.raw
    machines = []
    for machine in row.get("machines") or []:
        game = machine.get("game") or {}
        try:
            machine_id = int(machine["id"])
            game_id = int(game["id"])
        except (KeyError, TypeError, ValueError):
            continue
        machines.append(
            ZivMachine(
                machine_id=machine_id,
                game_id=game_id,
                game_name=(game.get("name") or "").strip(),
                genre=clean_text(game.get("genre")),
                condition=int(machine["condition"]) if str(machine.get("condition") or "").isdigit() else None,
            )
        )
    return ZivDetail(arcade=ziv, machines=machines, raw=row)


def existing_ziv_links(conn: sqlite3.Connection) -> set[int]:
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ziv_location_links'"
    ).fetchone():
        return set()
    return {
        int(row["ziv_location_id"])
        for row in conn.execute("SELECT ziv_location_id FROM ziv_location_links")
    }


def build_plan(
    conn: sqlite3.Connection,
    state: str,
    cache: Path,
    cache_hours: float,
    fetch_machines: bool,
    delay_seconds: float,
) -> ImportPlan:
    ziv_arcades = [ziv for ziv in fetch_ziv_us_arcades(cache, cache_hours) if ziv.state == state]
    locals_ = [local for local in load_local_locations(conn, include_inactive=True) if local.state == state]
    matches = best_matches(ziv_arcades, locals_)
    matched_ziv_ids = {match.ziv.ziv_id for match in matches}
    already_linked = existing_ziv_links(conn)
    matched_ziv_ids.update(already_linked)

    override_links = {
        ziv_id: location_id
        for ziv_id, location_id in ZIV_LOCATION_ID_OVERRIDES.items()
        if any(ziv.ziv_id == ziv_id for ziv in ziv_arcades)
    }
    matched_ziv_ids.update(override_links)

    locations_to_insert = [
        ziv for ziv in sorted(ziv_arcades, key=lambda item: (item.city, item.name))
        if ziv.ziv_id not in matched_ziv_ids
    ]

    details: dict[int, ZivDetail] = {}
    if fetch_machines:
        for ziv in locations_to_insert:
            details[ziv.ziv_id] = fetch_ziv_detail(ziv)
            time.sleep(delay_seconds)
    else:
        details = {ziv.ziv_id: ZivDetail(ziv, [], ziv.raw) for ziv in locations_to_insert}

    return ImportPlan(
        matched_ziv_ids=matched_ziv_ids,
        override_links=override_links,
        locations_to_insert=locations_to_insert,
        details=details,
    )


def upsert_override_links(conn: sqlite3.Connection, plan: ImportPlan, checked_at: str) -> None:
    conn.executemany(
        """
        INSERT INTO ziv_location_links (
            location_id, ziv_location_id, confidence, method, linked_at
        )
        VALUES (?, ?, 1.0, 'manual_override', ?)
        ON CONFLICT(location_id, ziv_location_id) DO UPDATE SET
            confidence = excluded.confidence,
            method = excluded.method,
            linked_at = excluded.linked_at
        """,
        [(location_id, ziv_id, checked_at) for ziv_id, location_id in plan.override_links.items()],
    )


def insert_locations(conn: sqlite3.Connection, plan: ImportPlan, checked_at: str) -> None:
    location_rows = []
    link_rows = []
    verification_rows = []
    game_rows: dict[int, tuple[int, str]] = {}
    location_game_rows = []

    for ziv in plan.locations_to_insert:
        detail = plan.details[ziv.ziv_id]
        location_id = ziv_db_id(ziv.ziv_id)
        machine_count = len(detail.machines)
        address_text = "\n".join(
            part
            for part in [ziv.address_line1, ziv.address_line2, ziv.city, f"{ziv.state}, {ziv.postal_code}".strip(", ")]
            if part
        )
        location_rows.append(
            (
                location_id,
                ziv.name,
                "Arcade",
                ziv.city,
                ziv.state,
                ziv.address_line1 or None,
                ziv.postal_code or None,
                clean_text(ziv.raw.get("contactNumber")),
                address_text or None,
                ziv.website or None,
                1,
                machine_count,
                len({machine.game_id for machine in detail.machines}),
                0,
                f"ZIv updated {ziv.last_update_time}" if ziv.last_update_time else None,
                clean_text(ziv.raw.get("information")),
                ziv.latitude,
                ziv.longitude,
                checked_at,
                ZIV_SOURCE_URL.format(ziv_id=ziv.ziv_id),
            )
        )
        link_rows.append((location_id, ziv.ziv_id, 1.0, "ziv_only_import", checked_at))
        verification_rows.append(
            (
                location_id,
                checked_at,
                "ziv",
                "ziv_imported",
                "ziv_only_import",
                str(ziv.ziv_id),
                ziv.name,
                ziv.matched_address,
                ziv.latitude,
                ziv.longitude,
                1.0,
                ZIV_SOURCE_URL.format(ziv_id=ziv.ziv_id),
                json.dumps(detail.raw, ensure_ascii=False),
                f"Imported ZIv-only Utah location; ZIv updated {ziv.last_update_time}; machines={machine_count}.",
            )
        )
        for machine in detail.machines:
            game_id = ziv_db_id(machine.game_id)
            game_rows[game_id] = (game_id, machine.game_name)
            location_game_rows.append(
                (
                    location_id,
                    game_id,
                    machine.genre,
                    None,
                    None,
                    machine.condition,
                    machine.condition,
                    machine.condition,
                    checked_at,
                )
            )

    conn.executemany(
        """
        INSERT INTO locations (
            location_id, name, type, city, state, street_address, postal_code,
            phone, address_text, website_url, is_public, game_count,
            unique_game_count, world_record_count, updated_text, description,
            latitude, longitude, detail_fetched_at, source_url
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(location_id) DO UPDATE SET
            name = excluded.name,
            type = excluded.type,
            city = excluded.city,
            state = excluded.state,
            street_address = excluded.street_address,
            postal_code = excluded.postal_code,
            phone = excluded.phone,
            address_text = excluded.address_text,
            website_url = excluded.website_url,
            is_public = excluded.is_public,
            game_count = excluded.game_count,
            unique_game_count = excluded.unique_game_count,
            updated_text = excluded.updated_text,
            description = excluded.description,
            latitude = excluded.latitude,
            longitude = excluded.longitude,
            detail_fetched_at = excluded.detail_fetched_at,
            source_url = excluded.source_url
        """,
        location_rows,
    )
    conn.executemany(
        """
        INSERT INTO ziv_location_links (
            location_id, ziv_location_id, confidence, method, linked_at
        )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(location_id, ziv_location_id) DO UPDATE SET
            confidence = excluded.confidence,
            method = excluded.method,
            linked_at = excluded.linked_at
        """,
        link_rows,
    )
    conn.executemany(
        """
        INSERT INTO games (game_id, name, manufacturer)
        VALUES (?, ?, NULL)
        ON CONFLICT(game_id) DO UPDATE SET
            name = excluded.name
        """,
        list(game_rows.values()),
    )
    conn.executemany(
        """
        INSERT INTO location_games (
            location_id, game_id, cabinet_type, year, players,
            controls_condition, screen_condition, cabinet_condition, fetched_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(location_id, game_id) DO UPDATE SET
            cabinet_type = excluded.cabinet_type,
            controls_condition = excluded.controls_condition,
            screen_condition = excluded.screen_condition,
            cabinet_condition = excluded.cabinet_condition,
            fetched_at = excluded.fetched_at
        """,
        location_game_rows,
    )
    conn.executemany(
        """
        INSERT INTO location_verifications (
            location_id, checked_at, provider, status, match_kind, query,
            matched_name, matched_address, matched_latitude, matched_longitude,
            distance_miles, confidence, evidence_url, raw_json, notes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
        """,
        verification_rows,
    )


def print_plan(plan: ImportPlan) -> None:
    print("# ZIv Utah Import Plan")
    print()
    print(f"- Manual override links to upsert: {len(plan.override_links)}")
    print(f"- New ZIv-only locations to insert: {len(plan.locations_to_insert)}")
    print(f"- New machine placements to upsert: {sum(len(detail.machines) for detail in plan.details.values())}")
    print()
    if plan.override_links:
        print("## Override Links")
        print("| ZIv id | local id |")
        print("|---:|---:|")
        for ziv_id, location_id in sorted(plan.override_links.items()):
            print(f"| {ziv_id} | {location_id} |")
        print()
    print("## New Locations")
    print("| new local id | ZIv id | name | city | machines | updated |")
    print("|---:|---:|---|---|---:|---|")
    for ziv in plan.locations_to_insert:
        detail = plan.details[ziv.ziv_id]
        print(
            f"| {ziv_db_id(ziv.ziv_id)} | {ziv.ziv_id} | {ziv.name} | "
            f"{ziv.city}, {ziv.state} | {len(detail.machines)} | {ziv.last_update_time} |"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import ZIv-only locations into the local DB.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--cache-hours", type=float, default=24.0)
    parser.add_argument("--state", default="UT")
    parser.add_argument("--locations-only", action="store_true")
    parser.add_argument("--delay-seconds", type=float, default=0.25)
    parser.add_argument("--apply", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    conn = connect(args.db)
    try:
        ensure_schema(conn)
        plan = build_plan(
            conn,
            args.state,
            args.cache,
            args.cache_hours,
            fetch_machines=not args.locations_only,
            delay_seconds=args.delay_seconds,
        )
        print_plan(plan)
        if args.apply:
            upsert_override_links(conn, plan, checked_at)
            insert_locations(conn, plan, checked_at)
            conn.commit()
            print()
            print("Applied.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
