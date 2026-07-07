#!/usr/bin/env python3
"""Local Arcade Road Trip prototype.

A small Flask app that plans arcade stops between an origin and destination using
our curated SQLite database. This is intentionally local-first and provider-light:
Leaflet/OSM tiles in the browser, cached Nominatim geocoding, and OSRM demo
routing for route geometry.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from flask import Flask, jsonify, request


DB_PATH = Path("aurcade_locations.sqlite")
USER_AGENT = "arcade-road-trip-local/0.1 (local prototype)"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_ROUTE_URL = "https://router.project-osrm.org/route/v1/driving/{lon1},{lat1};{lon2},{lat2}"
ACTIVE_STATUSES = ("active", "unverified", "uncertain", "matched", "needs_review")
DEFAULT_MAX_DETOUR_MILES = 15.0
DEFAULT_LIMIT = 25
CONTINENTAL_US_STATES = (
    "AL",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "FL",
    "GA",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
    "DC",
)

app = Flask(__name__)


@dataclass(frozen=True)
class Point:
    lat: float
    lon: float


@dataclass(frozen=True)
class CandidateLocation:
    location_id: int
    name: str
    city: str
    state: str
    street_address: str
    game_count: int
    latitude: float
    longitude: float
    status: str
    pinball_games: int
    rhythm_games: int
    source_tags: str


@dataclass(frozen=True)
class Bounds:
    min_lat: float
    max_lat: float
    min_lon: float
    max_lon: float


def connect(readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def ensure_cache_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS geocode_cache (
            provider TEXT NOT NULL,
            query TEXT NOT NULL,
            response_json TEXT NOT NULL,
            fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (provider, query)
        );

        CREATE TABLE IF NOT EXISTS route_cache (
            provider TEXT NOT NULL,
            profile TEXT NOT NULL,
            origin_lat REAL NOT NULL,
            origin_lon REAL NOT NULL,
            dest_lat REAL NOT NULL,
            dest_lon REAL NOT NULL,
            response_json TEXT NOT NULL,
            fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (provider, profile, origin_lat, origin_lon, dest_lat, dest_lon)
        );
        """
    )


def has_table(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def http_json(url: str, params: Optional[dict[str, Any]] = None) -> Any:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def geocode(query: str) -> list[dict[str, Any]]:
    normalized = " ".join(query.split()).lower()
    with connect() as conn:
        ensure_cache_schema(conn)
        cached = conn.execute(
            "SELECT response_json FROM geocode_cache WHERE provider = 'nominatim' AND query = ?",
            (normalized,),
        ).fetchone()
        if cached:
            return json.loads(cached["response_json"])
        # Public Nominatim is for light explicit searches only. Keep this slow and cached.
        time.sleep(1.0)
        data = http_json(
            NOMINATIM_URL,
            {"q": query, "format": "jsonv2", "limit": 5, "countrycodes": "us"},
        )
        conn.execute(
            """
            INSERT INTO geocode_cache (provider, query, response_json)
            VALUES ('nominatim', ?, ?)
            ON CONFLICT(provider, query) DO UPDATE SET
                response_json = excluded.response_json,
                fetched_at = CURRENT_TIMESTAMP
            """,
            (normalized, json.dumps(data)),
        )
        conn.commit()
        return data


def route_between(origin: Point, destination: Point) -> dict[str, Any]:
    key = (
        round(origin.lat, 5),
        round(origin.lon, 5),
        round(destination.lat, 5),
        round(destination.lon, 5),
    )
    with connect() as conn:
        ensure_cache_schema(conn)
        cached = conn.execute(
            """
            SELECT response_json FROM route_cache
            WHERE provider = 'osrm' AND profile = 'driving'
              AND origin_lat = ? AND origin_lon = ? AND dest_lat = ? AND dest_lon = ?
            """,
            key,
        ).fetchone()
        if cached:
            return json.loads(cached["response_json"])
        url = OSRM_ROUTE_URL.format(lon1=origin.lon, lat1=origin.lat, lon2=destination.lon, lat2=destination.lat)
        data = http_json(url, {"overview": "full", "geometries": "geojson", "steps": "false"})
        conn.execute(
            """
            INSERT INTO route_cache (
                provider, profile, origin_lat, origin_lon, dest_lat, dest_lon, response_json
            ) VALUES ('osrm', 'driving', ?, ?, ?, ?, ?)
            ON CONFLICT(provider, profile, origin_lat, origin_lon, dest_lat, dest_lon) DO UPDATE SET
                response_json = excluded.response_json,
                fetched_at = CURRENT_TIMESTAMP
            """,
            (*key, json.dumps(data)),
        )
        conn.commit()
        return data


def haversine_miles(a: Point, b: Point) -> float:
    radius = 3958.7613
    phi1, phi2 = math.radians(a.lat), math.radians(b.lat)
    dphi = math.radians(b.lat - a.lat)
    dlambda = math.radians(b.lon - a.lon)
    h = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(h), math.sqrt(1 - h))


