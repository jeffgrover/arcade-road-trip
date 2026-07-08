#!/usr/bin/env python3
"""Validate local locations against Pinball Map's public API.

Pinball Map is authoritative only for locations with pinball machines. A miss in
Pinball Map is not evidence that a venue is closed; it may simply have no pins.
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import duckdb

from arcade_db import DEFAULT_DUCKDB, connect as duckdb_connect, execute_script, rows as duckdb_rows
from import_pinballmap_locations import (
    LOCATION_ID_OVERRIDES,
    best_location_match,
    load_existing_locations,
    read_pinballmap_csv,
    source_key_to_db_id,
)


DEFAULT_DB = DEFAULT_DUCKDB
DEFAULT_CSV = Path("location_2026-07-05_15h22m53.csv")
PINBALLMAP_API = "https://pinballmap.com/api/v1/locations/{pinballmap_id}.json"
PINBALLMAP_URL_RE = re.compile(r"by_location_id=(\d+)")
USER_AGENT = "aurcade-pinballmap-validator/0.1 (personal local data cleanup)"


@dataclass(frozen=True)
class PinballMapLink:
    location_id: int
    pinballmap_location_id: int
    confidence: float
    method: str


def connect(db_path: Path) -> duckdb.DuckDBPyConnection:
    return duckdb_connect(db_path)


def ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    execute_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS pinballmap_location_links (
            location_id BIGINT NOT NULL,
            pinballmap_location_id BIGINT NOT NULL,
            confidence DOUBLE NOT NULL,
            method VARCHAR NOT NULL,
            linked_at VARCHAR NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_pinballmap_location_links_pinballmap
            ON pinballmap_location_links(pinballmap_location_id);

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


def pinballmap_id_from_url(source_url: Optional[str]) -> Optional[int]:
    if not source_url:
        return None
    match = PINBALLMAP_URL_RE.search(source_url)
    if not match:
        return None
    return int(match.group(1))


def discover_links(conn: duckdb.DuckDBPyConnection, csv_path: Optional[Path]) -> list[PinballMapLink]:
    links: dict[tuple[int, int], PinballMapLink] = {}
    for row in duckdb_rows(conn, "SELECT location_id, source_url FROM locations"):
        pinballmap_id = pinballmap_id_from_url(row["source_url"])
        if pinballmap_id is not None:
            link = PinballMapLink(row["location_id"], pinballmap_id, 1.0, "source_url")
            links[(link.location_id, link.pinballmap_location_id)] = link

    if csv_path and csv_path.exists():
        bundle = read_pinballmap_csv(csv_path)
        existing_locations = load_existing_locations(conn)
        for location in bundle.locations:
            if location.pinballmap_location_id in LOCATION_ID_OVERRIDES:
                link = PinballMapLink(
                    LOCATION_ID_OVERRIDES[location.pinballmap_location_id],
                    location.pinballmap_location_id,
                    1.0,
                    "manual_override",
                )
                links[(link.location_id, link.pinballmap_location_id)] = link
                continue
            match = best_location_match(location, existing_locations, threshold=0.78)
            if match is not None:
                link = PinballMapLink(
                    match.location_id,
                    location.pinballmap_location_id,
                    round(match.confidence, 3),
                    f"csv_{match.method}",
                )
                links[(link.location_id, link.pinballmap_location_id)] = link
    return sorted(links.values(), key=lambda link: (link.location_id, link.pinballmap_location_id))


def upsert_links(conn: duckdb.DuckDBPyConnection, links: list[PinballMapLink], linked_at: str) -> None:
    for link in links:
        row = (link.location_id, link.pinballmap_location_id, link.confidence, link.method, linked_at)
        conn.execute(
            "DELETE FROM pinballmap_location_links WHERE location_id = ? AND pinballmap_location_id = ?",
            row[:2],
        )
        conn.execute(
            """
            INSERT INTO pinballmap_location_links (
                location_id, pinballmap_location_id, confidence, method, linked_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            row,
        )


def fetch_pinballmap_location(pinballmap_id: int) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    url = PINBALLMAP_API.format(pinballmap_id=pinballmap_id)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8")), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {str(exc)[:180]}"


def classify_pinballmap(data: Optional[dict[str, Any]], error: Optional[str]) -> tuple[str, float, str]:
    if data is None:
        return "pinballmap_error", 0.0, error or "Pinball Map request failed."
    if data.get("id") is None:
        return "pinballmap_not_found", 0.0, "Pinball Map returned no location id."
    machine_count = int(data.get("machine_count") or data.get("num_machines") or 0)
    date_last_updated = data.get("date_last_updated") or ""
    user_submissions = int(data.get("user_submissions_count") or 0)
    ic_active = data.get("ic_active")
    if machine_count <= 0:
        return "pinballmap_no_machines", 0.65, "Location exists in Pinball Map but has no current machines."
    confidence = 0.82
    if date_last_updated >= "2025-01-01":
        confidence += 0.1
    if user_submissions >= 5:
        confidence += 0.05
    if ic_active is True:
        confidence += 0.03
    note = (
        f"Pinball Map id {data.get('id')} updated {date_last_updated}; "
        f"machine_count={machine_count}; user_submissions={user_submissions}; "
        f"ic_active={ic_active}. ic_active is competition/condition metadata, not venue-open status."
    )
    return "fresh_pinballmap", min(confidence, 0.99), note


