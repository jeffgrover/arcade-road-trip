#!/usr/bin/env python3
"""Slow opt-in Google Maps closure scanner.

This uses Google's public Maps URL scheme as a human-review signal. It is not
part of the default sync pipeline. Keep it low-volume, jittered, and explicit.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import duckdb

from arcade_db import (
    DEFAULT_DUCKDB,
    connect as duckdb_connect,
    continental_us_state_clause,
    continental_us_state_params,
    execute_script,
    has_table,
    rows as duckdb_rows,
)


DEFAULT_DB = DEFAULT_DUCKDB
GOOGLE_MAPS_SEARCH_BASE = "https://www.google.com/maps/search/"
USER_AGENT = "arcade-road-trip-local-closure-review/0.2"
SAMPLE_LOCATION_IDS = (214, 6137)
DEFAULT_MIN_DELAY_SECONDS = 45.0
DEFAULT_MAX_DELAY_SECONDS = 150.0
DEFAULT_STALE_DAYS = 180
DEFAULT_LIMIT = 50
RAW_TEXT_LIMIT = 12_000

PERMANENT_CLOSURE_PATTERNS = (
    re.compile(r"\bpermanently\s+closed\b", re.IGNORECASE),
    re.compile(r"\bclosed\s+permanently\b", re.IGNORECASE),
    re.compile(r"\bclosed_permanently\b", re.IGNORECASE),
    re.compile(r"\bpermanent(?:ly)?\s+closure\b", re.IGNORECASE),
)
TEMPORARY_CLOSURE_PATTERNS = (
    re.compile(r"\btemporarily\s+closed\b", re.IGNORECASE),
    re.compile(r"\bclosed\s+temporarily\b", re.IGNORECASE),
    re.compile(r"\bclosed_temporarily\b", re.IGNORECASE),
)


@dataclass(frozen=True)
class PageSignals:
    body_text: str
    title: str
    aria_labels: tuple[str, ...] = ()
    links: tuple[tuple[str, str, str], ...] = ()
    meta_text: str = ""
    html_excerpt: str = ""
    app_state: str = ""


@dataclass(frozen=True)
class PlaceMetadata:
    google_place_id: str | None = None
    google_cid: str | None = None
    website_url: str | None = None
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None


@dataclass(frozen=True)
class ClosureScan:
    query: str
    url: str
    status: str
    confidence: float
    matched_name: str | None
    metadata: PlaceMetadata
    notes: str
    raw_text: str
    signal_counts: dict[str, int]


def build_maps_search_url(query: str) -> str:
    return f"{GOOGLE_MAPS_SEARCH_BASE}?{urllib.parse.urlencode({'api': '1', 'query': query})}"


def location_query(location: dict[str, Any]) -> str:
    parts = [
        location.get("name"),
        location.get("street_address"),
        location.get("city"),
        location.get("state"),
        location.get("postal_code"),
    ]
    return " ".join(str(part).strip() for part in parts if part)


def compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def combined_signal_text(signals: PageSignals) -> str:
    return compact_text(
        " ".join(
            part
            for part in (
                signals.title,
                signals.meta_text,
                signals.body_text,
                " ".join(signals.aria_labels),
                " ".join(" ".join(link) for link in signals.links),
                signals.html_excerpt,
                signals.app_state,
            )
            if part
        )
    )


def count_patterns(text: str, patterns: Iterable[re.Pattern[str]]) -> int:
    return sum(len(pattern.findall(text)) for pattern in patterns)


def infer_place_name(signals: PageSignals, query: str) -> str | None:
    text = combined_signal_text(signals)
    query_name = query.split("  ", 1)[0]
    title = compact_text(re.sub(r"\s*-\s*Google Maps\s*$", "", signals.title, flags=re.IGNORECASE))
    if title and title.lower() not in {"google maps", "maps"}:
        return title
    for pattern in (
        r"Directions to ([^,|]+)",
        r"Search Result: ([^,|]+)",
        r"Share ([^,|]+)",
    ):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return compact_text(match.group(1))
    return compact_text(query_name) or None


def public_website_url(url: str) -> str | None:
    if not url:
        return None
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    host = parsed.netloc.lower()
    if host.endswith("google.com") or host.endswith("gstatic.com") or host.endswith("googleusercontent.com"):
        return None
    return url


def website_from_text(text: str) -> str | None:
    blocked_hosts = ("google.", "gstatic.", "googleusercontent.", "schema.org")
    for domain in re.findall(r"\b((?:[a-z0-9-]+\.)+[a-z]{2,})(?:/[^\s|]*)?", text, flags=re.IGNORECASE):
        host = domain.lower().strip(".")
        if any(part in host for part in blocked_hosts):
            continue
        if host in {"maps.google.com", "www.google.com"}:
            continue
        return f"https://{host}"
    return None


def extract_first(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return compact_text(match.group(1)) if match else None


def us_coordinate_pair(first: str, second: str) -> tuple[float, float] | None:
    a = float(first)
    b = float(second)
    if 20 <= a <= 50 and -130 <= b <= -60:
        return a, b
    if -130 <= a <= -60 and 20 <= b <= 50:
        return b, a
    return None


def extract_place_metadata(signals: PageSignals) -> PlaceMetadata:
    text = combined_signal_text(signals)
    google_place_id = extract_first(r"\b(ChIJ[A-Za-z0-9_-]+)\b", text)
    google_cid = extract_first(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)", text)
    website_url = None
    for label, _text, href in signals.links:
        if label.lower().startswith(("website:", "open website")):
            website_url = public_website_url(href)
            if website_url:
                break
    if website_url is None:
        for _label, _text, href in signals.links:
            website_url = public_website_url(href)
            if website_url:
                break
    if website_url is None:
        website_url = website_from_text(text)
    address = None
    for label in signals.aria_labels:
        if label.lower().startswith("address:"):
            address = compact_text(label.split(":", 1)[1])
            break
    if address is None:
        address = extract_first(r"\bAddress:\s*([^|]+?)(?:\s+Copy address\b|\s+Located in:|\s+Open\b|\s+Closed\b|$)", text)
    latitude = None
    longitude = None
    for first, second in re.findall(r"(-?\d+\.\d{5,}),(-?\d+\.\d{5,})", text):
        pair = us_coordinate_pair(first, second)
        if pair:
            latitude, longitude = pair
            break
    return PlaceMetadata(
        google_place_id=google_place_id,
        google_cid=google_cid,
        website_url=website_url,
        address=address,
        latitude=latitude,
        longitude=longitude,
    )


def scan_from_signals(query: str, url: str, signals: PageSignals) -> ClosureScan:
    text = combined_signal_text(signals)
    lower = text.lower()
    matched_name = infer_place_name(signals, query)
    metadata = extract_place_metadata(signals)
    permanent_count = count_patterns(text, PERMANENT_CLOSURE_PATTERNS)
    temporary_count = count_patterns(text, TEMPORARY_CLOSURE_PATTERNS)
    place_cues = sum(1 for cue in ("directions", "website", "reviews", "call", "save") if cue in lower)
    signal_counts = {
        "permanent_closure": permanent_count,
        "temporary_closure": temporary_count,
        "place_cues": place_cues,
    }
    if permanent_count:
        confidence = 0.98 if permanent_count >= 2 else 0.95
        return ClosureScan(
            query=query,
            url=url,
            status="closed",
            confidence=confidence,
            matched_name=matched_name,
            metadata=metadata,
            notes=f"Google Maps rendered explicit permanent-closure signal(s): {permanent_count}.",
            raw_text=text,
            signal_counts=signal_counts,
        )
    if temporary_count:
        return ClosureScan(
            query=query,
            url=url,
            status="needs_review",
            confidence=0.80,
            matched_name=matched_name,
            metadata=metadata,
            notes=f"Google Maps rendered explicit temporary-closure signal(s): {temporary_count}.",
            raw_text=text,
            signal_counts=signal_counts,
        )
    if "google maps" in lower and place_cues >= 2:
        return ClosureScan(
            query=query,
            url=url,
            status="matched",
            confidence=0.65,
            matched_name=matched_name,
            metadata=metadata,
            notes="Google Maps rendered a place page without an obvious permanent-closure label.",
            raw_text=text,
            signal_counts=signal_counts,
        )
    return ClosureScan(
        query=query,
        url=url,
        status="needs_review",
        confidence=0.20,
        matched_name=matched_name,
        metadata=metadata,
        notes="Google Maps page signals did not contain a clear place or closure signal.",
        raw_text=text,
        signal_counts=signal_counts,
    )


def parse_closure_signal(query: str, url: str, page_text: str) -> ClosureScan:
    return scan_from_signals(query, url, PageSignals(body_text=page_text, title="Google Maps"))


async def rendered_page_signals(url: str, timeout_ms: int, settle_ms: int) -> PageSignals:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise SystemExit(
            "Missing optional dependency: playwright. Install it and its browser with "
            "`python3 -m pip install playwright` and `python3 -m playwright install chromium`."
        ) from exc

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(user_agent=USER_AGENT)
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            await page.wait_for_timeout(settle_ms)
            title = await page.title()
            body_text = await page.locator("body").inner_text(timeout=timeout_ms)
            aria_labels = await page.locator("[aria-label]").evaluate_all(
                "(nodes) => nodes.map((node) => node.getAttribute('aria-label')).filter(Boolean).slice(0, 300)"
            )
            links = await page.locator("a[href]").evaluate_all(
                "(nodes) => nodes.map((node) => [node.getAttribute('aria-label') || '', node.innerText || '', node.href || '']).slice(0, 200)"
            )
            meta_text = await page.locator("meta[content]").evaluate_all(
                "(nodes) => nodes.map((node) => node.getAttribute('content')).filter(Boolean).slice(0, 100).join(' ')"
            )
            html_excerpt = (await page.content())[:RAW_TEXT_LIMIT]
            app_state = await page.evaluate(
                "() => globalThis.APP_INITIALIZATION_STATE ? JSON.stringify(globalThis.APP_INITIALIZATION_STATE).slice(0, 200000) : ''"
            )
            return PageSignals(
                body_text=body_text,
                title=title,
                aria_labels=tuple(str(label) for label in aria_labels),
                links=tuple((str(row[0]), str(row[1]), str(row[2])) for row in links),
                meta_text=str(meta_text),
                html_excerpt=html_excerpt,
                app_state=str(app_state),
            )
        finally:
            await browser.close()


async def scan_query(query: str, timeout_ms: int, settle_ms: int) -> ClosureScan:
    url = build_maps_search_url(query)
    signals = await rendered_page_signals(url, timeout_ms, settle_ms)
    return scan_from_signals(query, url, signals)


def connect(db_path: Path, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    return duckdb_connect(db_path, read_only=read_only)


def has_column(conn: duckdb.DuckDBPyConnection, table_name: str, column_name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'main'
          AND lower(table_name) = lower(?)
          AND lower(column_name) = lower(?)
        """,
        (table_name, column_name),
    ).fetchone()
    return row is not None


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
    if has_table(conn, "locations"):
        execute_script(
            conn,
            """
            ALTER TABLE locations ADD COLUMN IF NOT EXISTS google_place_id VARCHAR;
            ALTER TABLE locations ADD COLUMN IF NOT EXISTS google_cid VARCHAR;
            ALTER TABLE locations ADD COLUMN IF NOT EXISTS website_url VARCHAR;
            """
        )