def project_distance_miles(point: Point, route_points: list[Point]) -> float:
    # Local prototype approximation: minimum distance to sampled route vertices.
    # Good enough to rank candidates; exact route detours can be added later.
    return min(haversine_miles(point, route_point) for route_point in route_points)


def route_bounds(route_points: list[Point], max_detour_miles: float) -> Bounds:
    min_lat = min(point.lat for point in route_points)
    max_lat = max(point.lat for point in route_points)
    min_lon = min(point.lon for point in route_points)
    max_lon = max(point.lon for point in route_points)
    lat_padding = max_detour_miles / 69.0
    mid_lat = (min_lat + max_lat) / 2
    miles_per_lon_degree = max(20.0, 69.0 * math.cos(math.radians(mid_lat)))
    lon_padding = max_detour_miles / miles_per_lon_degree
    return Bounds(
        min_lat=min_lat - lat_padding,
        max_lat=max_lat + lat_padding,
        min_lon=min_lon - lon_padding,
        max_lon=max_lon + lon_padding,
    )


def load_candidate_locations(scope: str = "US", bounds: Optional[Bounds] = None) -> list[CandidateLocation]:
    params: list[Any] = list(ACTIVE_STATUSES)
    scope = scope.upper()
    if scope == "US":
        state_clause = f"AND l.state IN ({','.join('?' for _ in CONTINENTAL_US_STATES)})"
        params.extend(CONTINENTAL_US_STATES)
    else:
        state_clause = "AND l.state = ?"
        params.append(scope)
    bounds_clause = ""
    if bounds:
        bounds_clause = "AND l.latitude BETWEEN ? AND ? AND l.longitude BETWEEN ? AND ?"
        params.extend([bounds.min_lat, bounds.max_lat, bounds.min_lon, bounds.max_lon])
    with connect(readonly=True) as conn:
        rows = conn.execute(
            f"""
            SELECT
                l.location_id,
                l.name,
                COALESCE(l.city, '') AS city,
                COALESCE(l.state, '') AS state,
                COALESCE(l.street_address, '') AS street_address,
                COALESCE(l.game_count, 0) AS game_count,
                l.latitude,
                l.longitude,
                COALESCE(ls.status, 'active') AS status,
                SUM(CASE WHEN lg.cabinet_type = 'Pinball' THEN 1 ELSE 0 END) AS pinball_games,
                SUM(CASE WHEN lower(COALESCE(lg.cabinet_type, '')) IN ('music game', 'rhythm')
                          OR lower(g.name) LIKE '%dance%'
                          OR lower(g.name) LIKE '%pump it up%'
                          OR lower(g.name) LIKE '%sound voltex%'
                         THEN 1 ELSE 0 END) AS rhythm_games,
                TRIM(
                    (CASE WHEN pll.location_id IS NOT NULL THEN 'Pinball Map ' ELSE '' END) ||
                    (CASE WHEN zll.location_id IS NOT NULL THEN 'ZIv ' ELSE '' END)
                ) AS source_tags
            FROM locations l
            LEFT JOIN location_statuses ls ON ls.location_id = l.location_id
            LEFT JOIN location_games lg ON lg.location_id = l.location_id
            LEFT JOIN games g ON g.game_id = lg.game_id
            LEFT JOIN pinballmap_location_links pll ON pll.location_id = l.location_id
            LEFT JOIN ziv_location_links zll ON zll.location_id = l.location_id
            WHERE COALESCE(ls.status, 'active') IN ({','.join('?' for _ in ACTIVE_STATUSES)})
              AND l.latitude IS NOT NULL AND l.longitude IS NOT NULL
              {state_clause}
              {bounds_clause}
            GROUP BY l.location_id
            """,
            params,
        ).fetchall()
    return [CandidateLocation(**dict(row)) for row in rows]


