#!/usr/bin/env python3
"""Build Arcade Road Trip destination dashboard data.

The one-file atlas imports these data builders. The standalone HTML writer is
kept as a development aid, not as a checked-in product artifact.
"""

from __future__ import annotations

import argparse
import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import duckdb

from us_states import CONTINENTAL_US_STATES, US_STATES


DEFAULT_DB = Path("arcade_roadtrip.duckdb")
DEFAULT_OUTPUT = Path("static/dashboard.html")
ACTIVE_STATUSES = ("active", "unverified", "uncertain", "matched", "needs_review")
MIN_LAT = 24.396308
MAX_LAT = 49.384358
MIN_LON = -124.848974
MAX_LON = -66.885444


def connect(db_path: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(db_path), read_only=True)


def has_table(conn: duckdb.DuckDBPyConnection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE lower(table_name) = lower(?)",
        (table_name,),
    ).fetchone()
    return row is not None


def game_identity_cte(conn: duckdb.DuckDBPyConnection) -> str:
    if has_table(conn, "game_canonical_links"):
        return """
        game_identity AS (
            SELECT
                g.game_id,
                COALESCE(gcl.canonical_game_id, g.game_id) AS canonical_game_id
            FROM games g
            LEFT JOIN game_canonical_links gcl ON gcl.alias_game_id = g.game_id
        )
        """
    return """
    game_identity AS (
        SELECT g.game_id, g.game_id AS canonical_game_id
        FROM games g
    )
    """


def active_status_clause() -> str:
    return ",".join("?" for _ in ACTIVE_STATUSES)


def state_clause() -> str:
    return ",".join("?" for _ in CONTINENTAL_US_STATES)


def rows(conn: duckdb.DuckDBPyConnection, sql: str, params: Iterable[Any]) -> list[dict[str, Any]]:
    result = conn.execute(sql, list(params))
    columns = [description[0] for description in result.description]
    return [dict(zip(columns, row)) for row in result.fetchall()]


