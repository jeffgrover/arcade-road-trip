#!/usr/bin/env python3
"""Import Pinball Map public API data into the local arcade database.

This is the national Pinball Map path. It uses measured, cached public API
calls instead of privileged admin CSV exports.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from import_pinballmap_locations import (
    DEFAULT_GAME_MATCH_THRESHOLD,
    DEFAULT_LOCATION_MATCH_THRESHOLD,
    ImportBundle,
    PinballMapLocation,
    PinballMapMachine,
    PinballMapPlacement,
    clean_text,
    connect,
    import_bundle,
    merge_machine,
    parse_bool,
    parse_float,
    parse_int,
)
from us_states import add_state_selection_args, normalize_state, selected_states

from arcade_db import DEFAULT_DUCKDB


DEFAULT_DB = DEFAULT_DUCKDB
DEFAULT_CACHE_DIR = Path("cache/pinballmap_api")
PINBALLMAP_API = "https://pinballmap.com/api/v1"
USER_AGENT = "arcade-road-trip-pinballmap-importer/0.1 (personal local data curation)"


@dataclass(frozen=True)
class Region:
    region_id: int
    name: str
    full_name: str
    state: str


def api_get_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def read_cached_json(path: Path, cache_hours: float) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    age_hours = (time.time() - path.stat().st_mtime) / 3600
    if cache_hours <= 0 or age_hours <= cache_hours:
        return json.loads(path.read_text())
    return None


def fetch_cached_json(url: str, path: Path, cache_hours: float) -> dict[str, Any]:
    cached = read_cached_json(path, cache_hours)
    if cached is not None:
        return cached
    path.parent.mkdir(parents=True, exist_ok=True)
    data = api_get_json(url)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return data


def fetch_regions(cache_dir: Path, cache_hours: float) -> list[Region]:
    data = fetch_cached_json(
        f"{PINBALLMAP_API}/regions.json",
        cache_dir / "regions.json",
        cache_hours,
    )
    regions = []
    for row in data.get("regions", []):
        try:
            state = normalize_state(row.get("state") or "")
        except argparse.ArgumentTypeError:
            continue
        regions.append(
            Region(
                region_id=int(row["id"]),
                name=row["name"],
                full_name=row["full_name"],
                state=state,
            )
        )
    return regions


def fetch_location_types(cache_dir: Path, cache_hours: float) -> dict[int, str]:
    data = fetch_cached_json(
        f"{PINBALLMAP_API}/location_types.json",
        cache_dir / "location_types.json",
        cache_hours,
    )
    return {
        int(row["id"]): row["name"]
        for row in data.get("location_types", [])
        if row.get("id") is not None and row.get("name")
    }


def fetch_region_locations(region: Region, cache_dir: Path, cache_hours: float) -> dict[str, Any]:
    return fetch_cached_json(
        f"{PINBALLMAP_API}/region/{region.name}/locations.json",
        cache_dir / "regions" / f"{region.name}.json",
        cache_hours,
    )


def location_from_api(row: dict[str, Any], location_types: dict[int, str]) -> PinballMapLocation:
    location_type_id = parse_int(str(row.get("location_type_id") or ""))
    updated_text = clean_text(row.get("date_last_updated")) or clean_text(row.get("updated_at"))
    xrefs = [xref for xref in row.get("location_machine_xrefs") or [] if not xref.get("deleted_at")]
    return PinballMapLocation(
        pinballmap_location_id=int(row["id"]),
        name=(row.get("name") or "").strip(),
        street_address=clean_text(row.get("street")),
        city=clean_text(row.get("city")),
        state=clean_text(row.get("state")),
        postal_code=clean_text(row.get("zip")),
        phone=clean_text(row.get("phone")),
        website_url=clean_text(row.get("website")),
        description=clean_text(row.get("description")),
        latitude=parse_float(str(row.get("lat") or "")),
        longitude=parse_float(str(row.get("lon") or "")),
        location_type=location_types.get(location_type_id or -1),
        machine_count=parse_int(str(row.get("machine_count") or row.get("num_machines") or "")) or len(xrefs),
        updated_text=updated_text,
    )


def bundle_from_region_payloads(
    payloads: list[dict[str, Any]],
    location_types: dict[int, str],
) -> ImportBundle:
    locations_by_id: dict[int, PinballMapLocation] = {}
    machines: dict[int, PinballMapMachine] = {}
    placements_by_id: dict[int, PinballMapPlacement] = {}

    for payload in payloads:
        for row in payload.get("locations", []):
            if row.get("country") and row.get("country") != "US":
                continue
            location = location_from_api(row, location_types)
            locations_by_id[location.pinballmap_location_id] = location
            for xref in row.get("location_machine_xrefs") or []:
                if xref.get("deleted_at"):
                    continue
                machine_row = xref.get("machine") or {}
                machine_id = parse_int(str(machine_row.get("id") or ""))
                lmx_id = parse_int(str(xref.get("id") or ""))
                if not machine_id or not lmx_id:
                    continue
                machine = PinballMapMachine(
                    pinballmap_machine_id=machine_id,
                    name=(machine_row.get("name") or "").strip(),
                    manufacturer=clean_text(machine_row.get("manufacturer")),
                    year=parse_int(str(machine_row.get("year") or "")),
                    machine_type=None,
                    machine_display=None,
                    ipdb=clean_text(str(machine_row.get("ipdb_id") or "")),
                    opdb=clean_text(machine_row.get("opdb_id")),
                )
                machines[machine_id] = merge_machine(machines.get(machine_id), machine)
                ic_enabled = xref.get("ic_enabled")
                placements_by_id[lmx_id] = PinballMapPlacement(
                    pinballmap_lmx_id=lmx_id,
                    pinballmap_location_id=location.pinballmap_location_id,
                    pinballmap_machine_id=machine_id,
                    ic_enabled=parse_bool(str(ic_enabled)) if ic_enabled is not None else None,
                )

    return ImportBundle(
        locations=sorted(locations_by_id.values(), key=lambda location: location.pinballmap_location_id),
        machines=machines,
        placements=sorted(placements_by_id.values(), key=lambda placement: placement.pinballmap_lmx_id),
    )


def write_fetch_manifest(cache_dir: Path, regions: list[Region]) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "regions": [region.__dict__ for region in regions],
    }
    cache_dir.joinpath("last_fetch_manifest.json").write_text(json.dumps(manifest, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import Pinball Map public API region data.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--cache-hours", type=float, default=24 * 7)
    add_state_selection_args(parser, default_state="UT")
    parser.add_argument(
        "--regions",
        help="Comma-separated Pinball Map region slugs. Overrides state selection.",
    )
    parser.add_argument("--delay-seconds", type=float, default=1.0)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--matched-locations-only", action="store_true")
    parser.add_argument("--matched-games-only", action="store_true")
    parser.add_argument("--location-match-threshold", type=float, default=DEFAULT_LOCATION_MATCH_THRESHOLD)
    parser.add_argument(
        "--ambiguous-location-threshold",
        type=float,
        default=0.65,
        help="Skip source-only inserts when the best local location candidate is at or above this score.",
    )
    parser.add_argument("--game-match-threshold", type=float, default=DEFAULT_GAME_MATCH_THRESHOLD)
    parser.add_argument("--verbose", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    regions = fetch_regions(args.cache_dir, args.cache_hours)
    if args.regions:
        requested = {name.strip() for name in args.regions.split(",") if name.strip()}
        selected_regions = [region for region in regions if region.name in requested]
    else:
        states = set(selected_states(args))
        selected_regions = [region for region in regions if region.state in states]
    selected_regions.sort(key=lambda region: (region.state, region.name))

    print("# Pinball Map API Import Plan")
    print()
    print(f"- Regions selected: {len(selected_regions)}")
    for region in selected_regions:
        print(f"  - {region.name}: {region.full_name} ({region.state})")
    print()

    location_types = fetch_location_types(args.cache_dir, args.cache_hours)
    payloads = []
    for index, region in enumerate(selected_regions):
        payloads.append(fetch_region_locations(region, args.cache_dir, args.cache_hours))
        if index < len(selected_regions) - 1:
            time.sleep(args.delay_seconds)
    write_fetch_manifest(args.cache_dir, selected_regions)

    bundle = bundle_from_region_payloads(payloads, location_types)
    print(f"- API locations fetched: {len(bundle.locations)}")
    print(f"- API unique machines fetched: {len(bundle.machines)}")
    print(f"- API location-machine placements fetched: {len(bundle.placements)}")
    print()

    conn = connect(args.db, readonly=not args.apply)
    try:
        import_bundle(
            conn,
            bundle,
            apply=args.apply,
            insert_unmatched_locations=not args.matched_locations_only,
            insert_unmatched_games=not args.matched_games_only,
            location_match_threshold=args.location_match_threshold,
            game_match_threshold=args.game_match_threshold,
            verbose=args.verbose,
            ambiguous_location_threshold=args.ambiguous_location_threshold,
        )
    finally:
        conn.close()
    if not args.apply:
        print("\nDry run only. Re-run with --apply to write changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