def batch_highlighted_games(location_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    if not location_ids:
        return {}
    placeholders = ",".join("?" for _ in location_ids)
    with connect(readonly=True) as conn:
        canonical_enabled = has_table(conn, "game_canonical_links")
        canonical_join = (
            "LEFT JOIN game_canonical_links gcl ON gcl.alias_game_id = g.game_id "
            "LEFT JOIN games cg ON cg.game_id = gcl.canonical_game_id"
            if canonical_enabled
            else ""
        )
        canonical_id_expr = "COALESCE(gcl.canonical_game_id, g.game_id)" if canonical_enabled else "g.game_id"
        canonical_name_expr = "COALESCE(cg.name, g.name)" if canonical_enabled else "g.name"
        inventory_rows = conn.execute(
            f"""
            SELECT
                lg.location_id,
                sl.state AS location_state,
                g.game_id,
                g.name,
                {canonical_id_expr} AS canonical_game_id,
                {canonical_name_expr} AS canonical_name,
                COALESCE(lg.cabinet_type, '') AS cabinet_type
            FROM location_games lg
            JOIN locations sl ON sl.location_id = lg.location_id
            JOIN games g ON g.game_id = lg.game_id
            {canonical_join}
            WHERE lg.location_id IN ({placeholders})
            """,
            location_ids,
        ).fetchall()

        canonical_game_ids = sorted({row["canonical_game_id"] for row in inventory_rows})
        if not canonical_game_ids:
            return {location_id: [] for location_id in location_ids}

        game_placeholders = ",".join("?" for _ in canonical_game_ids)
        state_placeholders = ",".join("?" for _ in CONTINENTAL_US_STATES)
        us_count_rows = conn.execute(
            f"""
            SELECT {canonical_id_expr} AS canonical_game_id, COUNT(DISTINCT lg.location_id) AS us_location_count
            FROM location_games lg
            JOIN games g ON g.game_id = lg.game_id
            {canonical_join}
            JOIN locations l ON l.location_id = lg.location_id
            LEFT JOIN location_statuses ls ON ls.location_id = l.location_id
            WHERE {canonical_id_expr} IN ({game_placeholders})
              AND l.state IN ({state_placeholders})
              AND COALESCE(ls.status, 'active') IN ('active', 'unverified', 'uncertain', 'matched', 'needs_review')
            GROUP BY {canonical_id_expr}
            """,
            [*canonical_game_ids, *CONTINENTAL_US_STATES],
        ).fetchall()
        states = sorted({row["location_state"] for row in inventory_rows if row["location_state"]})
        state_counts: dict[tuple[str, str], int] = {}
        if states:
            selected_state_placeholders = ",".join("?" for _ in states)
            state_count_rows = conn.execute(
                f"""
                SELECT {canonical_id_expr} AS canonical_game_id, l.state, COUNT(DISTINCT lg.location_id) AS state_location_count
                FROM location_games lg
                JOIN games g ON g.game_id = lg.game_id
                {canonical_join}
                JOIN locations l ON l.location_id = lg.location_id
                LEFT JOIN location_statuses ls ON ls.location_id = l.location_id
                WHERE {canonical_id_expr} IN ({game_placeholders})
                  AND l.state IN ({selected_state_placeholders})
                  AND COALESCE(ls.status, 'active') IN ('active', 'unverified', 'uncertain', 'matched', 'needs_review')
                GROUP BY {canonical_id_expr}, l.state
                """,
                [*canonical_game_ids, *states],
            ).fetchall()
            state_counts = {
                (row["canonical_game_id"], row["state"]): row["state_location_count"]
                for row in state_count_rows
            }

    us_counts = {row["canonical_game_id"]: row["us_location_count"] for row in us_count_rows}
    by_location: dict[int, list[dict[str, Any]]] = {location_id: [] for location_id in location_ids}
    for row in inventory_rows:
        us_location_count = us_counts.get(row["canonical_game_id"], 0)
        state_location_count = state_counts.get((row["canonical_game_id"], row["location_state"]), 0)
        by_location[row["location_id"]].append(
            {
                "game_id": row["game_id"],
                "name": row["name"],
                "canonical_game_id": row["canonical_game_id"],
                "canonical_name": row["canonical_name"],
                "rare_us": us_location_count < 10,
                "unique_state": state_location_count == 1,
                "us_location_count": us_location_count,
                "state_location_count": state_location_count,
                "location_state": row["location_state"],
                "cabinet_type": row["cabinet_type"],
            }
        )

    def sort_key(game: dict[str, Any]) -> tuple[int, int, int, str]:
        cabinet_type = (game.get("cabinet_type") or "").lower()
        if cabinet_type == "music game":
            type_order = 0
        elif game.get("cabinet_type") == "Pinball":
            type_order = 1
        else:
            type_order = 2
        return (
            0 if game["rare_us"] else 1,
            0 if game["unique_state"] else 1,
            type_order,
            game["name"],
        )

    for games in by_location.values():
        games.sort(key=sort_key)
        for game in games:
            game.pop("cabinet_type", None)
    return by_location


def highlighted_games(location_id: int) -> list[dict[str, Any]]:
    return batch_highlighted_games([location_id]).get(location_id, [])


def attach_highlights(stops: list[dict[str, Any]]) -> list[dict[str, Any]]:
    highlights = batch_highlighted_games([stop["location_id"] for stop in stops])
    for stop in stops:
        stop["highlights"] = highlights.get(stop["location_id"], [])
    return stops


def plan_stops(route: dict[str, Any], max_detour_miles: float, scope: str, limit: int) -> list[dict[str, Any]]:
    coords = route["routes"][0]["geometry"]["coordinates"]
    route_points = [Point(lat=lat, lon=lon) for lon, lat in coords]
    bounds = route_bounds(route_points, max_detour_miles)
    results = []
    for loc in load_candidate_locations(scope, bounds):
        point = Point(lat=float(loc.latitude), lon=float(loc.longitude))
        route_distance = project_distance_miles(point, route_points)
        estimated_detour = route_distance * 2
        if estimated_detour > max_detour_miles:
            continue
        source_bonus = 10 if loc.source_tags else 0
        score = loc.game_count * 1.5 + loc.pinball_games * 0.6 + loc.rhythm_games * 2.0 + source_bonus - estimated_detour * 2.5
        results.append(
            {
                "location_id": loc.location_id,
                "name": loc.name,
                "city": loc.city,
                "state": loc.state,
                "street_address": loc.street_address,
                "game_count": loc.game_count,
                "pinball_games": loc.pinball_games or 0,
                "rhythm_games": loc.rhythm_games or 0,
                "latitude": loc.latitude,
                "longitude": loc.longitude,
                "status": loc.status,
                "source_tags": loc.source_tags,
                "route_distance_miles": round(route_distance, 2),
                "estimated_detour_miles": round(estimated_detour, 2),
                "score": round(score, 2),
            }
        )
    return attach_highlights(sorted(results, key=lambda row: row["score"], reverse=True)[:limit])


@app.get("/")
def index() -> str:
    return INDEX_HTML


@app.get("/api/geocode")
def api_geocode():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "Missing q parameter"}), 400
    return jsonify({"results": geocode(query)})