def record_validation(
    conn: duckdb.DuckDBPyConnection,
    location_id: int,
    pinballmap_id: int,
    data: Optional[dict[str, Any]],
    status: str,
    confidence: float,
    notes: str,
    checked_at: str,
    apply_status: bool,
) -> None:
    matched_address = None
    lat = lon = None
    matched_name = None
    if data:
        matched_name = data.get("name")
        matched_address = ", ".join(
            part
            for part in [data.get("street"), data.get("city"), data.get("state"), data.get("zip")]
            if part
        )
        try:
            lat = float(data["lat"]) if data.get("lat") is not None else None
            lon = float(data["lon"]) if data.get("lon") is not None else None
        except (TypeError, ValueError):
            lat = lon = None
    verification_id = conn.execute("SELECT COALESCE(MAX(verification_id), 0) + 1 FROM location_verifications").fetchone()[0]
    conn.execute(
        """
        INSERT INTO location_verifications (
            verification_id, location_id, checked_at, provider, status, match_kind, query,
            matched_name, matched_address, matched_latitude, matched_longitude,
            distance_miles, confidence, evidence_url, raw_json, notes
        )
        VALUES (?, ?, ?, 'pinballmap', ?, 'pinballmap_location_id', ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
        """,
        (
            verification_id,
            location_id,
            checked_at,
            status,
            str(pinballmap_id),
            matched_name,
            matched_address,
            lat,
            lon,
            confidence,
            PINBALLMAP_API.format(pinballmap_id=pinballmap_id),
            json.dumps(data, ensure_ascii=False) if data else None,
            notes,
        ),
    )
    if apply_status and status == "fresh_pinballmap":
        existing = conn.execute(
            "SELECT status FROM location_statuses WHERE location_id = ?",
            (location_id,),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO location_statuses (
                    location_id, status, replacement_name, confidence, verified_at, evidence, notes
                )
                VALUES (?, 'matched', NULL, ?, ?, 'pinballmap', ?)
                """,
                (location_id, confidence, checked_at, notes),
            )
        elif existing[0] not in ("closed", "replaced"):
            conn.execute(
                """
                UPDATE location_statuses SET
                    status = 'matched',
                    confidence = ?,
                    verified_at = ?,
                    evidence = 'pinballmap',
                    notes = ?
                WHERE location_id = ?
                """,
                (confidence, checked_at, notes, location_id),
            )


def links_to_validate(
    conn: duckdb.DuckDBPyConnection,
    location_ids: list[int],
    state: Optional[str],
    limit: Optional[int],
    include_inactive: bool,
) -> list[dict[str, Any]]:
    clauses = []
    params: list[Any] = []
    if location_ids:
        clauses.append(f"l.location_id IN ({','.join('?' for _ in location_ids)})")
        params.extend(location_ids)
    if state:
        clauses.append("l.state = ?")
        params.append(state)
    if not include_inactive:
        clauses.append("COALESCE(ls.status, 'active') NOT IN ('closed', 'replaced')")
    where = " AND ".join(clauses) if clauses else "1=1"
    sql = f"""
    SELECT l.location_id, l.name, l.city, l.state, l.game_count,
           pll.pinballmap_location_id, pll.confidence AS link_confidence, pll.method
    FROM pinballmap_location_links pll
    JOIN locations l ON l.location_id = pll.location_id
    LEFT JOIN location_statuses ls ON ls.location_id = l.location_id
    WHERE {where}
    ORDER BY COALESCE(l.game_count, 0) DESC, l.name
    """
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return duckdb_rows(conn, sql, params)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate local locations against Pinball Map.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--state", default="UT")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--location-id", type=int, action="append", default=[])
    parser.add_argument("--include-inactive", action="store_true")
    parser.add_argument("--delay-seconds", type=float, default=0.25)
    parser.add_argument("--apply", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    conn = connect(args.db)
    try:
        ensure_schema(conn)
        links = discover_links(conn, args.csv)
        if args.apply:
            upsert_links(conn, links, checked_at)
            conn.commit()
        rows = links_to_validate(
            conn,
            args.location_id,
            args.state,
            args.limit,
            args.include_inactive,
        )
        print(f"known Pinball Map links: {len(links)}")
        print(f"validating {len(rows)} location(s)")
        for row in rows:
            data, error = fetch_pinballmap_location(row["pinballmap_location_id"])
            status, confidence, notes = classify_pinballmap(data, error)
            print(
                f"{row['location_id']}: {row['name']} -> {status} "
                f"pm={row['pinballmap_location_id']} confidence={confidence:.2f}"
            )
            if args.apply:
                record_validation(
                    conn,
                    row["location_id"],
                    row["pinballmap_location_id"],
                    data,
                    status,
                    confidence,
                    notes,
                    checked_at,
                    apply_status=True,
                )
                conn.commit()
            time.sleep(args.delay_seconds)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
