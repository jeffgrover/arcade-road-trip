#!/usr/bin/env python3
"""Coordinate conservative national source curation.

Dry-run is the default. Apply mode backs up the SQLite database, links only
high-confidence source matches, imports clear source-only rows, and writes
review reports for ambiguous records.
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from import_pinballmap_api import (
    bundle_from_region_payloads,
    fetch_location_types,
    fetch_region_locations,
    fetch_regions,
)
from import_pinballmap_locations import (
    DEFAULT_GAME_MATCH_THRESHOLD,
    DEFAULT_LOCATION_MATCH_THRESHOLD,
    ImportBundle,
    best_location_candidate,
    connect as connect_pinballmap,
    import_bundle,
    load_existing_locations,
)
from import_ziv_locations import DEFAULT_CACHE as ZIV_CACHE, build_plan as build_ziv_import_plan
from import_ziv_locations import insert_locations as insert_ziv_locations
from import_ziv_locations import upsert_override_links
from merge_ziv_machines import apply_plan as apply_ziv_machine_plan
from merge_ziv_machines import build_plan as build_ziv_machine_plan
from merge_ziv_machines import ensure_machine_schema
from us_states import add_state_selection_args, selected_states
from validate_ziv_locations import (
    Match,
    best_matches,
    connect,
    ensure_schema,
    fetch_ziv_us_arcades,
    insert_verifications,
    load_local_locations,
    upsert_links,
)


DEFAULT_DB = Path("aurcade_locations.sqlite")
DEFAULT_REPORT_DIR = Path("reports")
PINBALLMAP_AMBIGUOUS_THRESHOLD = 0.65


@dataclass(frozen=True)
class PinballMapPossible:
    confidence: float
    pinballmap_id: int
    pinballmap_name: str
    pinballmap_city: str
    pinballmap_state: str
    local_id: int
    local_name: str
    local_city: str
    local_state: str


def backup_database(db_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.with_name(f"{db_path.stem}.backup_{stamp}{db_path.suffix}")
    source = sqlite3.connect(db_path)
    try:
        dest = sqlite3.connect(backup_path)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    return backup_path


def write_ziv_possible_report(report_dir: Path, matches: list[Match]) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"ziv_possible_matches_{date_stamp()}.md"
    possible = [match for match in matches if match.confidence < 0.84]
    lines = [
        "# ZIv Possible Matches",
        "",
        "| confidence | local id | local | local city | ZIv id | ZIv name | ZIv city | updated |",
        "|---:|---:|---|---|---:|---|---|---|",
    ]
    for match in sorted(possible, key=lambda item: item.confidence, reverse=True):
        lines.append(
            f"| {match.confidence:.3f} | {match.local.location_id} | {match.local.name} | "
            f"{match.local.city}, {match.local.state} | {match.ziv.ziv_id} | {match.ziv.name} | "
            f"{match.ziv.city}, {match.ziv.state} | {match.ziv.last_update_time} |"
        )
    path.write_text("\n".join(lines) + "\n")
    return path


def write_ziv_unmatched_report(report_dir: Path, ziv_arcades, matches: list[Match]) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"ziv_unmatched_source_locations_{date_stamp()}.csv"
    matched_ziv_ids = {match.ziv.ziv_id for match in matches}
    rows = [ziv for ziv in ziv_arcades if ziv.ziv_id not in matched_ziv_ids]
    rows.sort(key=lambda ziv: (ziv.last_update_time or "", ziv.state, ziv.city, ziv.name), reverse=True)
    with path.open("w", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(["ziv_id", "name", "city", "state", "address", "postal_code", "updated", "url"])
        for ziv in rows:
            writer.writerow(
                [
                    ziv.ziv_id,
                    ziv.name,
                    ziv.city,
                    ziv.state,
                    ziv.address_line1,
                    ziv.postal_code,
                    ziv.last_update_time,
                    ziv.evidence_url,
                ]
            )
    return path


def pinballmap_possible_matches(
    conn: sqlite3.Connection,
    bundle: ImportBundle,
    location_match_threshold: float,
) -> list[PinballMapPossible]:
    positive_locations = [
        location for location in load_existing_locations(conn) if location.location_id > 0
    ]
    possibles = []
    for location in bundle.locations:
        candidate = best_location_candidate(location, positive_locations)
        if not candidate:
            continue
        if PINBALLMAP_AMBIGUOUS_THRESHOLD <= candidate.confidence < location_match_threshold:
            possibles.append(
                PinballMapPossible(
                    confidence=candidate.confidence,
                    pinballmap_id=location.pinballmap_location_id,
                    pinballmap_name=location.name,
                    pinballmap_city=location.city or "",
                    pinballmap_state=location.state or "",
                    local_id=candidate.location_id,
                    local_name=next(
                        local.name for local in positive_locations if local.location_id == candidate.location_id
                    ),
                    local_city=next(
                        local.city or "" for local in positive_locations if local.location_id == candidate.location_id
                    ),
                    local_state=next(
                        local.state or "" for local in positive_locations if local.location_id == candidate.location_id
                    ),
                )
            )
    return sorted(possibles, key=lambda item: item.confidence, reverse=True)


def write_pinballmap_possible_report(report_dir: Path, possibles: list[PinballMapPossible]) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"pinballmap_possible_matches_{date_stamp()}.md"
    lines = [
        "# Pinball Map Possible Matches",
        "",
        "| confidence | PM id | Pinball Map | PM city | local id | local | local city |",
        "|---:|---:|---|---|---:|---|---|",
    ]
    for match in possibles:
        lines.append(
            f"| {match.confidence:.3f} | {match.pinballmap_id} | {match.pinballmap_name} | "
            f"{match.pinballmap_city}, {match.pinballmap_state} | {match.local_id} | "
            f"{match.local_name} | {match.local_city}, {match.local_state} |"
        )
    path.write_text("\n".join(lines) + "\n")
    return path


def write_quality_report(
    report_dir: Path,
    conn: sqlite3.Connection,
    states: list[str],
    ziv_matches: list[Match],
    pinballmap_bundle: ImportBundle | None,
) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"national_data_quality_{date_stamp()}.md"
    placeholders = ",".join("?" for _ in states)
    rows = conn.execute(
        f"""
        SELECT l.state,
               COUNT(DISTINCT l.location_id) AS locations,
               COUNT(DISTINCT pll.location_id) AS pinballmap_linked,
               COUNT(DISTINCT zll.location_id) AS ziv_linked,
               COUNT(DISTINCT CASE WHEN l.location_id BETWEEN -1999999999 AND -1000000000 THEN l.location_id END) AS pinballmap_only,
               COUNT(DISTINCT CASE WHEN l.location_id BETWEEN -2999999999 AND -2000000000 THEN l.location_id END) AS ziv_only
        FROM locations l
        LEFT JOIN pinballmap_location_links pll ON pll.location_id = l.location_id
        LEFT JOIN ziv_location_links zll ON zll.location_id = l.location_id
        WHERE l.state IN ({placeholders})
        GROUP BY l.state
        ORDER BY l.state
        """,
        states,
    ).fetchall()
    lines = [
        "# National Data Quality",
        "",
        f"- States: {', '.join(states)}",
        f"- ZIv matches found: {len(ziv_matches)}",
        f"- ZIv possible matches: {len([match for match in ziv_matches if match.confidence < 0.84])}",
    ]
    if pinballmap_bundle:
        lines.extend(
            [
                f"- Pinball Map API locations fetched: {len(pinballmap_bundle.locations)}",
                f"- Pinball Map API placements fetched: {len(pinballmap_bundle.placements)}",
            ]
        )
    lines.extend(
        [
            "",
            "| state | locations | Pinball Map linked | ZIv linked | Pinball Map-only | ZIv-only |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in rows:
        lines.append(
            f"| {row[0]} | {row[1] or 0} | {row[2] or 0} | {row[3] or 0} | {row[4] or 0} | {row[5] or 0} |"
        )
    path.write_text("\n".join(lines) + "\n")
    return path


def date_stamp() -> str:
    return datetime.now().strftime("%Y%m%d")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run conservative national source curation.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    add_state_selection_args(parser, default_state="UT")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--skip-backup", action="store_true")
    parser.add_argument("--skip-ziv", action="store_true")
    parser.add_argument("--skip-pinballmap", action="store_true")
    parser.add_argument("--ziv-cache", type=Path, default=ZIV_CACHE)
    parser.add_argument("--pinballmap-cache-dir", type=Path, default=Path("cache/pinballmap_api"))
    parser.add_argument("--cache-hours", type=float, default=24 * 7)
    parser.add_argument("--delay-seconds", type=float, default=1.0)
    parser.add_argument("--locations-only", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    states = selected_states(args)
    checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    backup_path = None
    if args.apply and not args.skip_backup:
        backup_path = backup_database(args.db)

    conn = connect(args.db)
    pinballmap_bundle = None
    report_paths: list[Path] = []
    try:
        ensure_schema(conn)
        ensure_machine_schema(conn)
        ziv_matches: list[Match] = []
        if not args.skip_ziv:
            all_ziv = [ziv for ziv in fetch_ziv_us_arcades(args.ziv_cache, args.cache_hours) if ziv.state in states]
            locals_ = [local for local in load_local_locations(conn, include_inactive=False) if local.state in states]
            ziv_matches = best_matches(all_ziv, locals_)
            high_matches = [match for match in ziv_matches if match.confidence >= 0.84]
            if args.apply:
                upsert_links(conn, high_matches, checked_at)
                insert_verifications(conn, high_matches, checked_at)
                conn.commit()
            report_paths.append(write_ziv_possible_report(args.report_dir, ziv_matches))
            report_paths.append(write_ziv_unmatched_report(args.report_dir, all_ziv, ziv_matches))

            for state in states:
                import_plan = build_ziv_import_plan(
                    conn,
                    state,
                    args.ziv_cache,
                    args.cache_hours,
                    fetch_machines=not args.locations_only,
                    delay_seconds=args.delay_seconds,
                )
                if args.apply:
                    upsert_override_links(conn, import_plan, checked_at)
                    insert_ziv_locations(conn, import_plan, checked_at)
                    conn.commit()

            machine_plan = build_ziv_machine_plan(
                conn,
                args.ziv_cache,
                args.cache_hours,
                states,
                include_ziv_only=True,
                delay_seconds=args.delay_seconds,
            )
            if args.apply:
                apply_ziv_machine_plan(conn, machine_plan, checked_at)
                conn.commit()

        if not args.skip_pinballmap:
            pm_regions = [
                region
                for region in fetch_regions(args.pinballmap_cache_dir, args.cache_hours)
                if region.state in states
            ]
            pm_regions.sort(key=lambda region: (region.state, region.name))
            location_types = fetch_location_types(args.pinballmap_cache_dir, args.cache_hours)
            payloads = []
            for index, region in enumerate(pm_regions):
                payloads.append(fetch_region_locations(region, args.pinballmap_cache_dir, args.cache_hours))
                if index < len(pm_regions) - 1:
                    time.sleep(args.delay_seconds)
            pinballmap_bundle = bundle_from_region_payloads(payloads, location_types)
            pm_conn = connect_pinballmap(args.db, readonly=not args.apply)
            try:
                possibles = pinballmap_possible_matches(
                    pm_conn,
                    pinballmap_bundle,
                    DEFAULT_LOCATION_MATCH_THRESHOLD,
                )
                report_paths.append(write_pinballmap_possible_report(args.report_dir, possibles))
                import_bundle(
                    pm_conn,
                    pinballmap_bundle,
                    apply=args.apply,
                    insert_unmatched_locations=True,
                    insert_unmatched_games=True,
                    location_match_threshold=DEFAULT_LOCATION_MATCH_THRESHOLD,
                    game_match_threshold=DEFAULT_GAME_MATCH_THRESHOLD,
                    verbose=False,
                    ambiguous_location_threshold=PINBALLMAP_AMBIGUOUS_THRESHOLD,
                )
            finally:
                pm_conn.close()

        report_paths.append(write_quality_report(args.report_dir, conn, states, ziv_matches, pinballmap_bundle))
    finally:
        conn.close()

    print("# National Source Curation")
    print()
    print(f"- States: {', '.join(states)}")
    print(f"- Mode: {'apply' if args.apply else 'dry-run'}")
    if backup_path:
        print(f"- Backup: {backup_path}")
    print("- Reports:")
    for path in report_paths:
        print(f"  - {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