def load_locations_by_id(conn: duckdb.DuckDBPyConnection, location_ids: Iterable[int]) -> list[dict[str, Any]]:
    ids = list(location_ids)
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    return duckdb_rows(
        conn,
        f"""
        SELECT location_id, name, street_address, city, state, postal_code, game_count
        FROM locations
        WHERE location_id IN ({placeholders})
        ORDER BY name
        """,
        ids,
    )


def load_scan_candidates(
    conn: duckdb.DuckDBPyConnection,
    state: str | None,
    limit: int,
    min_game_count: int,
    stale_days: int,
    include_inactive: bool,
) -> list[dict[str, Any]]:
    params: list[Any] = [min_game_count, stale_days]
    latest_google_cte = """
        latest_google AS (
            SELECT location_id, MAX(checked_at) AS last_google_checked_at
            FROM location_verifications
            WHERE provider = 'google_maps_url'
            GROUP BY location_id
        )
    """
    latest_join = "LEFT JOIN latest_google ON latest_google.location_id = l.location_id"
    latest_checked = "latest_google.last_google_checked_at"
    if not has_table(conn, "location_verifications"):
        latest_google_cte = "latest_google AS (SELECT NULL::BIGINT AS location_id, NULL::VARCHAR AS last_google_checked_at WHERE false)"
    status_join = "LEFT JOIN location_statuses ls ON ls.location_id = l.location_id"
    status_expr = "COALESCE(ls.status, 'active')"
    if not has_table(conn, "location_statuses"):
        status_join = ""
        status_expr = "'active'"
    state_filter = f"AND {continental_us_state_clause('l.state')}"
    params.extend(continental_us_state_params())
    if state:
        state_filter += " AND upper(l.state) = upper(?)"
        params.append(state)
    inactive_filter = ""
    if not include_inactive:
        inactive_filter = f"AND {status_expr} NOT IN ('closed', 'replaced')"
    params.append(limit)
    return duckdb_rows(
        conn,
        f"""
        WITH {latest_google_cte}
        SELECT
            l.location_id,
            l.name,
            l.street_address,
            l.city,
            l.state,
            l.postal_code,
            COALESCE(l.game_count, 0) AS game_count,
            {latest_checked} AS last_google_checked_at
        FROM locations l
        {status_join}
        {latest_join}
        WHERE COALESCE(l.game_count, 0) >= ?
          AND (
              latest_google.last_google_checked_at IS NULL
              OR CAST(latest_google.last_google_checked_at AS TIMESTAMP) < now() - (? * INTERVAL 1 DAY)
          )
          {state_filter}
          {inactive_filter}
        ORDER BY
            CASE WHEN latest_google.last_google_checked_at IS NULL THEN 0 ELSE 1 END,
            latest_google.last_google_checked_at,
            COALESCE(l.game_count, 0) DESC,
            l.name
        LIMIT ?
        """,
        params,
    )


