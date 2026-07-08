#!/usr/bin/env python3
"""Probe OpenStreetMap/Nominatim for location verification evidence.

The script records verification attempts in sidecar tables. It does not modify
the Aurcade-native locations/games/location_games tables.
"""

from __future__ import annotations

import argparse
import json
import math
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import duckdb

from arcade_db import DEFAULT_DUCKDB, connect as duckdb_connect, execute_script, rows as duckdb_rows
from arcade_query import fuzzy_score, normalize


DEFAULT_DB = DEFAULT_DUCKDB
NOMINATIM_URL = "https://nominatim.openstreetmap.org"
USER_AGENT = "aurcade-local-verifier/0.1 (personal local data cleanup)"


@dataclass(frozen=True)
class Candidate:
    match_kind: str
    name: Optional[str]
    address: Optional[str]
    lat: Optional[float]
    lon: Optional[float]
    distance_miles: Optional[float]
    confidence: float
    raw: dict[str, Any]


def connect(db_path: Path) -> duckdb.DuckDBPyConnection:
    return duckdb_connect(db_path)


def ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    execute_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS location_verifications (
            verification_id BIGINT,
            location_id BIGINT NOT NULL,
            checked_at VARCHAR NOT NULL,
            provider VARCHAR NOT NULL,
            status VARCHAR NOT NULL,
            match_kind VARCHAR,
            query VARCHAR,
            matched_name VARCHAR,
            matched_address VARCHAR,
            matched_latitude DOUBLE,
            matched_longitude DOUBLE,
            distance_miles DOUBLE,
            confidence DOUBLE,
            evidence_url VARCHAR,
            raw_json VARCHAR,
            notes VARCHAR
        );

        CREATE INDEX IF NOT EXISTS idx_location_verifications_location
            ON location_verifications(location_id, checked_at);

        CREATE TABLE IF NOT EXISTS location_statuses (
            location_id BIGINT,
            status VARCHAR NOT NULL,
            replacement_name VARCHAR,
            confidence DOUBLE,
            verified_at VARCHAR NOT NULL,
            evidence VARCHAR,
            notes VARCHAR
        );
        """
    )


def fetch_json(path: str, params: dict[str, str]) -> Any:
    url = f"{NOMINATIM_URL}{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def display_name(item: dict[str, Any]) -> Optional[str]:
    namedetails = item.get("namedetails") or {}
    address = item.get("address") or {}
    return (
        namedetails.get("name")
        or item.get("name")
        or address.get("amenity")
        or address.get("shop")
        or address.get("leisure")
        or item.get("display_name", "").split(",", 1)[0]
        or None
    )


def display_address(item: dict[str, Any]) -> Optional[str]:
    return item.get("display_name")


def parse_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_miles = 3958.7613
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return radius_miles * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def candidate_from_item(location: dict[str, Any], item: dict[str, Any], match_kind: str) -> Candidate:
    lat = parse_float(item.get("lat"))
    lon = parse_float(item.get("lon"))
    distance = None
    if lat is not None and lon is not None and location["latitude"] is not None and location["longitude"] is not None:
        distance = haversine_miles(float(location["latitude"]), float(location["longitude"]), lat, lon)
    name = display_name(item)
    address = display_address(item)
    name_score = fuzzy_score(location["name"], name or "")
    address_score = fuzzy_score(
        " ".join(str(location[key] or "") for key in ("street_address", "city", "state", "postal_code")),
        address or "",
    )
    distance_score = 1.0 if distance is None else max(0.0, 1.0 - min(distance, 5.0) / 5.0)
    if match_kind == "reverse":
        confidence = (name_score * 0.45) + (address_score * 0.25) + (distance_score * 0.30)
    else:
        confidence = (name_score * 0.55) + (address_score * 0.30) + (distance_score * 0.15)
    return Candidate(match_kind, name, address, lat, lon, distance, round(confidence, 3), item)


def classify(location: dict[str, Any], candidate: Optional[Candidate]) -> tuple[str, str]:
    if candidate is None:
        return "not_found", "No Nominatim result for name/address/reverse probes."
    name_score = fuzzy_score(location["name"], candidate.name or "")
    close = candidate.distance_miles is None or candidate.distance_miles <= 0.25
    if name_score >= 0.72 and close:
        return "matched", "Name and location are a plausible match."
    if close and candidate.match_kind in {"address", "reverse"}:
        return "possible_replaced", "Address/coordinates match but the mapped name differs."
    if candidate.confidence >= 0.55:
        return "needs_review", "Partial external match; review before curating status."
    return "not_found", "External result was weak or absent."


def search_name_address(location: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    query = " ".join(
        str(location[key] or "")
        for key in ("name", "street_address", "city", "state", "postal_code")
        if location[key]
    )
    return query, fetch_json(
        "/search",
        {
            "q": query,
            "format": "jsonv2",
            "addressdetails": "1",
            "namedetails": "1",
            "limit": "5",
            "countrycodes": "us",
        },
    )


def search_address(location: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    query = " ".join(
        str(location[key] or "")
        for key in ("street_address", "city", "state", "postal_code")
        if location[key]
    )
    return query, fetch_json(
        "/search",
        {
            "q": query,
            "format": "jsonv2",
            "addressdetails": "1",
            "namedetails": "1",
            "limit": "5",
            "countrycodes": "us",
        },
    )


def reverse_lookup(location: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    if location["latitude"] is None or location["longitude"] is None:
        return "", []
    query = f"{location['latitude']},{location['longitude']}"
    item = fetch_json(
        "/reverse",
        {
            "lat": str(location["latitude"]),
            "lon": str(location["longitude"]),
            "format": "jsonv2",
            "addressdetails": "1",
            "namedetails": "1",
            "zoom": "18",
        },
    )
    return query, [item] if isinstance(item, dict) and not item.get("error") else []


def best_candidate(location: dict[str, Any], delay_seconds: float) -> tuple[Optional[Candidate], str]:
    candidates: list[Candidate] = []
    query_parts: list[str] = []
    for match_kind, probe in (
        ("name_address", search_name_address),
        ("address", search_address),
        ("reverse", reverse_lookup),
    ):
        query, rows = probe(location)
        if query:
            query_parts.append(f"{match_kind}: {query}")
        for item in rows:
            candidates.append(candidate_from_item(location, item, match_kind))
        time.sleep(delay_seconds)
    if not candidates:
        return None, " | ".join(query_parts)
    candidates.sort(
        key=lambda candidate: (
            candidate.confidence,
            -(candidate.distance_miles or 0.0),
            1 if candidate.match_kind == "name_address" else 0,
        ),
        reverse=True,
    )
    return candidates[0], " | ".join(query_parts)


def locations_to_check(
    conn: duckdb.DuckDBPyConnection,
    state: str,
    limit: int,
    min_game_count: int,
    location_ids: Iterable[int],
    include_inactive: bool,
) -> list[dict[str, Any]]:
    ids = list(location_ids)
    if ids:
        placeholders = ",".join("?" for _ in ids)
        return duckdb_rows(
            conn,
            f"""
            SELECT *
            FROM locations
            WHERE location_id IN ({placeholders})
            ORDER BY game_count DESC, name
            """,
            ids,
        )
    inactive_filter = ""
    if not include_inactive:
        inactive_filter = """
        AND NOT EXISTS (
            SELECT 1
            FROM location_statuses ls
            WHERE ls.location_id = locations.location_id
              AND ls.status IN ('closed', 'replaced')
        )
        """
    return duckdb_rows(
        conn,
        f"""
        SELECT *
        FROM locations
        WHERE state = ?
          AND COALESCE(game_count, 0) >= ?
          {inactive_filter}
        ORDER BY game_count DESC, name
        LIMIT ?
        """,
        (state, min_game_count, limit),
    )


def record_verification(
    conn: duckdb.DuckDBPyConnection,
    location: dict[str, Any],
    status: str,
    notes: str,
    query: str,
    candidate: Optional[Candidate],
    checked_at: str,
    apply_status: bool,
) -> None:
    verification_id = conn.execute("SELECT COALESCE(MAX(verification_id), 0) + 1 FROM location_verifications").fetchone()[0]
    conn.execute(
        """
        INSERT INTO location_verifications (
            verification_id, location_id, checked_at, provider, status, match_kind, query,
            matched_name, matched_address, matched_latitude, matched_longitude,
            distance_miles, confidence, evidence_url, raw_json, notes
        )
        VALUES (?, ?, ?, 'nominatim', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            verification_id,
            location["location_id"],
            checked_at,
            status,
            candidate.match_kind if candidate else None,
            query,
            candidate.name if candidate else None,
            candidate.address if candidate else None,
            candidate.lat if candidate else None,
            candidate.lon if candidate else None,
            candidate.distance_miles if candidate else None,
            candidate.confidence if candidate else None,
            NOMINATIM_URL,
            json.dumps(candidate.raw, ensure_ascii=False) if candidate else None,
            notes,
        ),
    )
    if apply_status and status in {"matched", "needs_review"}:
        existing = conn.execute(
            "SELECT status FROM location_statuses WHERE location_id = ?",
            (location["location_id"],),
        ).fetchone()
        values = (
            location["location_id"],
            status,
            candidate.confidence if candidate else None,
            checked_at,
            notes,
        )
        if existing is None:
            conn.execute(
                """
                INSERT INTO location_statuses (
                    location_id, status, replacement_name, confidence, verified_at, evidence, notes
                )
                VALUES (?, ?, NULL, ?, ?, 'nominatim', ?)
                """,
                values,
            )
        elif existing[0] not in ("closed", "replaced"):
            conn.execute(
                """
                UPDATE location_statuses SET
                    status = ?,
                    confidence = ?,
                    verified_at = ?,
                    evidence = 'nominatim',
                    notes = ?
                WHERE location_id = ?
                """,
                (status, candidate.confidence if candidate else None, checked_at, notes, location["location_id"]),
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify arcade locations against OpenStreetMap/Nominatim.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--state", default="UT")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--min-game-count", type=int, default=1)
    parser.add_argument("--location-id", type=int, action="append", default=[])
    parser.add_argument("--delay-seconds", type=float, default=1.1)
    parser.add_argument("--include-inactive", action="store_true")
    parser.add_argument("--apply", action="store_true", help="Write verification rows and active/needs_review status rows.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    conn = connect(args.db)
    try:
        ensure_schema(conn)
        rows = locations_to_check(
            conn,
            args.state,
            args.limit,
            args.min_game_count,
            args.location_id,
            args.include_inactive,
        )
        print(f"checking {len(rows)} location(s)")
        for row in rows:
            candidate, query = best_candidate(row, args.delay_seconds)
            status, notes = classify(row, candidate)
            matched = candidate.name if candidate else ""
            distance = "" if not candidate or candidate.distance_miles is None else f"{candidate.distance_miles:.2f} mi"
            confidence = "" if not candidate else f"{candidate.confidence:.3f}"
            print(f"{row['location_id']}: {row['name']} -> {status} {matched!r} {distance} {confidence}")
            if args.apply:
                record_verification(conn, row, status, notes, query, candidate, checked_at, apply_status=True)
        if args.apply:
            conn.commit()
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