@app.post("/api/route-plan")
def api_route_plan():
    payload = request.get_json(force=True)
    origin = Point(float(payload["origin"]["lat"]), float(payload["origin"]["lon"]))
    destination = Point(float(payload["destination"]["lat"]), float(payload["destination"]["lon"]))
    max_detour_miles = float(payload.get("max_detour_miles", DEFAULT_MAX_DETOUR_MILES))
    limit = int(payload.get("limit", DEFAULT_LIMIT))
    scope = payload.get("scope", "US")
    route = route_between(origin, destination)
    if not route.get("routes"):
        return jsonify({"error": "No route found", "route_response": route}), 502
    stops = plan_stops(route, max_detour_miles, scope, limit)
    return jsonify({"route": route["routes"][0], "stops": stops})


@app.get("/api/location/<int:location_id>")
def api_location(location_id: int):
    with connect(readonly=True) as conn:
        loc = conn.execute("SELECT * FROM locations WHERE location_id = ?", (location_id,)).fetchone()
        if not loc:
            return jsonify({"error": "Location not found"}), 404
        games = conn.execute(
            """
            SELECT g.name, g.manufacturer, lg.cabinet_type
            FROM location_games lg JOIN games g ON g.game_id = lg.game_id
            WHERE lg.location_id = ?
            ORDER BY COALESCE(lg.cabinet_type, ''), g.name
            """,
            (location_id,),
        ).fetchall()
    return jsonify({"location": dict(loc), "games": [dict(row) for row in games]})


