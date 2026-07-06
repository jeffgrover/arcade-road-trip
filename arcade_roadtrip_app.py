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


def load_candidate_locations(scope: str = "UT") -> list[CandidateLocation]:
    params: list[Any] = list(ACTIVE_STATUSES)
    state_clause = ""
    if scope.upper() != "US":
        state_clause = "AND l.state = ?"
        params.append(scope.upper())
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
            GROUP BY l.location_id
            """,
            params,
        ).fetchall()
    return [CandidateLocation(**dict(row)) for row in rows]


def highlighted_games(location_id: int) -> list[dict[str, Any]]:
    with connect(readonly=True) as conn:
        rows = conn.execute(
            """
            WITH active_ut_game_counts AS (
                SELECT lg.game_id, COUNT(DISTINCT lg.location_id) AS ut_location_count
                FROM location_games lg
                JOIN locations l ON l.location_id = lg.location_id
                LEFT JOIN location_statuses ls ON ls.location_id = l.location_id
                WHERE l.state = 'UT'
                  AND COALESCE(ls.status, 'active') IN ('active', 'unverified', 'uncertain', 'matched', 'needs_review')
                GROUP BY lg.game_id
            )
            SELECT g.game_id, g.name, COALESCE(augc.ut_location_count, 0) AS ut_location_count
            FROM location_games lg
            JOIN games g ON g.game_id = lg.game_id
            LEFT JOIN active_ut_game_counts augc ON augc.game_id = g.game_id
            WHERE lg.location_id = ?
            ORDER BY
                CASE WHEN COALESCE(augc.ut_location_count, 0) = 1 THEN 0 ELSE 1 END,
                CASE
                    WHEN lower(COALESCE(lg.cabinet_type, '')) = 'music game' THEN 0
                    WHEN lg.cabinet_type = 'Pinball' THEN 1
                    ELSE 2
                END,
                g.name
            """,
            (location_id,),
        ).fetchall()
    return [
        {
            "game_id": row["game_id"],
            "name": row["name"],
            "rare_utah": row["ut_location_count"] == 1,
            "ut_location_count": row["ut_location_count"],
        }
        for row in rows
    ]


def plan_stops(route: dict[str, Any], max_detour_miles: float, scope: str, limit: int) -> list[dict[str, Any]]:
    coords = route["routes"][0]["geometry"]["coordinates"]
    route_points = [Point(lat=lat, lon=lon) for lon, lat in coords]
    results = []
    for loc in load_candidate_locations(scope):
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
                "highlights": highlighted_games(loc.location_id),
            }
        )
    return sorted(results, key=lambda row: row["score"], reverse=True)[:limit]


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
    scope = payload.get("scope", "UT")
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
    body { margin: 0; font-family: system-ui, -apple-system, Segoe UI, sans-serif; color: #17202a; }
    main { display: grid; grid-template-columns: 390px 1fr; min-height: 100vh; }
    aside { padding: 18px; border-right: 1px solid #d8dee4; overflow: auto; }
    h1 { font-size: 26px; margin: 0 0 14px; }
    label { display: block; font-size: 13px; font-weight: 650; margin-top: 12px; }
    input, button { width: 100%; box-sizing: border-box; font: inherit; padding: 10px; margin-top: 5px; }
    button { cursor: pointer; border: 0; background: #0d6efd; color: white; border-radius: 6px; font-weight: 700; }
    button.secondary { background: #425466; }
    #map { min-height: 100vh; }
    .stop { border-top: 1px solid #e6ebf0; padding: 12px 0; cursor: pointer; border-radius: 6px; }
    .stop:hover { background: #f7f9fb; }
    .stop.active { background: #e8f1ff; outline: 2px solid #0d6efd; outline-offset: -2px; padding-left: 8px; padding-right: 8px; }
    .stop h2 { font-size: 17px; margin: 0 0 4px; }
    .meta { color: #51606d; font-size: 13px; }
    .games { column-count: 2; column-gap: 18px; font-size: 13px; margin-top: 8px; }
    .game { break-inside: avoid; display: block; line-height: 1.25; margin: 0 0 4px; }
    .rare-game { color: #d000ff; font-weight: 800; }
    @media (min-width: 1250px) { .games { column-count: 3; } }
    @media (max-width: 800px) { .games { column-count: 1; } }
    .error { color: #b00020; margin-top: 10px; }
    @media (max-width: 800px) { main { grid-template-columns: 1fr; } #map { min-height: 55vh; } }
  </style>
</head>
<body>
<main>
  <aside>
    <h1>Arcade Road Trip</h1>
    <label>Origin</label>
    <input id="origin" value="South Jordan, UT" placeholder="South Jordan, UT" />
    <button class="secondary" id="current">Use current location</button>
    <label>Destination</label>
    <input id="destination" value="Ogden, UT" placeholder="Ogden, UT" />
    <label>Max detour miles: <span id="detourLabel">15</span></label>
    <input id="detour" type="range" min="2" max="60" value="15" />
    <button id="plan">Plan trip</button>
    <div id="message" class="error"></div>
    <div id="stops"></div>
  </aside>
  <div id="map"></div>
</main>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const map = L.map('map').setView([40.76, -111.89], 8);
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
    const content = game.rare_utah ? `<strong class="rare-game" title="Only known Utah location">${name}</strong>` : name;
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
    if (options.scroll !== false) card.scrollIntoView({block: 'nearest', behavior: 'smooth'});
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
  $('stops').innerHTML = '';
  try {
    const origin = await geocode($('origin').value, 'starting point');
    const destination = await geocode($('destination').value, 'destination');
    const res = await fetch('/api/route-plan', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({origin, destination, max_detour_miles: Number($('detour').value), scope: 'UT', limit: 25})
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Route failed');
    if (routeLayer) map.removeLayer(routeLayer);
    markers.forEach(m => map.removeLayer(m)); markers = []; markerByLocationId = new Map();
    routeLayer = L.geoJSON(data.route.geometry, {style: {color: '#0d6efd', weight: 5}}).addTo(map);
    map.fitBounds(routeLayer.getBounds(), {padding: [24, 24]});
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
  } catch (err) { $('message').textContent = err.message; }
});
</script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(debug=True)
