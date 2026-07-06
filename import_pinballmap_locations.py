#!/usr/bin/env python3
"""Import a Pinball Map CSV into the existing Aurcade SQLite schema.

The Aurcade schema uses source-native integer primary keys. Pinball Map ids
share the same integer shape but not the same namespace, so this importer keeps
Aurcade ids positive and derives negative ids for Pinball Map-only records.

By default the script performs a dry run. Pass --apply to write changes.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


PINBALLMAP_ID_OFFSET = 1_000_000_000
PINBALLMAP_SOURCE_URL = "https://pinballmap.com/map?by_location_id={location_id}"
DEFAULT_LOCATION_MATCH_THRESHOLD = 0.78
DEFAULT_GAME_MATCH_THRESHOLD = 0.86


LOCATION_TYPE_MAP = {
    "arcade": "Arcade",
    "bar": "Bar",
    "bar + arcade": "Bar",
    "bar + restaurant": "Restaurant",
    "bowling alley": "Bowling Alley",
    "family fun center": "Family Center",
    "movie theater": "Cinema",
    "pizza parlor": "Restaurant",
    "restaurant": "Restaurant",
}

MANUFACTURER_ALIASES = {
    "american": "american",
    "american pinball": "american",
    "barrels of fun": "barrels of fun",
    "stern": "stern",
    "stern electronics": "stern electronics",
    "stern pinball": "stern",
    "bally": "bally",
    "bally midway": "bally",
    "chicago gaming": "chicago gaming",
    "chicago gaming company": "chicago gaming",
    "data east": "data east",
    "gottlieb": "gottlieb",
    "jersey jack": "jersey jack",
    "jersey jack pinball": "jersey jack",
    "sega": "sega",
    "spooky": "spooky",
    "spooky pinball": "spooky",
    "williams": "williams",
}

LOCATION_ID_OVERRIDES = {
    # Same West Jordan Nickel Mania location; source addresses differ.
    10933: 695,
}


@dataclass(frozen=True)
class PinballMapLocation:
    pinballmap_location_id: int
    name: str
    street_address: Optional[str]
    city: Optional[str]
    state: Optional[str]
    postal_code: Optional[str]
    phone: Optional[str]
    website_url: Optional[str]
    description: Optional[str]
    latitude: Optional[float]
    longitude: Optional[float]
    location_type: Optional[str]
    machine_count: int
    updated_text: Optional[str]


@dataclass(frozen=True)
class PinballMapMachine:
    pinballmap_machine_id: int
    name: str
    manufacturer: Optional[str]
    year: Optional[int]
    machine_type: Optional[str]
    machine_display: Optional[str]
    ipdb: Optional[str]
    opdb: Optional[str]


@dataclass(frozen=True)
class PinballMapPlacement:
    pinballmap_lmx_id: int
    pinballmap_location_id: int
    pinballmap_machine_id: int
    ic_enabled: Optional[bool]


@dataclass(frozen=True)
class ImportBundle:
    locations: list[PinballMapLocation]
    machines: dict[int, PinballMapMachine]
    placements: list[PinballMapPlacement]


@dataclass(frozen=True)
class ExistingLocation:
    location_id: int
    name: str
    city: Optional[str]
    state: Optional[str]
    street_address: Optional[str]
    postal_code: Optional[str]


@dataclass(frozen=True)
class ExistingGame:
    game_id: int
    name: str
    manufacturer: Optional[str]


@dataclass(frozen=True)
class LocationMatch:
    pinballmap_location_id: int
    location_id: int
    confidence: float
    method: str


@dataclass(frozen=True)
class GameMatch:
    pinballmap_machine_id: int
    game_id: int
    confidence: float
    method: str


@dataclass
class ImportStats:
    locations_matched: int = 0
    locations_reused: int = 0
    locations_inserted: int = 0
    locations_skipped: int = 0
    games_matched: int = 0
    games_reused: int = 0
    games_inserted: int = 0
    location_games_upserted: int = 0
    placements_skipped: int = 0


def clean_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    if stripped in {"", "-", " - ", "nil"}:
        return None
    return stripped


def parse_int(value: Optional[str]) -> Optional[int]:
    value = clean_text(value)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def parse_float(value: Optional[str]) -> Optional[float]:
    value = clean_text(value)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_bool(value: Optional[str]) -> Optional[bool]:
    value = clean_text(value)
    if value is None:
        return None
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return None


def split_list_field(value: Optional[str]) -> list[str]:
    value = clean_text(value)
    if value is None:
        return []
    return [part.strip() for part in value.split(",")]


def source_key_to_db_id(source_id: int) -> int:
    return -(PINBALLMAP_ID_OFFSET + source_id)


def normalize_for_match(value: Optional[str]) -> str:
    value = clean_text(value) or ""
    value = value.lower()
    value = value.replace("&", " and ")
    value = re.sub(r"\bste\b", "suite", value)
    value = re.sub(r"\bst\b", "street", value)
    value = re.sub(r"\bs\b", "south", value)
    value = re.sub(r"\bn\b", "north", value)
    value = re.sub(r"\be\b", "east", value)
    value = re.sub(r"\bw\b", "west", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_game_name(value: Optional[str]) -> str:
    name = normalize_for_match(value)
    if name.startswith("the "):
        name = name[4:]
    if name.endswith(" the"):
        name = name[:-4]
    name = re.sub(r"\b(pro|premium|le|limited edition|special|remake|se)\b", "", name)
    name = re.sub(r"\bpinball\b", "", name)
    return re.sub(r"\s+", " ", name).strip()


def normalize_manufacturer(value: Optional[str]) -> str:
    normalized = normalize_for_match(value)
    return MANUFACTURER_ALIASES.get(normalized, normalized)


def similarity(left: Optional[str], right: Optional[str]) -> float:
    left_norm = normalize_for_match(left)
    right_norm = normalize_for_match(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    if left_norm in right_norm or right_norm in left_norm:
        return 0.92
    return difflib.SequenceMatcher(None, left_norm, right_norm).ratio()


def game_similarity(left: Optional[str], right: Optional[str]) -> float:
    left_norm = normalize_game_name(left)
    right_norm = normalize_game_name(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    if left_norm in right_norm or right_norm in left_norm:
        return 0.9
    return difflib.SequenceMatcher(None, left_norm, right_norm).ratio()


def pinballmap_location_url(location_id: int) -> str:
    return PINBALLMAP_SOURCE_URL.format(location_id=location_id)


def read_pinballmap_csv(path: Path) -> ImportBundle:
    with path.open(newline="") as csv_file:
        rows = list(csv.DictReader(csv_file))

    locations: list[PinballMapLocation] = []
    machines: dict[int, PinballMapMachine] = {}
    placements: list[PinballMapPlacement] = []

    for row in rows:
        location_id = int(row["Id"])
        machine_count = int(row["Machine count"] or 0)
        raw_type = clean_text(row.get("Name [Location type]"))
        location_type = LOCATION_TYPE_MAP.get((raw_type or "").lower(), raw_type)
        locations.append(
            PinballMapLocation(
                pinballmap_location_id=location_id,
                name=row["Name"].strip(),
                street_address=clean_text(row.get("Street")),
                city=clean_text(row.get("City")),
                state=clean_text(row.get("State")),
                postal_code=clean_text(row.get("Zip")),
                phone=clean_text(row.get("Phone")),
                website_url=clean_text(row.get("Website")),
                description=clean_text(row.get("Description")),
                latitude=parse_float(row.get("Lat")),
                longitude=parse_float(row.get("Lon")),
                location_type=location_type,
                machine_count=machine_count,
                updated_text=clean_text(row.get("Date last updated")),
            )
        )

        machine_ids = split_list_field(row.get("Id [Machines]"))
        lmx_ids = split_list_field(row.get("Id [Location machine xrefs]"))
        names = split_list_field(row.get("Name [Machines]"))
        manufacturers = split_list_field(row.get("Manufacturer [Machines]"))
        years = split_list_field(row.get("Year [Machines]"))
        machine_types = split_list_field(row.get("Machine type [Machines]"))
        displays = split_list_field(row.get("Machine display [Machines]"))
        ipdbs = split_list_field(row.get("Ipdb [Machines]"))
        opdbs = split_list_field(row.get("Opdb [Machines]"))
        ic_enabled_values = split_list_field(row.get("Ic enabled [Location machine xrefs]"))

        if len(machine_ids) != machine_count:
            raise ValueError(
                f"Location {location_id} has Machine count={machine_count}, "
                f"but {len(machine_ids)} machine ids"
            )
        if len(lmx_ids) not in {0, machine_count}:
            raise ValueError(
                f"Location {location_id} has Machine count={machine_count}, "
                f"but {len(lmx_ids)} location-machine ids"
            )

        for index, machine_id_text in enumerate(machine_ids):
            machine_id = int(machine_id_text)
            machine = PinballMapMachine(
                pinballmap_machine_id=machine_id,
                name=names[index],
                manufacturer=clean_text(value_at(manufacturers, index)),
                year=parse_int(value_at(years, index)),
                machine_type=clean_text(value_at(machine_types, index)),
                machine_display=clean_text(value_at(displays, index)),
                ipdb=clean_text(value_at(ipdbs, index)),
                opdb=clean_text(value_at(opdbs, index)),
            )
            machines[machine_id] = merge_machine(machines.get(machine_id), machine)

            lmx_id = int(lmx_ids[index]) if lmx_ids else source_key_to_db_id(machine_id)
            placements.append(
                PinballMapPlacement(
                    pinballmap_lmx_id=lmx_id,
                    pinballmap_location_id=location_id,
                    pinballmap_machine_id=machine_id,
                    ic_enabled=parse_bool(value_at(ic_enabled_values, index)),
                )
            )

    return ImportBundle(locations=locations, machines=machines, placements=placements)


def value_at(values: list[str], index: int) -> Optional[str]:
    if index >= len(values):
        return None
    return values[index]


def merge_machine(
    existing: Optional[PinballMapMachine], incoming: PinballMapMachine
) -> PinballMapMachine:
    if existing is None:
        return incoming
    return PinballMapMachine(
        pinballmap_machine_id=incoming.pinballmap_machine_id,
        name=existing.name or incoming.name,
        manufacturer=existing.manufacturer or incoming.manufacturer,
        year=existing.year or incoming.year,
        machine_type=existing.machine_type or incoming.machine_type,
        machine_display=existing.machine_display or incoming.machine_display,
        ipdb=existing.ipdb or incoming.ipdb,
        opdb=existing.opdb or incoming.opdb,
    )


def location_match_score(
    pinballmap_location: PinballMapLocation, existing_location: ExistingLocation
) -> float:
    name_score = similarity(pinballmap_location.name, existing_location.name)
    city_score = similarity(pinballmap_location.city, existing_location.city)
    street_score = similarity(pinballmap_location.street_address, existing_location.street_address)
    zip_score = 0.0
    if pinballmap_location.postal_code and existing_location.postal_code:
        zip_score = 1.0 if pinballmap_location.postal_code == existing_location.postal_code else 0.0

    same_address = street_score >= 0.85 and zip_score == 1.0
    if city_score < 0.75 and not same_address:
        return 0.0

    if name_score >= 0.88 and city_score >= 0.9:
        if street_score >= 0.8 or zip_score == 1.0:
            return 0.4 * name_score + 0.25 * street_score + 0.25 * city_score + 0.1 * zip_score
        if not existing_location.street_address and not existing_location.postal_code:
            return 0.82

    score = 0.4 * name_score + 0.25 * street_score + 0.25 * city_score + 0.1 * zip_score
    if pinballmap_location.postal_code and existing_location.postal_code and zip_score == 0.0:
        score -= 0.15
    if (
        pinballmap_location.street_address
        and existing_location.street_address
        and street_score < 0.45
    ):
        score -= 0.15
    return max(0.0, score)


def best_location_match(
    pinballmap_location: PinballMapLocation,
    existing_locations: Iterable[ExistingLocation],
    threshold: float,
) -> Optional[LocationMatch]:
    best: Optional[LocationMatch] = None
    for existing_location in existing_locations:
        if pinballmap_location.state and existing_location.state:
            if pinballmap_location.state.upper() != existing_location.state.upper():
                continue
        confidence = location_match_score(pinballmap_location, existing_location)
        if best is None or confidence > best.confidence:
            best = LocationMatch(
                pinballmap_location_id=pinballmap_location.pinballmap_location_id,
                location_id=existing_location.location_id,
                confidence=confidence,
                method="fuzzy_name_address",
            )
    if best and best.confidence >= threshold:
        return best
    return None


def game_match_score(machine: PinballMapMachine, existing_game: ExistingGame) -> float:
    name_score = game_similarity(machine.name, existing_game.name)
    machine_manufacturer = normalize_manufacturer(machine.manufacturer)
    existing_manufacturer = normalize_manufacturer(existing_game.manufacturer)
    manufacturer_score = 0.0
    if machine_manufacturer and existing_manufacturer:
        manufacturer_score = 1.0 if machine_manufacturer == existing_manufacturer else 0.0
    elif not machine_manufacturer or not existing_manufacturer:
        manufacturer_score = 0.5
    score = 0.82 * name_score + 0.18 * manufacturer_score
    if manufacturer_score == 0.0:
        score -= 0.12
    return max(0.0, score)


def best_game_match(
    machine: PinballMapMachine,
    existing_games: Iterable[ExistingGame],
    threshold: float,
) -> Optional[GameMatch]:
    best: Optional[GameMatch] = None
    for existing_game in existing_games:
        confidence = game_match_score(machine, existing_game)
        if best is None or confidence > best.confidence:
            best = GameMatch(
                pinballmap_machine_id=machine.pinballmap_machine_id,
                game_id=existing_game.game_id,
                confidence=confidence,
                method="fuzzy_name_manufacturer",
            )
    if best and best.confidence >= threshold:
        return best
    return None


def load_existing_locations(conn: sqlite3.Connection) -> list[ExistingLocation]:
    rows = conn.execute(
        """
        SELECT location_id, name, city, state, street_address, postal_code
        FROM locations
        """
    )
    return [
        ExistingLocation(
            location_id=int(row[0]),
            name=str(row[1]),
            city=row[2],
            state=row[3],
            street_address=row[4],
            postal_code=row[5],
        )
        for row in rows
    ]


def load_existing_games(conn: sqlite3.Connection) -> list[ExistingGame]:
    rows = conn.execute("SELECT game_id, name, manufacturer FROM games")
    return [
        ExistingGame(game_id=int(row[0]), name=str(row[1]), manufacturer=row[2])
        for row in rows
    ]


def coalesced_update_location(
    conn: sqlite3.Connection,
    location_id: int,
    location: PinballMapLocation,
    fetched_at: str,
    source_url: str,
) -> None:
    conn.execute(
        """
        INSERT INTO locations (
            location_id, name, type, city, state, street_address, postal_code,
            phone, address_text, website_url, game_count, updated_text,
            description, latitude, longitude, detail_fetched_at, source_url
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(location_id) DO UPDATE SET
            name=COALESCE(locations.name, excluded.name),
            type=COALESCE(locations.type, excluded.type),
            city=COALESCE(locations.city, excluded.city),
            state=COALESCE(locations.state, excluded.state),
            street_address=COALESCE(locations.street_address, excluded.street_address),
            postal_code=COALESCE(locations.postal_code, excluded.postal_code),
            phone=COALESCE(locations.phone, excluded.phone),
            address_text=COALESCE(locations.address_text, excluded.address_text),
            website_url=COALESCE(locations.website_url, excluded.website_url),
            game_count=COALESCE(locations.game_count, excluded.game_count),
            updated_text=COALESCE(locations.updated_text, excluded.updated_text),
            description=COALESCE(locations.description, excluded.description),
            latitude=COALESCE(locations.latitude, excluded.latitude),
            longitude=COALESCE(locations.longitude, excluded.longitude),
            detail_fetched_at=COALESCE(locations.detail_fetched_at, excluded.detail_fetched_at),
            source_url=COALESCE(locations.source_url, excluded.source_url)
        """,
        (
            location_id,
            location.name,
            location.location_type,
            location.city,
            location.state,
            location.street_address,
            location.postal_code,
            location.phone,
            build_address_text(location),
            location.website_url,
            location.machine_count,
            location.updated_text,
            location.description,
            location.latitude,
            location.longitude,
            fetched_at,
            source_url,
        ),
    )


def build_address_text(location: PinballMapLocation) -> Optional[str]:
    parts = [
        location.street_address,
        " ".join(
            part
            for part in [location.city, location.state, location.postal_code]
            if part
        )
        or None,
        location.phone,
    ]
    return "\n".join(part for part in parts if part) or None


def upsert_game(conn: sqlite3.Connection, game_id: int, machine: PinballMapMachine) -> None:
    conn.execute(
        """
        INSERT INTO games(game_id, name, manufacturer)
        VALUES (?, ?, ?)
        ON CONFLICT(game_id) DO UPDATE SET
            name=COALESCE(games.name, excluded.name),
            manufacturer=COALESCE(games.manufacturer, excluded.manufacturer)
        """,
        (game_id, machine.name, machine.manufacturer),
    )


def upsert_location_game(
    conn: sqlite3.Connection,
    location_id: int,
    game_id: int,
    machine: PinballMapMachine,
    fetched_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO location_games (
            location_id, game_id, cabinet_type, year, players,
            controls_condition, screen_condition, cabinet_condition, fetched_at
        )
        VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, ?)
        ON CONFLICT(location_id, game_id) DO UPDATE SET
            cabinet_type=COALESCE(location_games.cabinet_type, excluded.cabinet_type),
            year=COALESCE(location_games.year, excluded.year),
            fetched_at=COALESCE(location_games.fetched_at, excluded.fetched_at)
        """,
        (location_id, game_id, "Pinball", machine.year, fetched_at),
    )


