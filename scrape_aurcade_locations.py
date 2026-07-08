#!/usr/bin/env python3
"""Scrape Aurcade locations into the canonical DuckDB database.

Aurcade's location browser is an ASP.NET WebForms page. The script first
collects location ids by posting each location type filter, then fetches each
detail page at a polite cadence and upserts normalized rows into DuckDB.
"""

from __future__ import annotations

import argparse
import html
from html.parser import HTMLParser
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from urllib.error import URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

import duckdb

from arcade_db import DEFAULT_DUCKDB, connect as duckdb_connect, execute_script


BASE_URL = "https://www.aurcade.com"
LOCATIONS_URL = f"{BASE_URL}/locations/"
USER_AGENT = "llm-eval aurcade scraper/1.0 (polite archival scrape)"


@dataclass
class LocationIndexRow:
    location_id: int
    name: str
    game_count: Optional[int]
    location_type: str
    city: str
    state: str
    is_public: Optional[bool]
    website_url: Optional[str]


@dataclass
class LocationDetail:
    location_id: int
    name: str
    location_type: Optional[str]
    updated_text: Optional[str]
    website_url: Optional[str]
    address_text: Optional[str]
    street_address: Optional[str]
    city: Optional[str]
    state: Optional[str]
    postal_code: Optional[str]
    phone: Optional[str]
    game_count: Optional[int]
    unique_game_count: Optional[int]
    world_record_count: Optional[int]
    description: Optional[str]
    latitude: Optional[float]
    longitude: Optional[float]


@dataclass
class LocationGame:
    location_id: int
    game_id: int
    name: str
    cabinet_type: Optional[str]
    manufacturer: Optional[str]
    year: Optional[int]
    players: Optional[int]
    controls_condition: Optional[int]
    screen_condition: Optional[int]
    cabinet_condition: Optional[int]


class SelectOptionParser(HTMLParser):
    def __init__(self, select_id: str) -> None:
        super().__init__(convert_charrefs=True)
        self.select_id = select_id
        self.in_select = False
        self.in_option = False
        self.current_value: Optional[str] = None
        self.options: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
        attr = dict(attrs)
        if tag == "select" and attr.get("id") == self.select_id:
            self.in_select = True
        elif self.in_select and tag == "option":
            self.in_option = True
            self.current_value = attr.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "option" and self.in_option:
            if self.current_value:
                self.options.append(self.current_value)
            self.in_option = False
            self.current_value = None
        elif tag == "select" and self.in_select:
            self.in_select = False


class TableParser(HTMLParser):
    def __init__(self, table_id: Optional[str] = None, div_id: Optional[str] = None) -> None:
        super().__init__(convert_charrefs=True)
        self.table_id = table_id
        self.div_id = div_id
        self.in_scope = div_id is None
        self.scope_depth = 0
        self.in_table = table_id is None
        self.table_depth = 0
        self.in_row = False
        self.in_cell = False
        self.current_row: List[dict[str, object]] = []
        self.current_cell: Optional[dict[str, object]] = None
        self.rows: List[List[dict[str, object]]] = []

    def handle_starttag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
        attr = dict(attrs)
        if not self.in_scope and self.div_id and tag == "div" and attr.get("id") == self.div_id:
            self.in_scope = True
            self.scope_depth = 1
            return
        if self.in_scope and self.div_id and tag == "div":
            self.scope_depth += 1

        if self.in_scope and not self.in_table and tag == "table" and attr.get("id") == self.table_id:
            self.in_table = True
            self.table_depth = 1
            return
        if self.in_table and tag == "table":
            self.table_depth += 1

        if not self.in_table:
            return

        if tag == "tr":
            self.in_row = True
            self.current_row = []
        elif self.in_row and tag in {"td", "th"}:
            self.in_cell = True
            self.current_cell = {"text": "", "links": [], "images": []}
        elif self.in_cell and tag == "a":
            href = attr.get("href")
            if href:
                self.current_cell["links"].append(href)  # type: ignore[index, union-attr]
        elif self.in_cell and tag == "img":
            self.current_cell["images"].append(dict(attr))  # type: ignore[index, union-attr]

    def handle_endtag(self, tag: str) -> None:
        if self.in_table and tag in {"td", "th"} and self.in_cell and self.current_cell is not None:
            self.current_cell["text"] = normalize_space(str(self.current_cell["text"]))
            self.current_row.append(self.current_cell)
            self.current_cell = None
            self.in_cell = False
        elif self.in_table and tag == "tr" and self.in_row:
            if self.current_row:
                self.rows.append(self.current_row)
            self.current_row = []
            self.in_row = False
        elif self.in_table and tag == "table":
            self.table_depth -= 1
            if self.table_depth == 0 and self.table_id is not None:
                self.in_table = False
        elif self.in_scope and self.div_id and tag == "div":
            self.scope_depth -= 1
            if self.scope_depth == 0:
                self.in_scope = False

    def handle_data(self, data: str) -> None:
        if self.in_cell and self.current_cell is not None:
            self.current_cell["text"] += data  # type: ignore[operator]


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def clean_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = normalize_space(value)
    return value or None


