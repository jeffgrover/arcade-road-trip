#!/usr/bin/env python3
"""Validate local U.S. locations against Zenius -I- vanisher arcade data.

ZIv is useful coverage for arcade/rhythm/motion games that Pinball Map will
miss. A miss in ZIv is not closure evidence; it may simply mean the community
has not cataloged that venue.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Optional


DEFAULT_DB = Path("aurcade_locations.sqlite")
ZIV_API = "https://zenius-i-vanisher.com/api/arcades.php"
ZIV_ARCADE_URL = "https://zenius-i-vanisher.com/v5.2/arcade.php?id={ziv_id}"
USER_AGENT = "aurcade-ziv-validator/0.1 (personal local data cleanup)"

US_STATES = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
    "DC": "District of Columbia",
}
STATE_ALIASES = {abbr.lower(): abbr for abbr in US_STATES}
STATE_ALIASES.update({name.lower(): abbr for abbr, name in US_STATES.items()})
STATE_ALIASES.update({"washington dc": "DC", "d.c.": "DC"})

GENERIC_NAME_WORDS = {
    "the",
    "and",
    "at",
    "of",
    "arcade",
    "arcades",
    "bar",
    "bowling",
    "entertainment",
    "family",
    "fun",
    "center",
    "centre",
    "games",
    "game",
    "amusement",
    "sports",
    "mall",
    "location",
}


@dataclass(frozen=True)
class LocalLocation:
    location_id: int
    name: str
    city: str
    state: str
    street_address: str
    postal_code: str
    latitude: Optional[float]
    longitude: Optional[float]
    status: str
    game_count: int


@dataclass(frozen=True)
class ZivArcade:
    ziv_id: int
    name: str
    city: str
    state: str
    address_line1: str
    address_line2: str
    postal_code: str
    latitude: Optional[float]
    longitude: Optional[float]
    website: str
    last_update_time: str
    raw: dict[str, Any]

    @property
    def evidence_url(self) -> str:
        return ZIV_ARCADE_URL.format(ziv_id=self.ziv_id)

    @property
    def matched_address(self) -> str:
        parts = [
            self.address_line1,
            self.address_line2,
            self.city,
            self.state,
            self.postal_code,
        ]
        return ", ".join(part for part in parts if part)


@dataclass(frozen=True)
class Match:
    local: LocalLocation
    ziv: ZivArcade
    confidence: float
    method: str
    distance_miles: Optional[float]
    name_score: float
    address_score: float


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ziv_location_links (
            location_id INTEGER NOT NULL REFERENCES locations(location_id) ON DELETE CASCADE,
            ziv_location_id INTEGER NOT NULL,
            confidence REAL NOT NULL,
            method TEXT NOT NULL,
            linked_at TEXT NOT NULL,
            PRIMARY KEY (location_id, ziv_location_id)
        );

        CREATE INDEX IF NOT EXISTS idx_ziv_location_links_ziv
            ON ziv_location_links(ziv_location_id);

        CREATE TABLE IF NOT EXISTS location_verifications (
            verification_id INTEGER PRIMARY KEY,
            location_id INTEGER NOT NULL REFERENCES locations(location_id) ON DELETE CASCADE,
            checked_at TEXT NOT NULL,
            provider TEXT NOT NULL,
            status TEXT NOT NULL,
            match_kind TEXT,
            query TEXT,
            matched_name TEXT,
            matched_address TEXT,
            matched_latitude REAL,
            matched_longitude REAL,
            distance_miles REAL,
            confidence REAL,
            evidence_url TEXT,
            raw_json TEXT,
            notes TEXT
        );
        """
    )