def make_location_work_items(locations: Iterable[dict[str, Any]]) -> list[tuple[int | None, str]]:
    return [(int(row["location_id"]), location_query(row)) for row in locations]


def next_delay(min_seconds: float, max_seconds: float, rng: random.Random | None = None) -> float:
    if max_seconds < min_seconds:
        raise ValueError("--max-delay-seconds must be greater than or equal to --min-delay-seconds")
    rng = rng or random
    return rng.uniform(min_seconds, max_seconds)


def record_scan(
    conn: duckdb.DuckDBPyConnection,
    location_id: int,
    scan: ClosureScan,
    checked_at: str,
    apply_status: bool,
    overwrite_existing_details: bool = False,
) -> None:
    verification_id = conn.execute("SELECT COALESCE(MAX(verification_id), 0) + 1 FROM location_verifications").fetchone()[0]
    raw_json = json.dumps(
        {
            "signal_counts": scan.signal_counts,
            "place_metadata": {
                "google_place_id": scan.metadata.google_place_id,
                "google_cid": scan.metadata.google_cid,
                "website_url": scan.metadata.website_url,
                "address": scan.metadata.address,
                "latitude": scan.metadata.latitude,
                "longitude": scan.metadata.longitude,
            },
            "page_text_excerpt": scan.raw_text[:4000],
        },
        ensure_ascii=False,
    )
    conn.execute(
        """
        INSERT INTO location_verifications (
            verification_id, location_id, checked_at, provider, status, match_kind, query,
            matched_name, matched_address, matched_latitude, matched_longitude,
            distance_miles, confidence, evidence_url, raw_json, notes
        )
        VALUES (?, ?, ?, 'google_maps_url', ?, 'search_url', ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
        """,
        (
            verification_id,
            location_id,
            checked_at,
            scan.status,
            scan.query,
            scan.matched_name,
            scan.metadata.address,
            scan.metadata.latitude,
            scan.metadata.longitude,
            scan.confidence,
            scan.url,
            raw_json,
            scan.notes,
        ),
    )
    update_location_metadata(conn, location_id, scan.metadata, overwrite_existing_details)
    if apply_status and scan.status == "closed":
        existing = conn.execute("SELECT status FROM location_statuses WHERE location_id = ?", (location_id,)).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO location_statuses (
                    location_id, status, replacement_name, confidence, verified_at, evidence, notes
                )
                VALUES (?, 'closed', NULL, ?, ?, 'google_maps_url', ?)
                """,
                (location_id, scan.confidence, checked_at, scan.notes),
            )
        elif existing[0] not in ("closed", "replaced"):
            conn.execute(
                """
                UPDATE location_statuses SET
                    status = 'closed',
                    confidence = ?,
                    verified_at = ?,
                    evidence = 'google_maps_url',
                    notes = ?
                WHERE location_id = ?
                """,
                (scan.confidence, checked_at, scan.notes, location_id),
            )


def update_location_metadata(
    conn: duckdb.DuckDBPyConnection,
    location_id: int,
    metadata: PlaceMetadata,
    overwrite_existing_details: bool = False,
) -> None:
    assignments: list[str] = []
    values: list[Any] = []
    field_values = {
        "google_place_id": metadata.google_place_id,
        "google_cid": metadata.google_cid,
        "website_url": metadata.website_url,
        "street_address": metadata.address,
        "latitude": metadata.latitude,
        "longitude": metadata.longitude,
    }
    for field, value in field_values.items():
        if value is None or not has_column(conn, "locations", field):
            continue
        if overwrite_existing_details:
            assignments.append(f"{field} = ?")
        else:
            assignments.append(f"{field} = COALESCE({field}, ?)")
        values.append(value)
    if not assignments:
        return
    values.append(location_id)
    conn.execute(
        f"""
        UPDATE locations
        SET {", ".join(assignments)}
        WHERE location_id = ?
        """,
        values,
    )


def scan_log_prefix(location_id: int | None, query: str) -> str:
    return f"{location_id}: {query}" if location_id is not None else query


def metadata_summary(metadata: PlaceMetadata) -> str:
    parts = []
    if metadata.google_place_id:
        parts.append(f"place_id={metadata.google_place_id}")
    if metadata.google_cid:
        parts.append(f"cid={metadata.google_cid}")
    if metadata.website_url:
        parts.append(f"website={metadata.website_url}")
    if metadata.address:
        parts.append(f"address={metadata.address}")
    if metadata.latitude is not None and metadata.longitude is not None:
        parts.append(f"latlon={metadata.latitude:.6f},{metadata.longitude:.6f}")
    return "; ".join(parts)


def failed_scan(query: str, exc: Exception) -> ClosureScan:
    return ClosureScan(
        query=query,
        url=build_maps_search_url(query),
        status="scan_error",
        confidence=0.0,
        matched_name=None,
        metadata=PlaceMetadata(),
        notes=f"Google Maps scan failed: {type(exc).__name__}: {exc}",
        raw_text="",
        signal_counts={"permanent_closure": 0, "temporary_closure": 0, "place_cues": 0},
    )


async def scan_work_items(
    conn: duckdb.DuckDBPyConnection,
    work_items: list[tuple[int | None, str]],
    args: argparse.Namespace,
) -> int:
    scanned = 0
    for index, (location_id, query) in enumerate(work_items, start=1):
        if index > 1:
            delay = next_delay(args.min_delay_seconds, args.max_delay_seconds)
            print(f"sleeping {delay:.1f}s before next Google Maps request")
            await asyncio.sleep(delay)
        checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        try:
            scan = await scan_query(query, args.timeout_ms, args.settle_ms)
        except Exception as exc:
            scan = failed_scan(query, exc)
        scanned += 1
        print(
            f"{scan_log_prefix(location_id, query)} -> {scan.status} "
            f"({scan.confidence:.2f}) {scan.notes}"
        )
        print(f"  {scan.url}")
        metadata = metadata_summary(scan.metadata)
        if metadata:
            print(f"  metadata: {metadata}")
        if args.apply and location_id is not None:
            record_scan(
                conn,
                location_id,
                scan,
                checked_at,
                apply_status=True,
                overwrite_existing_details=args.overwrite_existing_details,
            )
            conn.commit()
    return scanned


async def scan_locations(args: argparse.Namespace) -> int:
    started = time.monotonic()
    total_scanned = 0
    conn = connect(args.db, read_only=not args.apply)
    try:
        if args.apply:
            ensure_schema(conn)
        while True:
            if args.sample:
                locations = load_locations_by_id(conn, SAMPLE_LOCATION_IDS)
            elif args.location_id:
                locations = load_locations_by_id(conn, args.location_id)
            else:
                batch_limit = args.limit
                if args.max_scans is not None:
                    remaining = args.max_scans - total_scanned
                    if remaining <= 0:
                        break
                    batch_limit = min(batch_limit, remaining)
                locations = load_scan_candidates(
                    conn,
                    args.state,
                    batch_limit,
                    args.min_game_count,
                    args.stale_days,
                    args.include_inactive,
                )
            work_items = make_location_work_items(locations)
            work_items.extend((None, query) for query in args.query)
            if not work_items:
                print("no eligible Google Maps closure scan candidates")
                break

            print(f"checking {len(work_items)} Google Maps URL search(es)")
            total_scanned += await scan_work_items(conn, work_items, args)
            if args.query or args.sample or args.location_id or not args.loop:
                break
            if args.max_scans is not None and total_scanned >= args.max_scans:
                break
            if args.max_runtime_minutes is not None:
                elapsed_minutes = (time.monotonic() - started) / 60
                if elapsed_minutes >= args.max_runtime_minutes:
                    break
    finally:
        conn.close()
    print(f"finished Google Maps closure scan; scanned={total_scanned}, apply={args.apply}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Probe Google Maps URL-rendered closure labels for locations.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--location-id", type=int, action="append", default=[])
    parser.add_argument("--query", action="append", default=[], help="Raw Google Maps search query.")
    parser.add_argument("--sample", action="store_true", help="Check Disney Quest and Arcade Monsters Oviedo.")
    parser.add_argument("--state", help="Limit automatic candidate selection to one state.")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Candidates per batch.")
    parser.add_argument("--min-game-count", type=int, default=1)
    parser.add_argument("--stale-days", type=int, default=DEFAULT_STALE_DAYS)
    parser.add_argument("--include-inactive", action="store_true")
    parser.add_argument("--loop", action="store_true", help="Keep selecting eligible batches until stopped or bounded.")
    parser.add_argument("--max-scans", type=int, help="Stop after this many scanned locations.")
    parser.add_argument("--max-runtime-minutes", type=float, help="Stop after roughly this many minutes.")
    parser.add_argument("--min-delay-seconds", type=float, default=DEFAULT_MIN_DELAY_SECONDS)
    parser.add_argument("--max-delay-seconds", type=float, default=DEFAULT_MAX_DELAY_SECONDS)
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument("--settle-ms", type=int, default=2000)
    parser.add_argument(
        "--overwrite-existing-details",
        action="store_true",
        help="Let Google Maps metadata overwrite existing website/address/coordinate fields when --apply is used.",
    )
    parser.add_argument("--apply", action="store_true", help="Write verification evidence and mark explicit permanent closures closed.")
    return parser


def main() -> int:
    return asyncio.run(scan_locations(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