def load_location_metrics(conn: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    params = [*ACTIVE_STATUSES, *CONTINENTAL_US_STATES, *ACTIVE_STATUSES, *CONTINENTAL_US_STATES]
    sql = f"""
    WITH {game_identity_cte(conn)},
    active_locations AS (
        SELECT
            l.location_id,
            l.name,
            COALESCE(l.city, '') AS city,
            COALESCE(l.state, '') AS state,
            COALESCE(l.street_address, '') AS street_address,
            l.latitude,
            l.longitude
        FROM locations l
        LEFT JOIN location_statuses ls ON ls.location_id = l.location_id
        WHERE COALESCE(ls.status, 'active') IN ({active_status_clause()})
          AND l.state IN ({state_clause()})
    ),
    placement_identity AS (
        SELECT
            al.location_id,
            gi.canonical_game_id,
            COALESCE(lg.cabinet_type, '') AS cabinet_type
        FROM active_locations al
        JOIN location_games lg ON lg.location_id = al.location_id
        JOIN game_identity gi ON gi.game_id = lg.game_id
    ),
    national_counts AS (
        SELECT gi.canonical_game_id, COUNT(DISTINCT l.location_id) AS location_count
        FROM location_games lg
        JOIN game_identity gi ON gi.game_id = lg.game_id
        JOIN locations l ON l.location_id = lg.location_id
        LEFT JOIN location_statuses ls ON ls.location_id = l.location_id
        WHERE COALESCE(ls.status, 'active') IN ({active_status_clause()})
          AND l.state IN ({state_clause()})
        GROUP BY gi.canonical_game_id
    )
    SELECT
        al.location_id,
        al.name,
        al.city,
        al.state,
        al.street_address,
        al.latitude,
        al.longitude,
        COUNT(pi.canonical_game_id) AS machine_count,
        COUNT(DISTINCT pi.canonical_game_id) AS unique_game_count,
        SUM(CASE WHEN pi.cabinet_type = 'Pinball' THEN 1 ELSE 0 END) AS pinball_count,
        COUNT(DISTINCT CASE WHEN nc.location_count < 10 THEN pi.canonical_game_id END) AS rare_us_count
    FROM active_locations al
    LEFT JOIN placement_identity pi ON pi.location_id = al.location_id
    LEFT JOIN national_counts nc ON nc.canonical_game_id = pi.canonical_game_id
    GROUP BY
        al.location_id,
        al.name,
        al.city,
        al.state,
        al.street_address,
        al.latitude,
        al.longitude
    """
    return rows(conn, sql, params)


def city_key(row: dict[str, Any]) -> tuple[str, str]:
    return ((row.get("city") or "Unknown").strip() or "Unknown", row.get("state") or "")


def aggregate_locations(rows: list[dict[str, Any]], key_fields: tuple[str, ...]) -> list[dict[str, Any]]:
    buckets: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        key = tuple(row.get(field) for field in key_fields)
        bucket = buckets.setdefault(
            key,
            {
                **{field: row.get(field) for field in key_fields},
                "arcades": 0,
                "machines": 0,
                "rare_us_machines": 0,
                "pinball_machines": 0,
            },
        )
        bucket["arcades"] += 1
        bucket["machines"] += int(row.get("machine_count") or 0)
        bucket["rare_us_machines"] += int(row.get("rare_us_count") or 0)
        bucket["pinball_machines"] += int(row.get("pinball_count") or 0)
    return list(buckets.values())


def machine_distribution(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bins = [
        (1, 5, "1-5"),
        (6, 10, "6-10"),
        (11, 25, "11-25"),
        (26, 50, "26-50"),
        (51, 100, "51-100"),
        (101, 250, "101-250"),
        (251, 500, "251-500"),
        (501, 10_000, "501+"),
    ]
    distribution = [
        {"label": label, "min": low, "max": high, "arcades": 0, "machines": 0, "rare_us_machines": 0}
        for low, high, label in bins
    ]
    for row in rows:
        machines = int(row.get("machine_count") or 0)
        for bucket in distribution:
            if bucket["min"] <= machines <= bucket["max"]:
                bucket["arcades"] += 1
                bucket["machines"] += machines
                bucket["rare_us_machines"] += int(row.get("rare_us_count") or 0)
                break
    return distribution


def build_dashboard_data(conn: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    locations = load_location_metrics(conn)
    locations = [row for row in locations if int(row.get("machine_count") or 0) > 0]

    cities = aggregate_locations(
        [
            {
                **row,
                "city": (row.get("city") or "Unknown").strip() or "Unknown",
                "state": row.get("state") or "",
            }
            for row in locations
        ],
        ("city", "state"),
    )
    for city in cities:
        city["label"] = f"{city['city']}, {city['state']}"

    states = aggregate_locations(locations, ("state",))
    for state in states:
        state["label"] = US_STATES.get(state["state"], state["state"])

    all_arcades = [
        {
            "name": row["name"],
            "city": row["city"],
            "state": row["state"],
            "street_address": row["street_address"],
            "machines": int(row.get("machine_count") or 0),
            "unique_games": int(row.get("unique_game_count") or 0),
            "rare_us_machines": int(row.get("rare_us_count") or 0),
            "pinball_machines": int(row.get("pinball_count") or 0),
            "lat": row["latitude"],
            "lon": row["longitude"],
        }
        for row in locations
    ]
    largest_by_machines = sorted(all_arcades, key=lambda row: (-row["machines"], row["name"]))[:100]
    largest_by_rare = sorted(all_arcades, key=lambda row: (-row["rare_us_machines"], row["name"]))[:100]
    largest_arcades_by_name = {row["name"] + "|" + row["city"] + "|" + row["state"]: row for row in largest_by_machines}
    for row in largest_by_rare:
        largest_arcades_by_name[row["name"] + "|" + row["city"] + "|" + row["state"]] = row
    largest_arcades = list(largest_arcades_by_name.values())

    heat_points = [
        {
            "lat": float(row["latitude"]),
            "lon": float(row["longitude"]),
            "machines": int(row.get("machine_count") or 0),
            "rare": int(row.get("rare_us_count") or 0),
        }
        for row in locations
        if row.get("latitude") is not None and row.get("longitude") is not None
        and MIN_LAT <= float(row["latitude"]) <= MAX_LAT
        and MIN_LON <= float(row["longitude"]) <= MAX_LON
    ]

    return {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "totals": {
            "arcades": len(locations),
            "cities": len(cities),
            "states": len(states),
            "machines": sum(int(row.get("machine_count") or 0) for row in locations),
            "rare_us_machines": sum(int(row.get("rare_us_count") or 0) for row in locations),
        },
        "cities": cities,
        "states": states,
        "arcades": largest_arcades,
        "heat_points": heat_points,
        "machine_distribution": machine_distribution(locations),
    }


def js_data(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=True, separators=(",", ":")).replace("</", "<\\/")


def build_html(data: dict[str, Any]) -> str:
    generated = html.escape(data["generated_at"])
    payload = js_data(data)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Arcade Road Trip Destinations</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <style>
    :root {{
      --ink: #15202b;
      --muted: #5b6672;
      --line: #d9e1e8;
      --paper: #ffffff;
      --wash: #f4f7f9;
      --teal: #007c89;
      --gold: #f2a900;
      --coral: #e84d35;
      --rare: #d000ff;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: system-ui, -apple-system, Segoe UI, sans-serif; color: var(--ink); background: var(--wash); }}
    a {{ color: inherit; }}
    header {{ background: linear-gradient(120deg, #092f3d, #006d77 58%, #f2a900); color: white; padding: 28px min(5vw, 56px) 26px; }}
    nav {{ display: flex; justify-content: space-between; gap: 16px; align-items: center; margin-bottom: 22px; }}
    .brand {{ font-size: 18px; font-weight: 800; letter-spacing: .02em; }}
    .navlinks {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    .navlinks a, .cta {{ display: inline-flex; align-items: center; justify-content: center; min-height: 38px; padding: 8px 13px; border-radius: 6px; text-decoration: none; font-weight: 800; }}
    .navlinks a {{ background: rgba(255,255,255,.15); }}
    .cta {{ background: white; color: #09313c; }}
    .hero {{ display: grid; grid-template-columns: minmax(280px, 1fr) minmax(360px, .9fr); gap: 28px; align-items: end; }}
    h1 {{ font-size: clamp(34px, 5vw, 68px); line-height: .95; margin: 0 0 14px; max-width: 760px; }}
    .lede {{ font-size: 18px; line-height: 1.45; max-width: 690px; margin: 0; color: rgba(255,255,255,.88); }}
    .stat-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }}
    .stat {{ background: rgba(255,255,255,.16); border: 1px solid rgba(255,255,255,.22); border-radius: 8px; padding: 13px; backdrop-filter: blur(4px); }}
    .stat b {{ display: block; font-size: 30px; line-height: 1; }}
    .stat span {{ display: block; margin-top: 6px; color: rgba(255,255,255,.82); font-size: 13px; }}
    main {{ padding: 24px min(5vw, 56px) 46px; }}
    section {{ margin-top: 22px; }}
    .section-head {{ display: flex; justify-content: space-between; align-items: end; gap: 18px; margin-bottom: 10px; }}
    h2 {{ font-size: 24px; margin: 0; }}
    .note {{ color: var(--muted); font-size: 13px; margin: 4px 0 0; }}
    .grid-2 {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 18px; align-items: start; }}
    .matched-panel-grid {{ align-items: stretch; }}
    .matched-panel-grid > div {{ display: flex; flex-direction: column; min-height: 0; }}
    .map-shell {{ display: grid; gap: 10px; }}
    .panel {{ background: var(--paper); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; box-shadow: 0 8px 26px rgba(20, 38, 52, .07); }}
    .panel-pad {{ padding: 16px; }}
    #hotspot-map {{ height: min(68vh, 640px); min-height: 430px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ padding: 9px 10px; border-bottom: 1px solid #e8edf2; text-align: right; vertical-align: top; }}
    th:first-child, td:first-child {{ text-align: left; }}
    th {{ position: sticky; top: 0; background: #f9fbfc; z-index: 1; color: #344454; font-size: 12px; cursor: pointer; user-select: none; }}
    td.name {{ font-weight: 750; color: #203040; }}
    .table-wrap {{ max-height: 560px; overflow: auto; }}
    .matched-panel-grid .table-wrap, .chart-panel {{ height: 560px; }}
    .chart-panel {{ display: flex; align-items: stretch; }}
    .chart-panel svg {{ flex: 1 1 auto; min-height: 0; }}
    .sorter {{ display: inline-flex; gap: 6px; align-items: center; flex-wrap: wrap; }}
    .sorter button {{ border: 1px solid var(--line); background: white; border-radius: 999px; padding: 6px 10px; font: inherit; font-size: 12px; cursor: pointer; }}
    .sorter button.active {{ background: #0b5963; border-color: #0b5963; color: white; }}
    .bar-cell {{ min-width: 130px; }}
    .bar {{ display: grid; grid-template-columns: 1fr auto; gap: 8px; align-items: center; }}
    .bar span:first-child {{ height: 8px; border-radius: 999px; background: linear-gradient(90deg, var(--teal), var(--gold)); min-width: 2px; }}
    .small {{ color: var(--muted); font-size: 12px; }}
    .rare {{ color: var(--rare); font-weight: 800; }}
    #machine-distribution {{ width: 100%; height: 100%; display: block; }}
    .footer {{ margin-top: 26px; color: var(--muted); font-size: 13px; }}
    @media (max-width: 980px) {{
      .hero, .grid-2 {{ grid-template-columns: 1fr; }}
      header {{ padding: 22px 18px; }}
      main {{ padding: 18px; }}
      nav {{ align-items: flex-start; flex-direction: column; }}
    }}
  </style>
</head>
<body>
  <header>
    <nav>
      <div class="brand">Arcade Road Trip</div>
      <div class="navlinks">
        <a href="#cities">Destinations</a>
        <a href="#hotspots">Hotspots</a>
        <a class="cta" href="/static/arcade_road_trip.html">Open Atlas</a>
        <a class="cta" href="/static/arcade_road_trip.html">Plan a Route</a>
      </div>
    </nav>
    <div class="hero">
      <div>
        <h1>Find arcade cities worth traveling for.</h1>
        <p class="lede">A static snapshot of the curated U.S. arcade database: destination cities, machine-dense states, rare-game concentrations, national hotspots, and individual arcades that can anchor a trip.</p>
      </div>
      <div class="stat-grid">
        <div class="stat"><b id="total-arcades">0</b><span>active continental U.S. arcades</span></div>
        <div class="stat"><b id="total-machines">0</b><span>known machine placements</span></div>
        <div class="stat"><b id="total-rare">0</b><span>rare U.S. game/location hits</span></div>
        <div class="stat"><b id="total-cities">0</b><span>cities with playable locations</span></div>
      </div>
    </div>
  </header>
  <main>
    <section id="hotspots" class="map-shell">
      <div class="section-head">
        <div>
          <h2>Continental U.S. Arcade Hotspots</h2>
          <p class="note">Heat intensity blends arcade density and machine count. Tighter heat circles show local clusters without turning the whole map into soup.</p>
        </div>
      </div>
      <div class="panel">
        <div id="hotspot-map"></div>
      </div>
    </section>

    <section id="cities">
      <div class="section-head">
        <div>
          <h2>Top 25 Arcade Destination Cities</h2>
          <p class="note">Click headers or chips to sort by arcade count, total machines, or rare U.S. machines.</p>
        </div>
        <div class="sorter" data-target="city-table">
          <button data-sort="arcades" class="active">Arcades</button>
          <button data-sort="machines">Machines</button>
          <button data-sort="rare_us_machines">Rare U.S.</button>
        </div>
      </div>
      <div class="panel table-wrap"><table id="city-table"></table></div>
    </section>

    <section class="grid-2 matched-panel-grid">
      <div>
        <div class="section-head">
          <div>
            <h2>Top 25 States</h2>
            <p class="note">Lower 48 plus D.C., ranked from the same source snapshot.</p>
          </div>
          <div class="sorter" data-target="state-table">
            <button data-sort="arcades" class="active">Arcades</button>
            <button data-sort="machines">Machines</button>
            <button data-sort="rare_us_machines">Rare U.S.</button>
          </div>
        </div>
        <div class="panel table-wrap"><table id="state-table"></table></div>
      </div>
      <div>
        <div class="section-head">
          <div>
            <h2>Machines Per Arcade Distribution</h2>
            <p class="note">Most playable places are small; the long tail of large museums, pinball halls, and mega-arcades carries a lot of the inventory.</p>
          </div>
        </div>
        <div class="panel panel-pad chart-panel"><svg id="machine-distribution" viewBox="0 0 760 560" role="img" aria-label="Distribution of machines per arcade"></svg></div>
      </div>
    </section>

    <section>
      <div class="section-head">
        <div>
          <h2>Largest 25 Individual Arcades</h2>
          <p class="note">Sort by total known machines or rare U.S. games.</p>
        </div>
        <div class="sorter" data-target="arcade-table">
          <button data-sort="machines" class="active">Machines</button>
          <button data-sort="rare_us_machines">Rare U.S.</button>
        </div>
      </div>
      <div class="panel table-wrap"><table id="arcade-table"></table></div>
    </section>

    <p class="footer">Generated from canonical DuckDB data at {generated}. Rare U.S. counts use canonical game mappings and active continental U.S. locations.</p>
  </main>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script src="https://unpkg.com/leaflet.heat@0.2.0/dist/leaflet-heat.js"></script>
  <script>
    const DATA = {payload};
    const format = new Intl.NumberFormat();
    const byId = id => document.getElementById(id);
    const metricLabels = {{
      arcades: 'Arcades',
      machines: 'Machines',
      rare_us_machines: 'Rare U.S.',
      pinball_machines: 'Pinball',
      unique_games: 'Unique Games'
    }};
    byId('total-arcades').textContent = format.format(DATA.totals.arcades);
    byId('total-machines').textContent = format.format(DATA.totals.machines);
    byId('total-rare').textContent = format.format(DATA.totals.rare_us_machines);
    byId('total-cities').textContent = format.format(DATA.totals.cities);

    function topRows(rows, metric, limit = 25) {{
      return [...rows].sort((a, b) => (b[metric] || 0) - (a[metric] || 0) || String(a.label || a.name).localeCompare(String(b.label || b.name))).slice(0, limit);
    }}
    function bar(value, max) {{
      const pct = max ? Math.max(2, Math.round((value / max) * 100)) : 0;
      return `<div class="bar"><span style="width:${{pct}}%"></span><b>${{format.format(value || 0)}}</b></div>`;
    }}
    function renderRankTable(tableId, rows, metric, columns) {{
      const table = byId(tableId);
      const selected = topRows(rows, metric);
      const max = Math.max(...selected.map(row => row[metric] || 0), 1);
      table.innerHTML = `<thead><tr><th>Rank</th>${{columns.map(col => `<th data-sort="${{col.key}}">${{col.label}}</th>`).join('')}}</tr></thead>` +
        `<tbody>${{selected.map((row, index) => `<tr><td>${{index + 1}}</td>${{columns.map(col => {{
          const value = row[col.key] || 0;
          if (col.kind === 'name') return `<td class="name">${{escapeHtml(row[col.key] || '')}}${{row.sub ? `<div class="small">${{escapeHtml(row.sub)}}</div>` : ''}}</td>`;
          if (col.kind === 'bar') return `<td class="bar-cell">${{bar(value, max)}}</td>`;
          if (col.kind === 'rare') return `<td class="rare">${{format.format(value)}}</td>`;
          return `<td>${{format.format(value)}}</td>`;
        }}).join('')}}</tr>`).join('')}}</tbody>`;
      table.querySelectorAll('th[data-sort]').forEach(th => th.addEventListener('click', () => setSort(tableId, th.dataset.sort)));
    }}
    function escapeHtml(value) {{
      return String(value).replace(/[&<>"']/g, ch => ({{'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}}[ch]));
    }}
    const tableConfigs = {{
      'city-table': {{
        rows: DATA.cities,
        sort: 'arcades',
        columns: [
          {{key: 'label', label: 'City', kind: 'name'}},
          {{key: 'arcades', label: 'Arcades', kind: 'bar'}},
          {{key: 'machines', label: 'Machines'}},
          {{key: 'rare_us_machines', label: 'Rare U.S.', kind: 'rare'}}
        ]
      }},
      'state-table': {{
        rows: DATA.states,
        sort: 'arcades',
        columns: [
          {{key: 'label', label: 'State', kind: 'name'}},
          {{key: 'arcades', label: 'Arcades', kind: 'bar'}},
          {{key: 'machines', label: 'Machines'}},
          {{key: 'rare_us_machines', label: 'Rare U.S.', kind: 'rare'}}
        ]
      }},
      'arcade-table': {{
        rows: DATA.arcades.map(row => ({{...row, label: row.name, sub: [row.city, row.state].filter(Boolean).join(', ') + (row.street_address ? ' - ' + row.street_address : '')}})),
        sort: 'machines',
        columns: [
          {{key: 'label', label: 'Arcade', kind: 'name'}},
          {{key: 'machines', label: 'Machines', kind: 'bar'}},
          {{key: 'unique_games', label: 'Unique Games'}},
          {{key: 'rare_us_machines', label: 'Rare U.S.', kind: 'rare'}},
          {{key: 'pinball_machines', label: 'Pinball'}}
        ]
      }}
    }};
    function setSort(tableId, metric) {{
      const config = tableConfigs[tableId];
      config.sort = metric;
      document.querySelectorAll(`.sorter[data-target="${{tableId}}"] button`).forEach(button => button.classList.toggle('active', button.dataset.sort === metric));
      renderRankTable(tableId, config.rows, metric, config.columns);
    }}
    Object.entries(tableConfigs).forEach(([id, config]) => renderRankTable(id, config.rows, config.sort, config.columns));
    document.querySelectorAll('.sorter button').forEach(button => button.addEventListener('click', () => setSort(button.closest('.sorter').dataset.target, button.dataset.sort)));

    const map = L.map('hotspot-map', {{scrollWheelZoom: false}}).setView([39.5, -98.35], 4);
    L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{maxZoom: 18, attribution: '&copy; OpenStreetMap contributors'}}).addTo(map);
    const maxMachines = Math.max(...DATA.heat_points.map(point => point.machines), 1);
    const heat = DATA.heat_points.map(point => [point.lat, point.lon, 0.18 + 0.82 * Math.sqrt(point.machines / maxMachines)]);
    L.heatLayer(heat, {{radius: 13, blur: 8, maxZoom: 8, gradient: {{0.15: '#007c89', 0.5: '#f2a900', 1.0: '#e84d35'}}}}).addTo(map);

    function renderMachineDistribution() {{
      const svg = byId('machine-distribution');
      const rows = DATA.machine_distribution;
      const width = 760, height = 560;
      const pad = {{left: 64, right: 28, top: 34, bottom: 56}};
      const plotWidth = width - pad.left - pad.right;
      const plotHeight = height - pad.top - pad.bottom;
      const maxArcades = Math.max(...rows.map(row => row.arcades), 1);
      const maxMachines = Math.max(...rows.map(row => row.machines), 1);
      const barGap = 12;
      const barWidth = (plotWidth - barGap * (rows.length - 1)) / rows.length;
      const bars = rows.map((row, index) => {{
        const x = pad.left + index * (barWidth + barGap);
        const h = plotHeight * (row.arcades / maxArcades);
        const y = pad.top + plotHeight - h;
        const rareRatio = row.machines ? row.rare_us_machines / row.machines : 0;
        const fill = rareRatio > .18 ? '#e84d35' : rareRatio > .08 ? '#f2a900' : '#007c89';
        return `
          <g>
            <rect x="${{x}}" y="${{y}}" width="${{barWidth}}" height="${{h}}" rx="5" fill="${{fill}}" opacity=".86">
              <title>${{row.label}} machines: ${{format.format(row.arcades)}} arcades, ${{format.format(row.machines)}} machines, ${{format.format(row.rare_us_machines)}} rare U.S. hits</title>
            </rect>
            <text x="${{x + barWidth / 2}}" y="${{height - 31}}" text-anchor="middle" font-size="12" fill="#344454">${{row.label}}</text>
            <text x="${{x + barWidth / 2}}" y="${{y - 8}}" text-anchor="middle" font-size="12" font-weight="800" fill="#15202b">${{format.format(row.arcades)}}</text>
          </g>`;
      }}).join('');
      const machineLine = rows.map((row, index) => {{
        const x = pad.left + index * (barWidth + barGap) + barWidth / 2;
        const y = pad.top + plotHeight - plotHeight * (row.machines / maxMachines);
        return `${{index === 0 ? 'M' : 'L'}} ${{x}} ${{y}}`;
      }}).join(' ');
      const machineDots = rows.map((row, index) => {{
        const x = pad.left + index * (barWidth + barGap) + barWidth / 2;
        const y = pad.top + plotHeight - plotHeight * (row.machines / maxMachines);
        return `<circle cx="${{x}}" cy="${{y}}" r="4" fill="#15202b"><title>${{row.label}} machines: ${{format.format(row.machines)}}</title></circle>`;
      }}).join('');
      svg.innerHTML = `
        <line x1="${{pad.left}}" y1="${{pad.top + plotHeight}}" x2="${{width - pad.right}}" y2="${{pad.top + plotHeight}}" stroke="#ccd6df"/>
        <line x1="${{pad.left}}" y1="${{pad.top}}" x2="${{pad.left}}" y2="${{pad.top + plotHeight}}" stroke="#ccd6df"/>
        <text x="18" y="${{pad.top + plotHeight / 2}}" transform="rotate(-90 18 ${{pad.top + plotHeight / 2}})" text-anchor="middle" font-size="12" fill="#5b6672">Arcade count</text>
        <text x="${{width / 2}}" y="${{height - 10}}" text-anchor="middle" font-size="12" fill="#5b6672">Known machines at one arcade</text>
        ${{bars}}
        <path d="${{machineLine}}" fill="none" stroke="#15202b" stroke-width="2.5" stroke-linejoin="round"/>
        ${{machineDots}}
        <text x="${{width - 180}}" y="34" font-size="12" fill="#15202b" font-weight="800">black line = total machines in bin</text>`;
    }}
    renderMachineDistribution();
  </script>
</body>
</html>
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate static Arcade Road Trip dashboard HTML.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    with connect(args.db) as conn:
        data = build_dashboard_data(conn)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(build_html(data))
    print(f"wrote {args.output}")
    print(
        "snapshot: "
        f"{data['totals']['arcades']} arcades, "
        f"{data['totals']['machines']} machines, "
        f"{data['totals']['rare_us_machines']} rare U.S. hits"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