def normalize_state(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return STATE_ALIASES.get(value.strip().lower())


def normalize_text(value: Optional[str], *, drop_generic: bool = False) -> str:
    text = (value or "").lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    words = [word for word in text.split() if word]
    if drop_generic:
        words = [word for word in words if word not in GENERIC_NAME_WORDS]
    return " ".join(words)


def ratio(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    base = SequenceMatcher(None, left, right).ratio()
    left_words = set(left.split())
    right_words = set(right.split())
    if left_words and right_words:
        overlap = len(left_words & right_words) / min(len(left_words), len(right_words))
        base = max(base, overlap * 0.96)
    if len(left) >= 5 and len(right) >= 5 and (left in right or right in left):
        base = max(base, 0.94)
    return base


def haversine_miles(
    lat1: Optional[float],
    lon1: Optional[float],
    lat2: Optional[float],
    lon2: Optional[float],
) -> Optional[float]:
    if None in {lat1, lon1, lat2, lon2}:
        return None
    earth_radius_miles = 3958.7613
    phi1 = math.radians(float(lat1))
    phi2 = math.radians(float(lat2))
    dphi = math.radians(float(lat2) - float(lat1))
    dlambda = math.radians(float(lon2) - float(lon1))
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return earth_radius_miles * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def as_float(value: Any) -> Optional[float]:
    try:
        if value in {None, ""}:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_ziv_us_arcades(cache_path: Optional[Path], cache_hours: float) -> list[ZivArcade]:
    if cache_path and cache_path.exists():
        age_hours = (time.time() - cache_path.stat().st_mtime) / 3600
        if age_hours <= cache_hours:
            data = json.loads(cache_path.read_text())
            return parse_ziv_arcades(data)

    params = urllib.parse.urlencode(
        {
            "action": "query",
            "country": "United States of America",
            "skip_machines": "1",
            "skip_pictures": "1",
            "skip_visitors": "1",
            "skip_comments": "1",
        }
    )
    request = urllib.request.Request(
        f"{ZIV_API}?{params}",
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        data = json.loads(response.read().decode("utf-8"))
    if cache_path:
        cache_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return parse_ziv_arcades(data)


def parse_ziv_arcades(data: dict[str, Any]) -> list[ZivArcade]:
    arcades = []
    for row in data.get("arcades", []):
        state = normalize_state(row.get("subregion"))
        if not state:
            continue
        try:
            ziv_id = int(row["id"])
        except (KeyError, TypeError, ValueError):
            continue
        arcades.append(
            ZivArcade(
                ziv_id=ziv_id,
                name=(row.get("name") or "").strip(),
                city=(row.get("city") or "").strip(),
                state=state,
                address_line1=(row.get("addressLine1") or "").strip(),
                address_line2=(row.get("addressLine2") or "").strip(),
                postal_code=(row.get("postalCode") or "").strip(),
                latitude=as_float(row.get("latitude")),
                longitude=as_float(row.get("longitude")),
                website=(row.get("website") or "").strip(),
                last_update_time=(row.get("lastUpdateTime") or "").strip(),
                raw=row,
            )
        )
    return arcades


def load_local_locations(conn: sqlite3.Connection, include_inactive: bool) -> list[LocalLocation]:
    status_filter = "" if include_inactive else "AND COALESCE(ls.status, 'active') NOT IN ('closed', 'replaced')"
    rows = conn.execute(
        f"""
        SELECT l.location_id, l.name, l.city, l.state, l.street_address,
               l.postal_code, l.latitude, l.longitude, COALESCE(ls.status, 'active') AS status,
               COUNT(lg.game_id) AS game_count
        FROM locations l
        LEFT JOIN location_statuses ls ON ls.location_id = l.location_id
        LEFT JOIN location_games lg ON lg.location_id = l.location_id
        WHERE l.state IN ({",".join("?" for _ in US_STATES)}) {status_filter}
        GROUP BY l.location_id
        """,
        list(US_STATES),
    ).fetchall()
    return [
        LocalLocation(
            location_id=row["location_id"],
            name=row["name"] or "",
            city=row["city"] or "",
            state=row["state"] or "",
            street_address=row["street_address"] or "",
            postal_code=row["postal_code"] or "",
            latitude=row["latitude"],
            longitude=row["longitude"],
            status=row["status"],
            game_count=int(row["game_count"] or 0),
        )
        for row in rows
    ]


def score_pair(ziv: ZivArcade, local: LocalLocation) -> Optional[Match]:
    if ziv.state != local.state:
        return None
    name_score = ratio(
        normalize_text(ziv.name, drop_generic=True),
        normalize_text(local.name, drop_generic=True),
    )
    strict_name_score = ratio(normalize_text(ziv.name), normalize_text(local.name))
    name_score = max(name_score, strict_name_score)
    city_score = ratio(normalize_text(ziv.city), normalize_text(local.city))
    address_score = ratio(
        normalize_text(ziv.address_line1),
        normalize_text(local.street_address),
    )
    distance = haversine_miles(ziv.latitude, ziv.longitude, local.latitude, local.longitude)

    score = name_score * 0.62 + city_score * 0.23 + address_score * 0.15
    if distance is not None:
        if distance <= 0.15:
            score += 0.12
        elif distance <= 1.0:
            score += 0.05
        elif distance >= 20.0:
            score -= 0.2
    if city_score < 0.72:
        score -= 0.08
    score = max(0.0, min(score, 0.99))

    if score >= 0.92:
        method = "name_city_address"
    elif score >= 0.84:
        method = "probable_name_city"
    elif score >= 0.76:
        method = "possible_name_city"
    else:
        return None
    return Match(
        local=local,
        ziv=ziv,
        confidence=round(score, 3),
        method=method,
        distance_miles=round(distance, 3) if distance is not None else None,
        name_score=round(name_score, 3),
        address_score=round(address_score, 3),
    )


def best_matches(ziv_arcades: Iterable[ZivArcade], locals_: Iterable[LocalLocation]) -> list[Match]:
    by_state: dict[str, list[LocalLocation]] = {}
    for local in locals_:
        by_state.setdefault(local.state, []).append(local)

    chosen: list[Match] = []
    used_locations: set[int] = set()
    candidates: list[Match] = []
    for ziv in ziv_arcades:
        for local in by_state.get(ziv.state, []):
            match = score_pair(ziv, local)
            if match:
                candidates.append(match)

    candidates.sort(key=lambda match: match.confidence, reverse=True)
    used_ziv: set[int] = set()
    for match in candidates:
        if match.ziv.ziv_id in used_ziv or match.local.location_id in used_locations:
            continue
        chosen.append(match)
        used_ziv.add(match.ziv.ziv_id)
        used_locations.add(match.local.location_id)
    return sorted(chosen, key=lambda match: (match.local.state, match.local.city, match.local.name))


def upsert_links(conn: sqlite3.Connection, matches: list[Match], checked_at: str) -> None:
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
        [
            (
                match.local.location_id,
                match.ziv.ziv_id,
                match.confidence,
                match.method,
                checked_at,
            )
            for match in matches
        ],
    )


def insert_verifications(conn: sqlite3.Connection, matches: list[Match], checked_at: str) -> None:
    conn.executemany(
        """
        INSERT INTO location_verifications (
            location_id, checked_at, provider, status, match_kind, query,
            matched_name, matched_address, matched_latitude, matched_longitude,
            distance_miles, confidence, evidence_url, raw_json, notes
        )
        VALUES (?, ?, 'ziv', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                match.local.location_id,
                checked_at,
                "ziv_matched" if match.confidence >= 0.84 else "ziv_possible_match",
                match.method,
                str(match.ziv.ziv_id),
                match.ziv.name,
                match.ziv.matched_address,
                match.ziv.latitude,
                match.ziv.longitude,
                match.distance_miles,
                match.confidence,
                match.ziv.evidence_url,
                json.dumps(match.ziv.raw, ensure_ascii=False),
                (
                    f"ZIv id {match.ziv.ziv_id}; updated {match.ziv.last_update_time}; "
                    f"name_score={match.name_score}; address_score={match.address_score}. "
                    "ZIv confirms arcade/community catalog presence, not general business status."
                ),
            )
            for match in matches
        ],
    )


def print_report(
    ziv_arcades: list[ZivArcade],
    locals_: list[LocalLocation],
    matches: list[Match],
    limit: int,
) -> None:
    matched_ziv_ids = {match.ziv.ziv_id for match in matches}
    matched_location_ids = {match.local.location_id for match in matches}
    unmatched_ziv = [ziv for ziv in ziv_arcades if ziv.ziv_id not in matched_ziv_ids]
    unmatched_local = [local for local in locals_ if local.location_id not in matched_location_ids]
    high = [match for match in matches if match.confidence >= 0.84]
    possible = [match for match in matches if match.confidence < 0.84]

    print("# ZIv Validation Probe")
    print()
    print(f"- ZIv U.S. arcades fetched: {len(ziv_arcades)}")
    print(f"- Local active U.S. locations considered: {len(locals_)}")
    print(f"- Matched local locations: {len(matches)}")
    print(f"- High/probable matches: {len(high)}")
    print(f"- Possible matches: {len(possible)}")
    print(f"- ZIv locations not matched locally: {len(unmatched_ziv)}")
    print(f"- Local locations not found in ZIv: {len(unmatched_local)}")
    print()

    print("## Matches")
    print("| confidence | local_id | local | city | ZIv id | ZIv name | ZIv updated |")
    print("|---:|---:|---|---|---:|---|---|")
    for match in sorted(matches, key=lambda m: m.confidence, reverse=True)[:limit]:
        print(
            f"| {match.confidence:.3f} | {match.local.location_id} | "
            f"{match.local.name} | {match.local.city}, {match.local.state} | "
            f"{match.ziv.ziv_id} | {match.ziv.name} | {match.ziv.last_update_time} |"
        )
    print()

    print("## ZIv Locations Not Matched Locally")
    print("| ZIv id | name | city | state | updated |")
    print("|---:|---|---|---|---|")
    for ziv in sorted(unmatched_ziv, key=lambda z: z.last_update_time, reverse=True)[:limit]:
        print(f"| {ziv.ziv_id} | {ziv.name} | {ziv.city} | {ziv.state} | {ziv.last_update_time} |")
    print()

    print("## Local Locations Not Found In ZIv")
    print("| local_id | name | city | state | games |")
    print("|---:|---|---|---|---:|")
    for local in sorted(unmatched_local, key=lambda l: l.game_count, reverse=True)[:limit]:
        print(f"| {local.location_id} | {local.name} | {local.city} | {local.state} | {local.game_count} |")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate local locations against ZIv.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--cache", type=Path, default=Path("ziv_us_arcades_cache.json"))
    parser.add_argument("--cache-hours", type=float, default=24.0)
    parser.add_argument("--include-inactive", action="store_true")
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--apply", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    ziv_arcades = fetch_ziv_us_arcades(args.cache, args.cache_hours)
    conn = connect(args.db)
    try:
        ensure_schema(conn)
        locals_ = load_local_locations(conn, args.include_inactive)
        matches = best_matches(ziv_arcades, locals_)
        if args.apply:
            upsert_links(conn, matches, checked_at)
            insert_verifications(conn, matches, checked_at)
            conn.commit()
        print_report(ziv_arcades, locals_, matches, args.limit)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