INDEX_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Arcade Road Trip</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <style>
    html, body { height: 100%; }
    body { margin: 0; font-family: system-ui, -apple-system, Segoe UI, sans-serif; color: #17202a; overflow: hidden; }
    main { display: grid; grid-template-columns: minmax(480px, 1fr) minmax(480px, 1fr); height: 100vh; width: 100vw; overflow: hidden; }
    aside { border-right: 1px solid #d8dee4; min-height: 0; overflow: hidden; display: flex; flex-direction: column; background: white; }
    .controls { flex: 0 0 auto; padding: 14px 16px 10px; border-bottom: 1px solid #e6ebf0; }
    .results-shell { flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column; overflow: hidden; }
    .results { flex: 1 1 auto; min-height: 0; overflow-y: auto; overscroll-behavior: contain; padding: 0 16px 18px; scroll-padding-top: 12px; }
    h1 { font-size: 24px; margin: 0 0 10px; }
    label { display: block; font-size: 12px; font-weight: 650; margin: 0 0 4px; }
    input, button { width: 100%; box-sizing: border-box; font: inherit; padding: 8px 9px; margin: 0; }
    input { min-width: 0; }
    button { cursor: pointer; border: 0; background: #0d6efd; color: white; border-radius: 6px; font-weight: 700; white-space: nowrap; }
    button:disabled { cursor: wait; opacity: 0.72; }
    button.secondary { background: #425466; }
    .trip-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 8px; align-items: end; }
    .action-row { display: grid; grid-template-columns: minmax(130px, 180px) minmax(120px, 170px) minmax(110px, 150px); gap: 8px; align-items: end; margin-top: 8px; }
    .detour-field { min-width: 0; }
    .detour-field label { display: flex; justify-content: space-between; gap: 8px; }
    .section-title { flex: 0 0 auto; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px 16px 8px; border-bottom: 1px solid #edf1f5; }
    .section-title h2 { font-size: 16px; margin: 0; }
    #map { height: 100vh; min-width: 0; }
    .stop { border-top: 1px solid #e6ebf0; padding: 12px 0; cursor: pointer; border-radius: 6px; }
    .stop:hover { background: #f7f9fb; }
    .stop.active { background: #e8f1ff; outline: 2px solid #0d6efd; outline-offset: -2px; padding-left: 8px; padding-right: 8px; }
    .stop h2 { font-size: 17px; margin: 0 0 4px; }
    .meta { color: #51606d; font-size: 13px; }
    .games { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); column-gap: 16px; row-gap: 2px; font-size: 13px; margin-top: 8px; }
    .game { break-inside: avoid; display: block; line-height: 1.25; margin: 0 0 4px; }
    .rare-us { color: #d000ff; font-weight: 800; }
    .unique-state { font-weight: 800; }
    .machine-count { color: #51606d; font-size: 11px; font-weight: 650; margin-left: 3px; }
    .common-game { color: #51606d; font-weight: 400; }
    .legend { color: #51606d; font-size: 12px; line-height: 1.35; display: flex; flex-wrap: wrap; gap: 8px; }
    .error { color: #b00020; margin-top: 10px; }
    .loading { display: flex; align-items: center; gap: 10px; color: #425466; padding: 18px 0; font-weight: 650; }
    .car-track { width: 70px; height: 24px; position: relative; overflow: hidden; border-bottom: 2px dashed #ccd6df; }
    .car { position: absolute; left: -34px; bottom: 4px; width: 26px; height: 10px; background: #0d6efd; border-radius: 7px 9px 4px 4px; animation: drive 1.35s linear infinite; }
    .car::before { content: ""; position: absolute; left: 7px; top: -6px; width: 12px; height: 7px; background: #7fb3ff; border-radius: 7px 7px 0 0; }
    .car::after { content: ""; position: absolute; left: 4px; bottom: -4px; width: 4px; height: 4px; background: #17202a; border-radius: 50%; box-shadow: 14px 0 0 #17202a; }
    @keyframes drive { from { transform: translateX(0); } to { transform: translateX(106px); } }
    @media (max-width: 800px) {
      body { overflow: auto; }
      main { grid-template-columns: 1fr; grid-template-rows: minmax(320px, 52vh) minmax(0, 48vh); height: 100vh; }
      aside { grid-row: 2; border-right: 0; border-top: 1px solid #d8dee4; }
      #map { grid-row: 1; height: 100%; min-height: 0; }
      .controls { padding: 12px 14px 10px; }
      .results { padding: 0 14px 14px; }
      .section-title { padding: 10px 14px 8px; align-items: flex-start; flex-direction: column; gap: 4px; }
      h1 { font-size: 22px; margin-bottom: 8px; }
      .trip-grid, .action-row { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
<main>
  <aside>
    <div class="controls">
      <h1>Arcade Road Trip</h1>
      <div class="trip-grid">
        <div>
          <label>Origin</label>
          <input id="origin" value="South Jordan, UT" placeholder="South Jordan, UT" />
        </div>
        <div>
          <label>Destination</label>
          <input id="destination" value="Ogden, UT" placeholder="Ogden, UT" />
        </div>
      </div>
      <div class="action-row">
        <button class="secondary" id="current">Use current location</button>
        <div class="detour-field">
          <label><span>Max detour</span><span><span id="detourLabel">15</span> mi</span></label>
          <input id="detour" type="range" min="2" max="60" value="15" />
        </div>
        <button id="plan">Plan trip</button>
      </div>
      <div id="message" class="error"></div>
    </div>
    <div class="results-shell">
      <div class="section-title">
        <h2>Arcades:</h2>
        <div class="legend">
          <span class="rare-us">Under 10 in U.S.</span>
          <span class="unique-state">Only one in state</span>
          <span class="common-game">more common</span>
        </div>
      </div>
      <div id="stops" class="results"></div>
    </div>
  </aside>
  <div id="map"></div>
</main>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const continentalBounds = L.latLngBounds([24.396308, -124.848974], [49.384358, -66.885444]);
const map = L.map('map', {
  maxBounds: continentalBounds,
  maxBoundsViscosity: 1.0,
  minZoom: 4
}).setView([39.5, -98.35], 4);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
  attribution: '&copy; OpenStreetMap contributors'
}).addTo(map);
let routeLayer, markers = [];
let markerByLocationId = new Map();
const $ = id => document.getElementById(id);
$('detour').addEventListener('input', () => $('detourLabel').textContent = $('detour').value);
$('current').addEventListener('click', () => {
  navigator.geolocation?.getCurrentPosition(pos => {
    $('origin').value = `${pos.coords.latitude.toFixed(6)},${pos.coords.longitude.toFixed(6)}`;
  }, () => $('message').textContent = 'Could not get current location.');
});
function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
}
function renderHighlights(highlights) {
  return highlights.map(game => {
    const name = escapeHtml(game.name);
    let content = name;
    if (game.rare_us) {
      const count = `<span class="machine-count">(${game.us_location_count} US)</span>`;
      content = `<strong class="rare-us" title="Known at ${game.us_location_count} active continental U.S. location${game.us_location_count === 1 ? '' : 's'}">${name}${count}</strong>`;
    } else if (game.unique_state) {
      content = `<strong class="unique-state" title="Only known active ${escapeHtml(game.location_state)} location">${name}</strong>`;
    }
    return `<span class="game">${content}</span>`;
  }).join('');
}
function parsePoint(text) {
  const parts = text.split(',').map(v => Number(v.trim()));
  if (parts.length === 2 && parts.every(Number.isFinite)) return {lat: parts[0], lon: parts[1]};
  return null;
}
function selectStop(locationId, options = {}) {
  document.querySelectorAll('.stop.active').forEach(el => el.classList.remove('active'));
  const card = document.querySelector(`.stop[data-location-id="${locationId}"]`);
  if (card) {
    card.classList.add('active');
    if (options.scroll !== false) {
      const results = $('stops');
      results.scrollTo({top: card.offsetTop - results.offsetTop, behavior: 'smooth'});
    }
  }
  const marker = markerByLocationId.get(Number(locationId));
  if (marker) {
    marker.openPopup();
    if (options.pan !== false) map.panTo(marker.getLatLng(), {animate: true});
  }
}
async function geocode(text, label) {
  text = text.trim();
  if (!text) throw new Error(`Enter a ${label}.`);
  const point = parsePoint(text);
  if (point) return point;
  const res = await fetch(`/api/geocode?q=${encodeURIComponent(text)}`);
  const data = await res.json();
  if (!res.ok || !data.results?.length) throw new Error(`Could not geocode: ${text}`);
  return {lat: Number(data.results[0].lat), lon: Number(data.results[0].lon)};
}
$('plan').addEventListener('click', async () => {
  $('message').textContent = '';
  $('stops').innerHTML = `
    <div class="loading">
      <span>Planning your trip...</span>
      <span class="car-track" aria-hidden="true"><span class="car"></span></span>
    </div>`;
  $('plan').disabled = true;
  $('plan').textContent = 'Planning...';
  try {
    const origin = await geocode($('origin').value, 'starting point');
    const destination = await geocode($('destination').value, 'destination');
    const res = await fetch('/api/route-plan', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({origin, destination, max_detour_miles: Number($('detour').value), scope: 'US', limit: 25})
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Route failed');
    if (routeLayer) map.removeLayer(routeLayer);
    markers.forEach(m => map.removeLayer(m)); markers = []; markerByLocationId = new Map();
    routeLayer = L.geoJSON(data.route.geometry, {style: {color: '#0d6efd', weight: 5}}).addTo(map);
    map.fitBounds(routeLayer.getBounds(), {padding: [24, 24], maxZoom: 12});
    data.stops.forEach(stop => {
      const marker = L.marker([stop.latitude, stop.longitude]).addTo(map).bindPopup(`<b>${stop.name}</b><br>${stop.city}<br>${stop.estimated_detour_miles} mi detour`);
      marker.on('click', () => selectStop(stop.location_id, {pan: false}));
      markers.push(marker);
      markerByLocationId.set(Number(stop.location_id), marker);
    });
    $('stops').innerHTML = data.stops.map(stop => `
      <section class="stop" data-location-id="${stop.location_id}" tabindex="0" role="button" aria-label="Show ${stop.name} on map">
        <h2>${stop.name}</h2>
        <div class="meta">${stop.city}, ${stop.state} · ${stop.game_count} games · ${stop.estimated_detour_miles} mi estimated detour · ${stop.source_tags || 'local'}</div>
        <div class="games">${renderHighlights(stop.highlights)}</div>
      </section>`).join('') || '<p>No stops inside this detour budget.</p>';
    document.querySelectorAll('.stop[data-location-id]').forEach(card => {
      card.addEventListener('click', () => selectStop(card.dataset.locationId));
      card.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          selectStop(card.dataset.locationId);
        }
      });
    });
  } catch (err) {
    $('message').textContent = err.message;
    $('stops').innerHTML = '';
  } finally {
    $('plan').disabled = false;
    $('plan').textContent = 'Plan trip';
  }
});
</script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(debug=True)