def connect(path: Path, readonly: bool) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
    return conn


def import_bundle(
    conn: sqlite3.Connection,
    bundle: ImportBundle,
    *,
    apply: bool,
    insert_unmatched_locations: bool,
    insert_unmatched_games: bool,
    location_match_threshold: float,
    game_match_threshold: float,
    verbose: bool,
) -> ImportStats:
    stats = ImportStats()
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    existing_locations = load_existing_locations(conn)
    existing_games = load_existing_games(conn)
    existing_location_ids = {location.location_id for location in existing_locations}
    existing_game_ids = {game.game_id for game in existing_games}
    positive_existing_locations = [
        location for location in existing_locations if location.location_id > 0
    ]
    positive_existing_games = [game for game in existing_games if game.game_id > 0]

    location_matches: dict[int, LocationMatch] = {}
    location_ids: dict[int, int] = {}
    for location in bundle.locations:
        if location.pinballmap_location_id in LOCATION_ID_OVERRIDES:
            location_id = LOCATION_ID_OVERRIDES[location.pinballmap_location_id]
            if location_id in existing_location_ids:
                location_matches[location.pinballmap_location_id] = LocationMatch(
                    pinballmap_location_id=location.pinballmap_location_id,
                    location_id=location_id,
                    confidence=1.0,
                    method="manual_override",
                )
                location_ids[location.pinballmap_location_id] = location_id
                stats.locations_matched += 1
                continue

        match = best_location_match(location, positive_existing_locations, location_match_threshold)
        if match:
            location_matches[location.pinballmap_location_id] = match
            location_ids[location.pinballmap_location_id] = match.location_id
            stats.locations_matched += 1
            continue

        derived_location_id = source_key_to_db_id(location.pinballmap_location_id)
        if derived_location_id in existing_location_ids:
            location_ids[location.pinballmap_location_id] = derived_location_id
            stats.locations_reused += 1
        elif insert_unmatched_locations:
            location_ids[location.pinballmap_location_id] = derived_location_id
            stats.locations_inserted += 1
        else:
            stats.locations_skipped += 1

    game_matches: dict[int, GameMatch] = {}
    game_ids: dict[int, int] = {}
    for machine in bundle.machines.values():
        match = best_game_match(machine, positive_existing_games, game_match_threshold)
        if match:
            game_matches[machine.pinballmap_machine_id] = match
            game_ids[machine.pinballmap_machine_id] = match.game_id
            stats.games_matched += 1
            continue

        derived_game_id = source_key_to_db_id(machine.pinballmap_machine_id)
        if derived_game_id in existing_game_ids:
            game_ids[machine.pinballmap_machine_id] = derived_game_id
            stats.games_reused += 1
        elif insert_unmatched_games:
            game_ids[machine.pinballmap_machine_id] = derived_game_id
            stats.games_inserted += 1

    for placement in bundle.placements:
        if (
            placement.pinballmap_location_id not in location_ids
            or placement.pinballmap_machine_id not in game_ids
        ):
            stats.placements_skipped += 1
            continue
        stats.location_games_upserted += 1

    print_plan(
        bundle,
        stats,
        location_matches,
        game_matches,
        location_ids,
        game_ids,
        verbose=verbose,
    )

    if not apply:
        return stats

    with conn:
        conn.execute("INSERT OR IGNORE INTO location_types(type) VALUES (?)", ("Pinball Map",))
        for location in bundle.locations:
            location_id = location_ids.get(location.pinballmap_location_id)
            if location_id is None:
                continue
            source_url = (
                pinballmap_location_url(location.pinballmap_location_id)
                if location_id < 0
                else f"https://www.aurcade.com/locations/view.aspx?id={location_id}"
            )
            coalesced_update_location(conn, location_id, location, fetched_at, source_url)

        for machine in bundle.machines.values():
            game_id = game_ids.get(machine.pinballmap_machine_id)
            if game_id is None:
                continue
            upsert_game(conn, game_id, machine)

        for placement in bundle.placements:
            location_id = location_ids.get(placement.pinballmap_location_id)
            game_id = game_ids.get(placement.pinballmap_machine_id)
            if location_id is None or game_id is None:
                continue
            upsert_location_game(
                conn,
                location_id,
                game_id,
                bundle.machines[placement.pinballmap_machine_id],
                fetched_at,
            )

    return stats