def to_int(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    digits = re.sub(r"[^0-9-]", "", value)
    return int(digits) if digits else None


def to_bool(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered == "yes":
        return True
    if lowered == "no":
        return False
    return None


def request_url(url: str, data: Optional[dict[str, str]] = None, timeout: int = 30) -> str:
    body = None if data is None else urlencode(data).encode()
    request = Request(
        url,
        data=body,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def fetch_with_retries(
    url: str,
    data: Optional[dict[str, str]] = None,
    retries: int = 3,
    delay: float = 1.0,
) -> str:
    last_error: Optional[Exception] = None
    for attempt in range(retries):
        try:
            return request_url(url, data=data)
        except URLError as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(delay * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def extract_hidden_fields(page: str) -> dict[str, str]:
    fields = {}
    for name in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"):
        match = re.search(
            rf'name="{re.escape(name)}"[^>]*value="([^"]*)"',
            page,
            flags=re.IGNORECASE,
        )
        if match:
            fields[name] = html.unescape(match.group(1))
    return fields


def extract_location_types(page: str) -> List[str]:
    parser = SelectOptionParser("ctl00_content_ddlType")
    parser.feed(page)
    return parser.options


def parse_index_rows(page: str) -> List[LocationIndexRow]:
    parser = TableParser(table_id="tblItems")
    parser.feed(page)
    rows: List[LocationIndexRow] = []

    for cells in parser.rows:
        if len(cells) < 8 or cells[0]["text"] == "#":
            continue
        links = cells[1]["links"]  # type: ignore[index]
        location_link = str(links[0]) if links else ""
        match = re.search(r"id=(\d+)", location_link)
        if not match:
            continue
        website_links = cells[7]["links"]  # type: ignore[index]
        rows.append(
            LocationIndexRow(
                location_id=int(match.group(1)),
                name=str(cells[1]["text"]),
                game_count=to_int(str(cells[2]["text"])),
                location_type=str(cells[3]["text"]),
                city=str(cells[4]["text"]),
                state=str(cells[5]["text"]),
                is_public=to_bool(str(cells[6]["text"])),
                website_url=str(website_links[0]) if website_links else None,
            )
        )
    return rows


def strip_tags(fragment: str) -> str:
    fragment = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.IGNORECASE)
    fragment = re.sub(r"<[^>]+>", "", fragment)
    return html.unescape(fragment).strip()


def extract_span_html(page: str, span_id: str) -> Optional[str]:
    match = re.search(
        rf'<span id="{re.escape(span_id)}">(.*?)</span>',
        page,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return match.group(1) if match else None


def extract_span_text(page: str, span_id: str) -> Optional[str]:
    fragment = extract_span_html(page, span_id)
    if fragment is None:
        return None
    return clean_text(strip_tags(fragment))


def extract_span_link(page: str, span_id: str) -> Optional[str]:
    fragment = extract_span_html(page, span_id)
    if fragment is None:
        return None
    match = re.search(r"<a\s+[^>]*href=['\"]([^'\"]+)['\"]", fragment, flags=re.IGNORECASE)
    return html.unescape(match.group(1)) if match else None


def parse_address(address_text: Optional[str]) -> dict[str, Optional[str]]:
    if not address_text:
        return {"street_address": None, "city": None, "state": None, "postal_code": None, "phone": None}
    lines = [line.strip() for line in address_text.splitlines() if line.strip()]
    phone = None
    if lines and re.search(r"\(\d{3}\)\s*\d{3}-\d{4}", lines[-1]):
        phone = lines.pop()
    street_address = lines[0] if lines else None
    city = state = postal_code = None
    if len(lines) > 1:
        match = re.match(r"(.+?),\s*([A-Z]{2})\s*(.*)$", lines[1])
        if match:
            city = match.group(1).strip()
            state = match.group(2).strip()
            postal_code = match.group(3).strip() or None
    return {
        "street_address": street_address,
        "city": city,
        "state": state,
        "postal_code": postal_code,
        "phone": phone,
    }


def parse_detail(location_id: int, page: str) -> LocationDetail:
    title_match = re.search(
        r'<div id="ctl00_pagetitle"[^>]*>\s*Location:\s*(.*?)\s*</div>',
        page,
        flags=re.IGNORECASE | re.DOTALL,
    )
    name = clean_text(strip_tags(title_match.group(1))) if title_match else str(location_id)

    address_text = extract_span_html(page, "ctl00_content_lblAddress")
    address_text = strip_tags(address_text) if address_text else None
    parsed_address = parse_address(address_text)

    lat_match = re.search(r"\bvar\s+lat\s*=\s*(-?\d+(?:\.\d+)?)", page)
    lon_match = re.search(r"\bvar\s+lon\s*=\s*(-?\d+(?:\.\d+)?)", page)

    return LocationDetail(
        location_id=location_id,
        name=name or str(location_id),
        location_type=extract_span_text(page, "ctl00_content_lblType"),
        updated_text=extract_span_text(page, "ctl00_content_lblUpdated"),
        website_url=extract_span_link(page, "ctl00_content_lblWebsite"),
        address_text=address_text,
        street_address=parsed_address["street_address"],
        city=parsed_address["city"],
        state=parsed_address["state"],
        postal_code=parsed_address["postal_code"],
        phone=parsed_address["phone"],
        game_count=to_int(extract_span_text(page, "ctl00_content_Games")),
        unique_game_count=to_int(extract_span_text(page, "ctl00_content_Unqiue")),
        world_record_count=to_int(extract_span_text(page, "ctl00_content_WorldRecords")),
        description=extract_span_text(page, "ctl00_content_lblDescription"),
        latitude=float(lat_match.group(1)) if lat_match else None,
        longitude=float(lon_match.group(1)) if lon_match else None,
    )


def condition_from_images(cell: dict[str, object]) -> List[Optional[int]]:
    values: List[Optional[int]] = []
    for image in cell.get("images", []):  # type: ignore[union-attr]
        if not isinstance(image, dict):
            continue
        alt = image.get("alt")
        values.append(to_int(str(alt)) if alt is not None else None)
    while len(values) < 3:
        values.append(None)
    return values[:3]


def parse_games(location_id: int, page: str) -> List[LocationGame]:
    parser = TableParser(div_id="LocationGames")
    parser.feed(page)
    games: List[LocationGame] = []
    for cells in parser.rows:
        if len(cells) < 7 or cells[0]["text"] == "#":
            continue
        links = cells[1]["links"]  # type: ignore[index]
        match = re.search(r"id=(\d+)", str(links[0]) if links else "")
        if not match:
            continue
        controls, screen, cabinet = condition_from_images(cells[6])
        games.append(
            LocationGame(
                location_id=location_id,
                game_id=int(match.group(1)),
                name=str(cells[1]["text"]),
                cabinet_type=clean_text(str(cells[2]["text"])),
                manufacturer=clean_text(str(cells[3]["text"])),
                year=to_int(str(cells[4]["text"])),
                players=to_int(str(cells[5]["text"])),
                controls_condition=controls,
                screen_condition=screen,
                cabinet_condition=cabinet,
            )
        )
    return games


def ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    execute_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS scrape_runs (
            id BIGINT,
            started_at VARCHAR NOT NULL,
            completed_at VARCHAR,
            source_url VARCHAR NOT NULL,
            include_games BIGINT NOT NULL DEFAULT 0,
            location_count BIGINT NOT NULL DEFAULT 0,
            game_count BIGINT NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS location_types (
            type VARCHAR
        );

        CREATE TABLE IF NOT EXISTS locations (
            location_id BIGINT,
            name VARCHAR NOT NULL,
            type VARCHAR,
            city VARCHAR,
            state VARCHAR,
            street_address VARCHAR,
            postal_code VARCHAR,
            phone VARCHAR,
            address_text VARCHAR,
            website_url VARCHAR,
            google_place_id VARCHAR,
            google_cid VARCHAR,
            is_public BIGINT,
            game_count BIGINT,
            unique_game_count BIGINT,
            world_record_count BIGINT,
            updated_text VARCHAR,
            description VARCHAR,
            latitude DOUBLE,
            longitude DOUBLE,
            detail_fetched_at VARCHAR,
            source_url VARCHAR NOT NULL
        );

        CREATE TABLE IF NOT EXISTS location_index_sources (
            location_id BIGINT NOT NULL,
            filter_type VARCHAR NOT NULL,
            seen_at VARCHAR NOT NULL
        );

        CREATE TABLE IF NOT EXISTS games (
            game_id BIGINT,
            name VARCHAR NOT NULL,
            manufacturer VARCHAR
        );

        CREATE TABLE IF NOT EXISTS location_games (
            location_id BIGINT NOT NULL,
            game_id BIGINT NOT NULL,
            cabinet_type VARCHAR,
            year BIGINT,
            players BIGINT,
            controls_condition BIGINT,
            screen_condition BIGINT,
            cabinet_condition BIGINT,
            fetched_at VARCHAR NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_locations_state_city ON locations(state, city);
        CREATE INDEX IF NOT EXISTS idx_locations_type ON locations(type);
        CREATE INDEX IF NOT EXISTS idx_location_games_location ON location_games(location_id);
        """
    )


def connect_db(path: Path) -> duckdb.DuckDBPyConnection:
    conn = duckdb_connect(path)
    ensure_schema(conn)
    return conn


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def next_scrape_run_id(conn: duckdb.DuckDBPyConnection) -> int:
    value = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM scrape_runs").fetchone()[0]
    return int(value)


def upsert_index_row(
    conn: duckdb.DuckDBPyConnection,
    row: LocationIndexRow,
    filter_type: str,
    seen_at: str,
) -> None:
    if not conn.execute("SELECT 1 FROM location_types WHERE type = ?", (filter_type,)).fetchone():
        conn.execute("INSERT INTO location_types(type) VALUES (?)", (filter_type,))
    values = (
        row.location_id,
        row.name,
        row.location_type,
        row.city,
        row.state,
        row.website_url,
        None if row.is_public is None else int(row.is_public),
        row.game_count,
        f"{BASE_URL}/locations/view.aspx?id={row.location_id}",
    )
    if conn.execute("SELECT 1 FROM locations WHERE location_id = ?", (row.location_id,)).fetchone():
        conn.execute(
            """
            UPDATE locations SET
                name = ?,
                type = COALESCE(type, ?),
                city = COALESCE(city, ?),
                state = COALESCE(state, ?),
                website_url = COALESCE(website_url, ?),
                is_public = COALESCE(is_public, ?),
                game_count = COALESCE(game_count, ?)
            WHERE location_id = ?
            """,
            (row.name, row.location_type, row.city, row.state, row.website_url, values[6], row.game_count, row.location_id),
        )
    else:
        conn.execute(
            """
            INSERT INTO locations (
                location_id, name, type, city, state, website_url, is_public,
                game_count, source_url
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
    conn.execute(
        "DELETE FROM location_index_sources WHERE location_id = ? AND filter_type = ?",
        (row.location_id, filter_type),
    )
    conn.execute(
        "INSERT INTO location_index_sources(location_id, filter_type, seen_at) VALUES (?, ?, ?)",
        (row.location_id, filter_type, seen_at),
    )


def upsert_detail(conn: duckdb.DuckDBPyConnection, detail: LocationDetail, fetched_at: str) -> None:
    values = (
        detail.location_id,
        detail.name,
        detail.location_type,
        detail.city,
        detail.state,
        detail.street_address,
        detail.postal_code,
        detail.phone,
        detail.address_text,
        detail.website_url,
        detail.game_count,
        detail.unique_game_count,
        detail.world_record_count,
        detail.updated_text,
        detail.description,
        detail.latitude,
        detail.longitude,
        fetched_at,
        f"{BASE_URL}/locations/view.aspx?id={detail.location_id}",
    )
    if conn.execute("SELECT 1 FROM locations WHERE location_id = ?", (detail.location_id,)).fetchone():
        conn.execute(
            """
            UPDATE locations SET
                name = ?,
                type = COALESCE(?, type),
                city = COALESCE(?, city),
                state = COALESCE(?, state),
                street_address = ?,
                postal_code = ?,
                phone = ?,
                address_text = ?,
                website_url = COALESCE(?, website_url),
                game_count = COALESCE(?, game_count),
                unique_game_count = ?,
                world_record_count = ?,
                updated_text = ?,
                description = ?,
                latitude = ?,
                longitude = ?,
                detail_fetched_at = ?,
                source_url = ?
            WHERE location_id = ?
            """,
            (*values[1:], detail.location_id),
        )
    else:
        conn.execute(
            """
            INSERT INTO locations (
                location_id, name, type, city, state, street_address, postal_code,
                phone, address_text, website_url, game_count, unique_game_count,
                world_record_count, updated_text, description, latitude, longitude,
                detail_fetched_at, source_url
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )


def upsert_games(conn: duckdb.DuckDBPyConnection, games: Iterable[LocationGame], fetched_at: str) -> int:
    count = 0
    for game in games:
        if conn.execute("SELECT 1 FROM games WHERE game_id = ?", (game.game_id,)).fetchone():
            conn.execute(
                """
                UPDATE games SET
                    name = ?,
                    manufacturer = COALESCE(?, manufacturer)
                WHERE game_id = ?
                """,
                (game.name, game.manufacturer, game.game_id),
            )
        else:
            conn.execute(
                "INSERT INTO games(game_id, name, manufacturer) VALUES (?, ?, ?)",
                (game.game_id, game.name, game.manufacturer),
            )
        location_game = (
            game.location_id,
            game.game_id,
            game.cabinet_type,
            game.year,
            game.players,
            game.controls_condition,
            game.screen_condition,
            game.cabinet_condition,
            fetched_at,
        )
        if conn.execute(
            "SELECT 1 FROM location_games WHERE location_id = ? AND game_id = ?",
            location_game[:2],
        ).fetchone():
            conn.execute(
                """
                UPDATE location_games SET
                    cabinet_type = ?,
                    year = ?,
                    players = ?,
                    controls_condition = ?,
                    screen_condition = ?,
                    cabinet_condition = ?,
                    fetched_at = ?
                WHERE location_id = ? AND game_id = ?
                """,
                (*location_game[2:], game.location_id, game.game_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO location_games (
                    location_id, game_id, cabinet_type, year, players,
                    controls_condition, screen_condition, cabinet_condition, fetched_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                location_game,
            )
        count += 1
    return count


def existing_detail_ids(conn: duckdb.DuckDBPyConnection) -> set[int]:
    return {
        int(row[0])
        for row in conn.execute(
            "SELECT location_id FROM locations WHERE detail_fetched_at IS NOT NULL"
        ).fetchall()
    }


def collect_index(
    conn: duckdb.DuckDBPyConnection,
    delay: float,
    verbose: bool,
) -> List[int]:
    page = fetch_with_retries(LOCATIONS_URL, delay=delay)
    hidden = extract_hidden_fields(page)
    location_types = extract_location_types(page)
    if not hidden or not location_types:
        raise RuntimeError("Could not find Aurcade hidden form fields or location types")

    seen_at = utc_now()
    ids: set[int] = set()
    for index, location_type in enumerate(location_types, start=1):
        fields = dict(hidden)
        fields.update(
            {
                "ctl00$content$txtName": "",
                "ctl00$content$ddlType": location_type,
                "ctl00$content$txtCity": "",
                "ctl00$content$txtState": "",
                "ctl00$content$btnGo": "Go",
            }
        )
        result_page = fetch_with_retries(LOCATIONS_URL, data=fields, delay=delay)
        rows = parse_index_rows(result_page)
        for row in rows:
            upsert_index_row(conn, row, location_type, seen_at)
            ids.add(row.location_id)
        if verbose:
            print(
                f"[index {index:02d}/{len(location_types)}] {location_type}: "
                f"{len(rows)} rows, {len(ids)} unique"
            )
        time.sleep(delay)
    return sorted(ids)


def fetch_details(
    conn: duckdb.DuckDBPyConnection,
    location_ids: List[int],
    delay: float,
    limit: Optional[int],
    resume: bool,
    include_games: bool,
    verbose: bool,
) -> tuple[int, int]:
    if resume:
        completed = existing_detail_ids(conn)
        location_ids = [location_id for location_id in location_ids if location_id not in completed]
    if limit is not None:
        location_ids = location_ids[:limit]

    detail_count = 0
    game_count = 0
    total = len(location_ids)
    for index, location_id in enumerate(location_ids, start=1):
        detail_url = f"{BASE_URL}/locations/view.aspx?id={location_id}"
        detail_page = fetch_with_retries(detail_url, delay=delay)
        fetched_at = utc_now()
        detail = parse_detail(location_id, detail_page)
        upsert_detail(conn, detail, fetched_at)
        detail_count += 1

        if include_games:
            games_url = f"{BASE_URL}/services/ws_games.aspx?a=lg&id={location_id}"
            games_page = fetch_with_retries(games_url, delay=delay)
            games = parse_games(location_id, games_page)
            game_count += upsert_games(conn, games, utc_now())
            time.sleep(delay)

        if verbose:
            print(f"[detail {index:04d}/{total:04d}] {location_id}: {detail.name}")
        time.sleep(delay)
    return detail_count, game_count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Politely scrape Aurcade locations into DuckDB."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DUCKDB,
        help=f"DuckDB database path (default: {DEFAULT_DUCKDB})",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Delay between requests in seconds (default: 0.5)",
    )
    parser.add_argument(
        "--index-only",
        action="store_true",
        help="Only collect filtered index rows; skip detail pages.",
    )
    parser.add_argument(
        "--include-games",
        action="store_true",
        help="Also fetch each location's game inventory endpoint.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Limit detail-page fetches, useful for smoke tests.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Refetch details even when a location already has detail_fetched_at.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    conn = connect_db(args.db)
    started_at = utc_now()
    run_id = next_scrape_run_id(conn)
    conn.execute(
        "INSERT INTO scrape_runs(id, started_at, source_url, include_games) VALUES (?, ?, ?, ?)",
        (run_id, started_at, LOCATIONS_URL, int(args.include_games)),
    )

    try:
        location_ids = collect_index(conn, args.delay, not args.quiet)
        detail_count = 0
        game_count = 0
        if not args.index_only:
            detail_count, game_count = fetch_details(
                conn=conn,
                location_ids=location_ids,
                delay=args.delay,
                limit=args.limit,
                resume=not args.no_resume,
                include_games=args.include_games,
                verbose=not args.quiet,
            )
        completed_at = utc_now()
        conn.execute(
            """
            UPDATE scrape_runs
            SET completed_at = ?, location_count = ?, game_count = ?
            WHERE id = ?
            """,
            (completed_at, len(location_ids), game_count, run_id),
        )
        print(
            f"Done. Indexed {len(location_ids)} locations, fetched {detail_count} "
            f"details, wrote {game_count} location-game rows to {args.db}."
        )
        return 0
    except KeyboardInterrupt:
        print("Interrupted; partial results are still in the database.", file=sys.stderr)
        return 130
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