def print_plan(
    bundle: ImportBundle,
    stats: ImportStats,
    location_matches: dict[int, LocationMatch],
    game_matches: dict[int, GameMatch],
    location_ids: dict[int, int],
    game_ids: dict[int, int],
    *,
    verbose: bool,
) -> None:
    print("Pinball Map CSV import plan")
    print(f"  CSV locations: {len(bundle.locations)}")
    print(f"  CSV unique machines: {len(bundle.machines)}")
    print(f"  CSV location-machine placements: {len(bundle.placements)}")
    print(f"  Locations matched to Aurcade: {stats.locations_matched}")
    print(f"  Existing Pinball Map-only locations reused: {stats.locations_reused}")
    print(f"  Pinball Map-only locations to insert: {stats.locations_inserted}")
    print(f"  Locations skipped: {stats.locations_skipped}")
    print(f"  Machines matched to Aurcade games: {stats.games_matched}")
    print(f"  Existing Pinball Map-only games reused: {stats.games_reused}")
    print(f"  Pinball Map-only games to insert: {stats.games_inserted}")
    print(f"  Location-game rows to upsert: {stats.location_games_upserted}")
    print(f"  Placements skipped: {stats.placements_skipped}")

    if not verbose:
        return

    print("\nLocation matches:")
    by_location = {location.pinballmap_location_id: location for location in bundle.locations}
    for match in sorted(location_matches.values(), key=lambda item: item.pinballmap_location_id):
        location = by_location[match.pinballmap_location_id]
        print(
            f"  PM {match.pinballmap_location_id} {location.name!r} -> "
            f"Aurcade {match.location_id} ({match.confidence:.2f})"
        )

    print("\nSample Pinball Map-only locations:")
    inserted = [
        location
        for location in bundle.locations
        if location.pinballmap_location_id in location_ids
        and location_ids[location.pinballmap_location_id] < 0
    ]
    for location in inserted[:15]:
        print(
            f"  PM {location.pinballmap_location_id} {location.name!r} -> "
            f"{location_ids[location.pinballmap_location_id]}"
        )

    print("\nSample machine matches:")
    by_machine = bundle.machines
    for match in sorted(game_matches.values(), key=lambda item: item.pinballmap_machine_id)[:25]:
        machine = by_machine[match.pinballmap_machine_id]
        print(
            f"  PM {match.pinballmap_machine_id} {machine.name!r} -> "
            f"Aurcade game {match.game_id} ({match.confidence:.2f})"
        )

    print("\nSample Pinball Map-only games:")
    inserted_game_ids = [
        machine_id for machine_id, game_id in game_ids.items() if game_id < 0
    ]
    for machine_id in sorted(inserted_game_ids)[:25]:
        machine = bundle.machines[machine_id]
        print(f"  PM {machine_id} {machine.name!r} -> {game_ids[machine_id]}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Transform a Pinball Map location CSV into the Aurcade SQLite schema."
    )
    parser.add_argument("csv_path", type=Path, help="Pinball Map CSV export path")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("aurcade_locations.sqlite"),
        help="SQLite database path (default: aurcade_locations.sqlite)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes. Without this flag the script only prints a dry-run plan.",
    )
    parser.add_argument(
        "--matched-locations-only",
        action="store_true",
        help="Skip Pinball Map locations that cannot be matched to an existing Aurcade location.",
    )
    parser.add_argument(
        "--matched-games-only",
        action="store_true",
        help="Skip Pinball Map machines that cannot be matched to an existing Aurcade game.",
    )
    parser.add_argument(
        "--location-match-threshold",
        type=float,
        default=DEFAULT_LOCATION_MATCH_THRESHOLD,
        help=f"Minimum fuzzy confidence for location matches (default: {DEFAULT_LOCATION_MATCH_THRESHOLD}).",
    )
    parser.add_argument(
        "--game-match-threshold",
        type=float,
        default=DEFAULT_GAME_MATCH_THRESHOLD,
        help=f"Minimum fuzzy confidence for game matches (default: {DEFAULT_GAME_MATCH_THRESHOLD}).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print matched and inserted record samples.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bundle = read_pinballmap_csv(args.csv_path)
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
        )
    finally:
        conn.close()
    if not args.apply:
        print("\nDry run only. Re-run with --apply to write changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
