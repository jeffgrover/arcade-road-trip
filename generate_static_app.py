#!/usr/bin/env python3
"""Generate the one-file Arcade Road Trip static app."""

from __future__ import annotations

import argparse
import base64
import html
import json
from pathlib import Path

from export_static_data import DEFAULT_OUTPUT_DIR, connect as export_connect, load_location_games, load_route_locations, write_parquet
from generate_dashboard import build_dashboard_data, connect as dashboard_connect


DEFAULT_DB = Path("aurcade_locations.sqlite")
DEFAULT_OUTPUT = Path("static/arcade_road_trip.html")


def js_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).replace("</", "<\\/")


def ensure_parquet(db_path: Path, output_dir: Path) -> dict[str, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    with export_connect(db_path) as conn:
        route_locations = load_route_locations(conn)
        location_games = load_location_games(conn)
    write_parquet(route_locations, output_dir / "route_locations.parquet")
    write_parquet(location_games, output_dir / "location_games.parquet")
    return {"route_locations": len(route_locations), "location_games": len(location_games)}


def embedded_parquet(data_dir: Path, counts: dict[str, int]) -> dict[str, object]:
    return {
        "route_locations": base64.b64encode((data_dir / "route_locations.parquet").read_bytes()).decode("ascii"),
        "location_games": base64.b64encode((data_dir / "location_games.parquet").read_bytes()).decode("ascii"),
        "manifest": {"counts": counts},
    }


def build_html(dashboard: dict[str, object], parquet: dict[str, object]) -> str:
    dashboard_payload = js_json(dashboard)
    parquet_payload = js_json(parquet)
    generated = html.escape(str(dashboard["generated_at"]))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Arcade Road Trip Atlas</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <style>
    :root {{ --ink:#15202b; --muted:#5b6672; --line:#d9e1e8; --paper:#fff; --wash:#f4f7f9; --teal:#007c89; --gold:#f2a900; --coral:#e84d35; --rare:#d000ff; --blue:#0d6efd; }}
    * {{ box-sizing:border-box; }}
    html,body {{ min-height:100%; }}
    body {{ margin:0; font-family:system-ui,-apple-system,Segoe UI,sans-serif; color:var(--ink); background:var(--wash); }}
    button,input {{ font:inherit; }} button {{ cursor:pointer; }}
    header {{ position:sticky; top:0; z-index:20; color:white; background:linear-gradient(120deg,#092f3d,#006d77 58%,#f2a900); box-shadow:0 2px 16px rgba(0,0,0,.18); }}
    .topbar {{ display:flex; align-items:center; justify-content:space-between; gap:12px; padding:12px min(5vw,48px); }}
    .brand {{ font-weight:900; font-size:18px; letter-spacing:.01em; }}
    .tabs {{ display:flex; flex-wrap:wrap; gap:8px; }}
    .tab {{ border:1px solid rgba(255,255,255,.25); background:rgba(255,255,255,.14); color:white; border-radius:999px; padding:8px 12px; font-weight:800; }}
    .tab.active {{ background:white; color:#09313c; }}
    .hero {{ padding:18px min(5vw,48px) 24px; display:grid; grid-template-columns:minmax(280px,1fr) minmax(320px,.8fr); gap:22px; align-items:end; }}
    h1 {{ margin:0 0 10px; font-size:clamp(34px,5vw,62px); line-height:.96; }}
    .lede {{ margin:0; color:rgba(255,255,255,.87); font-size:17px; line-height:1.45; max-width:760px; }}
    .stat-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }}
    .stat {{ border:1px solid rgba(255,255,255,.22); background:rgba(255,255,255,.16); border-radius:8px; padding:12px; }}
    .stat b {{ display:block; font-size:28px; line-height:1; }} .stat span {{ display:block; margin-top:6px; font-size:12px; color:rgba(255,255,255,.8); }}
    main {{ padding:22px min(5vw,48px) 42px; }}
    .view {{ display:none; }} .view.active {{ display:block; }}
    .section-head {{ display:flex; justify-content:space-between; align-items:end; gap:14px; margin:0 0 10px; }}
    h2 {{ font-size:24px; margin:0; }} .note,.small {{ color:var(--muted); }} .note {{ margin:4px 0 0; font-size:13px; }}
    .panel {{ background:var(--paper); border:1px solid var(--line); border-radius:8px; overflow:hidden; box-shadow:0 8px 26px rgba(20,38,52,.07); }}
    .panel-pad {{ padding:16px; }} section {{ margin-top:22px; }}
    #hotspot-map {{ height:min(68vh,650px); min-height:430px; }}
    .grid-2 {{ display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:18px; align-items:stretch; }}
    .table-wrap {{ max-height:560px; overflow:auto; }} table {{ width:100%; border-collapse:collapse; font-size:13px; }}
    th,td {{ padding:9px 10px; border-bottom:1px solid #e8edf2; text-align:right; vertical-align:top; }} th:first-child,td:first-child {{ text-align:left; }}
    th {{ position:sticky; top:0; z-index:1; background:#f9fbfc; color:#344454; font-size:12px; cursor:pointer; }} td.name {{ font-weight:750; color:#203040; }}
    .sorter {{ display:inline-flex; gap:6px; flex-wrap:wrap; }} .sorter button {{ border:1px solid var(--line); background:white; border-radius:999px; padding:6px 10px; font-size:12px; }} .sorter button.active {{ background:#0b5963; color:white; border-color:#0b5963; }}
    .bar {{ display:grid; grid-template-columns:1fr auto; gap:8px; align-items:center; }} .bar span:first-child {{ height:8px; border-radius:999px; background:linear-gradient(90deg,var(--teal),var(--gold)); min-width:2px; }} .bar-cell {{ min-width:130px; }} .rare {{ color:var(--rare); font-weight:800; }}
    #machine-distribution {{ width:100%; height:560px; display:block; }}
    .planner-shell {{ display:grid; grid-template-columns:minmax(430px,.9fr) minmax(480px,1.1fr); height:calc(100vh - 76px); min-height:620px; margin:-22px calc(-1 * min(5vw,48px)) -42px; }}
    .planner-side {{ min-height:0; display:flex; flex-direction:column; background:white; border-right:1px solid var(--line); }}
    .controls {{ padding:14px 16px 10px; border-bottom:1px solid #e6ebf0; }} label {{ display:block; font-size:12px; font-weight:700; margin-bottom:4px; }} input {{ width:100%; padding:8px 9px; }}
    .trip-grid,.action-row {{ display:grid; grid-template-columns:1fr 1fr; gap:8px; align-items:end; }} .action-row {{ grid-template-columns:minmax(140px,1fr) 150px; margin-top:8px; }} .action-row button {{ border:0; border-radius:6px; padding:9px; background:var(--blue); color:white; font-weight:800; }} .action-row button:disabled {{ opacity:.7; cursor:wait; }}
    .detour-field label {{ display:flex; justify-content:space-between; }} .message {{ margin-top:9px; font-size:13px; }} .error {{ color:#b00020; }}
    .results-head {{ display:flex; justify-content:space-between; gap:10px; align-items:center; padding:12px 16px 8px; border-bottom:1px solid #edf1f5; }} .results-head h2 {{ font-size:16px; }}
    .legend {{ color:var(--muted); font-size:12px; display:flex; flex-wrap:wrap; gap:8px; }} .rare-us {{ color:var(--rare); font-weight:800; }} .unique-state {{ font-weight:800; }} .common-game {{ color:var(--muted); }}
    #stops {{ flex:1; min-height:0; overflow:auto; padding:0 16px 18px; }} .stop {{ border-top:1px solid #e6ebf0; padding:12px 0; cursor:pointer; border-radius:6px; }} .stop.active {{ background:#e8f1ff; outline:2px solid var(--blue); outline-offset:-2px; padding-left:8px; padding-right:8px; }} .stop h3 {{ margin:0 0 4px; font-size:17px; }}
    .meta {{ color:#51606d; font-size:13px; }} .games {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(165px,1fr)); gap:2px 14px; margin-top:8px; font-size:13px; }} .game {{ display:block; line-height:1.25; margin-bottom:4px; }} .machine-count {{ color:#51606d; font-size:11px; font-weight:650; margin-left:3px; }}
    #route-map {{ height:100%; min-height:0; }} .loading {{ display:flex; align-items:center; gap:10px; color:#425466; padding:18px 0; font-weight:650; }} .car-track {{ width:70px; height:24px; position:relative; overflow:hidden; border-bottom:2px dashed #ccd6df; }} .car {{ position:absolute; left:-34px; bottom:4px; width:26px; height:10px; background:var(--blue); border-radius:7px 9px 4px 4px; animation:drive 1.35s linear infinite; }} .car:before {{ content:""; position:absolute; left:7px; top:-6px; width:12px; height:7px; background:#7fb3ff; border-radius:7px 7px 0 0; }} .car:after {{ content:""; position:absolute; left:4px; bottom:-4px; width:4px; height:4px; background:#17202a; border-radius:50%; box-shadow:14px 0 0 #17202a; }} @keyframes drive {{ to {{ transform:translateX(106px); }} }}
    .explore-grid {{ display:grid; grid-template-columns:minmax(280px,420px) minmax(0,1fr); gap:18px; align-items:start; }} .searchbox {{ display:flex; gap:8px; }} .searchbox input {{ flex:1; }} .searchbox button {{ border:0; border-radius:6px; padding:8px 12px; background:var(--blue); color:white; font-weight:800; }} #explore-results {{ display:grid; gap:10px; }} .result-card {{ padding:13px; }}
    @media (max-width:900px) {{
      header {{ position:static; }} .topbar {{ align-items:flex-start; flex-direction:column; padding:10px 14px; }} .hero {{ display:block; padding:14px; }} .stat-grid {{ margin-top:14px; }} main {{ padding:14px; }} .grid-2,.explore-grid {{ grid-template-columns:1fr; }} #hotspot-map {{ min-height:360px; }}
      .planner-shell {{ grid-template-columns:1fr; grid-template-rows:auto minmax(300px,42vh) minmax(260px,1fr); height:auto; min-height:0; margin:-14px -14px -42px; }} .planner-side {{ grid-row:1 / span 3; display:contents; }} .controls {{ background:white; }} #route-map {{ grid-row:2; min-height:320px; }} .results-wrap {{ grid-row:3; background:white; min-height:0; display:flex; flex-direction:column; }} #stops {{ max-height:45vh; }} .trip-grid,.action-row {{ grid-template-columns:1fr; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="topbar"><div class="brand">Arcade Road Trip Atlas</div><nav class="tabs"><button class="tab active" data-view="destinations">Destinations</button><button class="tab" data-view="planner">Plan Route</button><button class="tab" data-view="explore">Explore Games</button><button class="tab" data-view="about">About Data</button></nav></div>
    <div class="hero" id="hero"><div><h1>Find arcade trips worth taking.</h1><p class="lede">A portable static atlas of U.S. arcade destinations, rare machines, and road-trip stops. The data is baked into this file and queried in your browser with DuckDB-WASM.</p></div><div class="stat-grid"><div class="stat"><b id="total-arcades">0</b><span>active continental U.S. arcades</span></div><div class="stat"><b id="total-machines">0</b><span>known machine placements</span></div><div class="stat"><b id="total-rare">0</b><span>rare U.S. game/location hits</span></div><div class="stat"><b id="total-cities">0</b><span>cities with playable locations</span></div></div></div>
  </header>
  <main>
    <div class="view active" id="view-destinations">
      <section><div class="section-head"><div><h2>Continental U.S. Arcade Hotspots</h2><p class="note">Heat intensity blends arcade density and machine count.</p></div></div><div class="panel"><div id="hotspot-map"></div></div></section>
      <section><div class="section-head"><div><h2>Top 25 Arcade Destination Cities</h2><p class="note">Sort by arcade count, total machines, or rare U.S. machines.</p></div><div class="sorter" data-target="city-table"><button data-sort="arcades" class="active">Arcades</button><button data-sort="machines">Machines</button><button data-sort="rare_us_machines">Rare U.S.</button></div></div><div class="panel table-wrap"><table id="city-table"></table></div></section>
      <section class="grid-2"><div><div class="section-head"><div><h2>Top 25 States</h2><p class="note">Lower 48 plus D.C.</p></div><div class="sorter" data-target="state-table"><button data-sort="arcades" class="active">Arcades</button><button data-sort="machines">Machines</button><button data-sort="rare_us_machines">Rare U.S.</button></div></div><div class="panel table-wrap"><table id="state-table"></table></div></div><div><div class="section-head"><div><h2>Machines Per Arcade Distribution</h2><p class="note">The long tail of large museums, halls, and mega-arcades carries a lot of inventory.</p></div></div><div class="panel panel-pad"><svg id="machine-distribution" viewBox="0 0 760 560"></svg></div></div></section>
      <section><div class="section-head"><div><h2>Largest 25 Individual Arcades</h2><p class="note">Sort by total known machines or rare U.S. games.</p></div><div class="sorter" data-target="arcade-table"><button data-sort="machines" class="active">Machines</button><button data-sort="rare_us_machines">Rare U.S.</button></div></div><div class="panel table-wrap"><table id="arcade-table"></table></div></section>
    </div>
    <div class="view" id="view-planner"><div class="planner-shell"><aside class="planner-side"><div class="controls"><div class="trip-grid"><div><label>Origin</label><input id="origin" value="South Jordan, UT" /></div><div><label>Destination</label><input id="destination" value="Ogden, UT" /></div></div><div class="action-row"><div class="detour-field"><label><span>Max detour</span><span><span id="detourLabel">15</span> mi</span></label><input id="detour" type="range" min="2" max="60" value="15" /></div><button id="plan" disabled>Loading data...</button></div><div id="status" class="message"></div><div id="message" class="message error"></div></div><div class="results-wrap"><div class="results-head"><h2>Arcades:</h2><div class="legend"><span class="rare-us">Under 10 in U.S.</span><span class="unique-state">Only one in state</span><span class="common-game">more common</span></div></div><div id="stops"></div></div></aside><div id="route-map"></div></div></div>
    <div class="view" id="view-explore"><section class="explore-grid"><div class="panel panel-pad"><h2>Find A Game</h2><p class="note">Search canonical and source game names, then see where they are playable.</p><div class="searchbox" style="margin-top:12px"><input id="game-search" value="Godzilla" /><button id="search-games" disabled>Search</button></div><div id="explore-message" class="message"></div></div><div id="explore-results"></div></section></div>
    <div class="view" id="view-about"><section class="panel panel-pad"><h2>About This Snapshot</h2><p>Generated from local SQLite data at {generated}. The app embeds dashboard summaries plus Parquet route/game data and queries it locally with DuckDB-WASM.</p><p id="about-counts" class="note"></p><p class="note">Network is still used for map tiles, typed geocoding, OSRM route geometry, and the DuckDB-WASM library CDN. The arcade dataset itself is inside this HTML file.</p></section></div>
  </main>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script src="https://unpkg.com/leaflet.heat@0.2.0/dist/leaflet-heat.js"></script>
  <script type="module">
import * as duckdb from 'https://cdn.jsdelivr.net/npm/@duckdb/duckdb-wasm/+esm';
const DATA={dashboard_payload};
const EMBEDDED_PARQUET={parquet_payload};
const format=new Intl.NumberFormat(); const $=id=>document.getElementById(id);
let conn, hotspotMap, routeMap, routeLayer, markers=[], markerByLocationId=new Map();
function esc(v){{return String(v??'').replace(/[&<>"']/g,ch=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));}}
function rowObjects(r){{return r.toArray().map(row=>row.toJSON?row.toJSON():Object.fromEntries(row));}}
function base64ToBytes(v){{const b=atob(v), bytes=new Uint8Array(b.length); for(let i=0;i<b.length;i++)bytes[i]=b.charCodeAt(i); return bytes;}}
function topRows(rows,metric,limit=25){{return [...rows].sort((a,b)=>(b[metric]||0)-(a[metric]||0)||String(a.label||a.name).localeCompare(String(b.label||b.name))).slice(0,limit);}}
function bar(value,max){{const pct=max?Math.max(2,Math.round(value/max*100)):0; return `<div class="bar"><span style="width:${{pct}}%"></span><b>${{format.format(value||0)}}</b></div>`;}}
function renderRankTable(id,rows,metric,cols){{const selected=topRows(rows,metric), max=Math.max(...selected.map(r=>r[metric]||0),1); $(id).innerHTML=`<thead><tr><th>Rank</th>${{cols.map(c=>`<th data-sort="${{c.key}}">${{c.label}}</th>`).join('')}}</tr></thead><tbody>${{selected.map((r,i)=>`<tr><td>${{i+1}}</td>${{cols.map(c=>{{const v=r[c.key]||0;if(c.kind==='name')return `<td class="name">${{esc(r[c.key]||'')}}${{r.sub?`<div class="small">${{esc(r.sub)}}</div>`:''}}</td>`;if(c.kind==='bar')return `<td class="bar-cell">${{bar(v,max)}}</td>`;if(c.kind==='rare')return `<td class="rare">${{format.format(v)}}</td>`;return `<td>${{format.format(v)}}</td>`;}}).join('')}}</tr>`).join('')}}</tbody>`;}}
const tableConfigs={{'city-table':{{rows:DATA.cities,sort:'arcades',columns:[{{key:'label',label:'City',kind:'name'}},{{key:'arcades',label:'Arcades',kind:'bar'}},{{key:'machines',label:'Machines'}},{{key:'rare_us_machines',label:'Rare U.S.',kind:'rare'}}]}},'state-table':{{rows:DATA.states,sort:'arcades',columns:[{{key:'label',label:'State',kind:'name'}},{{key:'arcades',label:'Arcades',kind:'bar'}},{{key:'machines',label:'Machines'}},{{key:'rare_us_machines',label:'Rare U.S.',kind:'rare'}}]}},'arcade-table':{{rows:DATA.arcades.map(r=>({{...r,label:r.name,sub:[r.city,r.state].filter(Boolean).join(', ')+(r.street_address?' - '+r.street_address:'')}})),sort:'machines',columns:[{{key:'label',label:'Arcade',kind:'name'}},{{key:'machines',label:'Machines',kind:'bar'}},{{key:'unique_games',label:'Unique Games'}},{{key:'rare_us_machines',label:'Rare U.S.',kind:'rare'}},{{key:'pinball_machines',label:'Pinball'}}]}}}};
function setSort(id,metric){{const c=tableConfigs[id]; c.sort=metric; document.querySelectorAll(`.sorter[data-target="${{id}}"] button`).forEach(b=>b.classList.toggle('active',b.dataset.sort===metric)); renderRankTable(id,c.rows,metric,c.columns);}}
function initDashboard(){{$('total-arcades').textContent=format.format(DATA.totals.arcades);$('total-machines').textContent=format.format(DATA.totals.machines);$('total-rare').textContent=format.format(DATA.totals.rare_us_machines);$('total-cities').textContent=format.format(DATA.totals.cities); Object.entries(tableConfigs).forEach(([id,c])=>renderRankTable(id,c.rows,c.sort,c.columns)); document.querySelectorAll('.sorter button').forEach(b=>b.addEventListener('click',()=>setSort(b.closest('.sorter').dataset.target,b.dataset.sort))); hotspotMap=L.map('hotspot-map',{{scrollWheelZoom:false}}).setView([39.5,-98.35],4); L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{maxZoom:18,attribution:'&copy; OpenStreetMap contributors'}}).addTo(hotspotMap); const max=Math.max(...DATA.heat_points.map(p=>p.machines),1); L.heatLayer(DATA.heat_points.map(p=>[p.lat,p.lon,.18+.82*Math.sqrt(p.machines/max)]),{{radius:13,blur:8,maxZoom:8,gradient:{{.15:'#007c89',.5:'#f2a900',1:'#e84d35'}}}}).addTo(hotspotMap); renderMachineDistribution();}}
function renderMachineDistribution(){{const svg=$('machine-distribution'), rows=DATA.machine_distribution, width=760,height=560,pad={{left:64,right:28,top:34,bottom:56}},pw=width-pad.left-pad.right,ph=height-pad.top-pad.bottom,maxA=Math.max(...rows.map(r=>r.arcades),1),maxM=Math.max(...rows.map(r=>r.machines),1),gap=12,bw=(pw-gap*(rows.length-1))/rows.length; const bars=rows.map((r,i)=>{{const x=pad.left+i*(bw+gap),h=ph*r.arcades/maxA,y=pad.top+ph-h,rareRatio=r.machines?r.rare_us_machines/r.machines:0,fill=rareRatio>.18?'#e84d35':rareRatio>.08?'#f2a900':'#007c89'; return `<g><rect x="${{x}}" y="${{y}}" width="${{bw}}" height="${{h}}" rx="5" fill="${{fill}}" opacity=".86"></rect><text x="${{x+bw/2}}" y="${{height-31}}" text-anchor="middle" font-size="12" fill="#344454">${{r.label}}</text><text x="${{x+bw/2}}" y="${{y-8}}" text-anchor="middle" font-size="12" font-weight="800">${{format.format(r.arcades)}}</text></g>`;}}).join(''); const line=rows.map((r,i)=>{{const x=pad.left+i*(bw+gap)+bw/2,y=pad.top+ph-ph*r.machines/maxM;return `${{i?'L':'M'}} ${{x}} ${{y}}`;}}).join(' '); const dots=rows.map((r,i)=>{{const x=pad.left+i*(bw+gap)+bw/2,y=pad.top+ph-ph*r.machines/maxM;return `<circle cx="${{x}}" cy="${{y}}" r="4" fill="#15202b"></circle>`;}}).join(''); svg.innerHTML=`<line x1="${{pad.left}}" y1="${{pad.top+ph}}" x2="${{width-pad.right}}" y2="${{pad.top+ph}}" stroke="#ccd6df"/><line x1="${{pad.left}}" y1="${{pad.top}}" x2="${{pad.left}}" y2="${{pad.top+ph}}" stroke="#ccd6df"/><text x="18" y="${{pad.top+ph/2}}" transform="rotate(-90 18 ${{pad.top+ph/2}})" text-anchor="middle" font-size="12" fill="#5b6672">Arcade count</text><text x="${{width/2}}" y="${{height-10}}" text-anchor="middle" font-size="12" fill="#5b6672">Known machines at one arcade</text>${{bars}}<path d="${{line}}" fill="none" stroke="#15202b" stroke-width="2.5" stroke-linejoin="round"/>${{dots}}<text x="${{width-180}}" y="34" font-size="12" font-weight="800">black line = total machines in bin</text>`;}}
async function bootDuckDB(){{$('status').textContent='Loading embedded arcade data...'; const bundle=await duckdb.selectBundle(duckdb.getJsDelivrBundles()); const workerUrl=URL.createObjectURL(new Blob([`importScripts("${{bundle.mainWorker}}");`],{{type:'text/javascript'}})); const worker=new Worker(workerUrl); const db=new duckdb.AsyncDuckDB(new duckdb.ConsoleLogger(),worker); await db.instantiate(bundle.mainModule,bundle.pthreadWorker); URL.revokeObjectURL(workerUrl); await db.registerFileBuffer('route_locations.parquet',base64ToBytes(EMBEDDED_PARQUET.route_locations)); await db.registerFileBuffer('location_games.parquet',base64ToBytes(EMBEDDED_PARQUET.location_games)); conn=await db.connect(); await conn.query("CREATE VIEW route_locations AS SELECT * FROM read_parquet('route_locations.parquet')"); await conn.query("CREATE VIEW location_games AS SELECT * FROM read_parquet('location_games.parquet')"); $('status').textContent=`Loaded ${{EMBEDDED_PARQUET.manifest.counts.route_locations.toLocaleString()}} arcades and ${{EMBEDDED_PARQUET.manifest.counts.location_games.toLocaleString()}} machine placements.`; $('about-counts').textContent=$('status').textContent; $('plan').disabled=false; $('plan').textContent='Plan trip'; $('search-games').disabled=false;}}
function initRouteMap(){{if(routeMap)return; routeMap=L.map('route-map',{{maxBounds:L.latLngBounds([24.396308,-124.848974],[49.384358,-66.885444]),maxBoundsViscosity:1,minZoom:4}}).setView([39.5,-98.35],4); L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{maxZoom:19,attribution:'&copy; OpenStreetMap contributors'}}).addTo(routeMap);}}
function parsePoint(t){{const p=t.split(',').map(v=>Number(v.trim())); return p.length===2&&p.every(Number.isFinite)?{{lat:p[0],lon:p[1]}}:null;}} async function geocode(t,label){{t=t.trim(); if(!t)throw new Error(`Enter a ${{label}}.`); const p=parsePoint(t); if(p)return p; const r=await fetch(`https://nominatim.openstreetmap.org/search?format=jsonv2&limit=1&countrycodes=us&q=${{encodeURIComponent(t)}}`), d=await r.json(); if(!r.ok||!d?.length)throw new Error(`Could not geocode: ${{t}}`); return {{lat:Number(d[0].lat),lon:Number(d[0].lon)}};}}
async function routeBetween(o,d){{const r=await fetch(`https://router.project-osrm.org/route/v1/driving/${{o.lon}},${{o.lat}};${{d.lon}},${{d.lat}}?overview=full&geometries=geojson&steps=false`), data=await r.json(); if(!r.ok||!data.routes?.length)throw new Error('No route found.'); return data.routes[0];}}
function hav(a,b){{const R=3958.7613,p1=a.lat*Math.PI/180,p2=b.lat*Math.PI/180,dp=(b.lat-a.lat)*Math.PI/180,dl=(b.lon-a.lon)*Math.PI/180,h=Math.sin(dp/2)**2+Math.cos(p1)*Math.cos(p2)*Math.sin(dl/2)**2;return R*2*Math.atan2(Math.sqrt(h),Math.sqrt(1-h));}} function proj(p,pts){{let best=Infinity,stride=Math.max(1,Math.floor(pts.length/650)); for(let i=0;i<pts.length;i+=stride)best=Math.min(best,hav(p,pts[i])); return best;}} function bounds(pts,m){{const la=pts.map(p=>p.lat),lo=pts.map(p=>p.lon),minLat=Math.min(...la),maxLat=Math.max(...la),minLon=Math.min(...lo),maxLon=Math.max(...lo),latPad=m/69,mid=(minLat+maxLat)/2,lonPad=m/Math.max(20,69*Math.cos(mid*Math.PI/180)); return {{minLat:minLat-latPad,maxLat:maxLat+latPad,minLon:minLon-lonPad,maxLon:maxLon+lonPad}};}}
async function candidates(b){{return rowObjects(await conn.query(`SELECT * FROM route_locations WHERE latitude BETWEEN ${{Number(b.minLat)}} AND ${{Number(b.maxLat)}} AND longitude BETWEEN ${{Number(b.minLon)}} AND ${{Number(b.maxLon)}} ORDER BY game_count DESC LIMIT 2000`));}} async function gamesFor(ids){{if(!ids.length)return new Map(); const rows=rowObjects(await conn.query(`SELECT * FROM location_games WHERE location_id IN (${{ids.map(Number).filter(Number.isFinite).join(',')}}) ORDER BY rare_us DESC, unique_state DESC, cabinet_type, name`)), g=new Map(); for(const r of rows){{if(!g.has(r.location_id))g.set(r.location_id,[]); g.get(r.location_id).push(r);}} return g;}}
function renderGames(gs){{return gs.map(g=>{{let name=esc(g.name), content=name; if(Number(g.rare_us))content=`<strong class="rare-us">${{name}}<span class="machine-count">(${{g.us_location_count}} US)</span></strong>`; else if(Number(g.unique_state))content=`<strong class="unique-state">${{name}}</strong>`; return `<span class="game">${{content}}</span>`;}}).join('');}}
function selectStop(id,opt={{}}){{document.querySelectorAll('.stop.active').forEach(e=>e.classList.remove('active')); const card=document.querySelector(`.stop[data-location-id="${{id}}"]`); if(card){{card.classList.add('active'); if(opt.scroll!==false)$('stops').scrollTo({{top:card.offsetTop-$('stops').offsetTop,behavior:'smooth'}});}} const marker=markerByLocationId.get(Number(id)); if(marker){{marker.openPopup(); if(opt.pan!==false)routeMap.panTo(marker.getLatLng(),{{animate:true}});}}}}
async function planTrip(){{initRouteMap(); $('message').textContent=''; $('stops').innerHTML='<div class="loading"><span>Planning your trip...</span><span class="car-track"><span class="car"></span></span></div>'; $('plan').disabled=true; $('plan').textContent='Planning...'; try{{const o=await geocode($('origin').value,'starting point'), d=await geocode($('destination').value,'destination'), route=await routeBetween(o,d), pts=route.geometry.coordinates.map(([lon,lat])=>({{lat,lon}})), max=Number($('detour').value), cand=await candidates(bounds(pts,max)); const stops=cand.map(l=>{{const rd=proj({{lat:Number(l.latitude),lon:Number(l.longitude)}},pts), det=rd*2, bonus=l.source_tags?10:0; return {{...l,estimated_detour_miles:det,score:Number(l.game_count||0)*1.5+Number(l.pinball_games||0)*.6+Number(l.rhythm_games||0)*2+bonus-det*2.5}};}}).filter(l=>l.estimated_detour_miles<=max).sort((a,b)=>b.score-a.score).slice(0,25), games=await gamesFor(stops.map(s=>s.location_id)); if(routeLayer)routeMap.removeLayer(routeLayer); markers.forEach(m=>routeMap.removeLayer(m)); markers=[]; markerByLocationId=new Map(); routeLayer=L.geoJSON(route.geometry,{{style:{{color:'#0d6efd',weight:5}}}}).addTo(routeMap); routeMap.fitBounds(routeLayer.getBounds(),{{padding:[24,24],maxZoom:12}}); stops.forEach(s=>{{const m=L.marker([s.latitude,s.longitude]).addTo(routeMap).bindPopup(`<b>${{esc(s.name)}}</b><br>${{esc(s.city)}}, ${{esc(s.state)}}<br>${{s.estimated_detour_miles.toFixed(2)}} mi detour`); m.on('click',()=>selectStop(s.location_id,{{pan:false}})); markers.push(m); markerByLocationId.set(Number(s.location_id),m);}}); $('stops').innerHTML=stops.map(s=>`<section class="stop" data-location-id="${{s.location_id}}" tabindex="0"><h3>${{esc(s.name)}}</h3><div class="meta">${{esc(s.city)}}, ${{esc(s.state)}} · ${{format.format(s.game_count||0)}} games · ${{s.estimated_detour_miles.toFixed(2)}} mi estimated detour · ${{esc(s.source_tags||'local')}}</div><div class="games">${{renderGames(games.get(s.location_id)||[])}}</div></section>`).join('')||'<p>No stops inside this detour budget.</p>'; document.querySelectorAll('.stop').forEach(c=>c.addEventListener('click',()=>selectStop(c.dataset.locationId)));}}catch(e){{$('message').textContent=e.message;$('stops').innerHTML='';}}finally{{$('plan').disabled=false;$('plan').textContent='Plan trip';}}}}
async function searchGames(){{const q=$('game-search').value.trim().replaceAll("'","''"); if(!q)return; $('explore-message').textContent='Searching...'; const rows=rowObjects(await conn.query(`SELECT lg.canonical_name, lg.us_location_count, lg.state_location_count, lg.rare_us, rl.name AS location_name, rl.city, rl.state, rl.street_address, rl.game_count FROM location_games lg JOIN route_locations rl USING(location_id) WHERE lower(lg.name) LIKE lower('%${{q}}%') OR lower(lg.canonical_name) LIKE lower('%${{q}}%') ORDER BY lg.us_location_count ASC, rl.game_count DESC LIMIT 80`)); $('explore-message').textContent=`${{rows.length}} matching location rows`; $('explore-results').innerHTML=rows.map(r=>`<div class="panel result-card"><b>${{esc(r.canonical_name)}}</b> <span class="small">${{r.us_location_count}} U.S. locations</span><div>${{esc(r.location_name)}} · ${{esc(r.city)}}, ${{esc(r.state)}}</div><div class="small">${{esc(r.street_address)}} · ${{format.format(r.game_count||0)}} known machines</div></div>`).join('')||'<p>No matches.</p>';}}
document.querySelectorAll('.tab').forEach(t=>t.addEventListener('click',()=>{{document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));document.querySelectorAll('.view').forEach(v=>v.classList.remove('active'));t.classList.add('active');$('view-'+t.dataset.view).classList.add('active');$('hero').style.display=t.dataset.view==='destinations'?'grid':'none'; if(t.dataset.view==='planner')setTimeout(()=>{{initRouteMap();routeMap.invalidateSize();}},50); if(t.dataset.view==='destinations'&&hotspotMap)setTimeout(()=>hotspotMap.invalidateSize(),50);}}));
$('detour').addEventListener('input',()=>$('detourLabel').textContent=$('detour').value); $('plan').addEventListener('click',planTrip); $('search-games').addEventListener('click',searchGames); $('game-search').addEventListener('keydown',e=>{{if(e.key==='Enter')searchGames();}});
initDashboard(); bootDuckDB().catch(e=>{{$('message').textContent='Could not load embedded DuckDB data: '+e.message; $('explore-message').textContent=e.message;}});
  </script>
</body>
</html>"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate one-file static Arcade Road Trip app.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    counts = ensure_parquet(args.db, args.data_dir)
    with dashboard_connect(args.db) as conn:
        dashboard = build_dashboard_data(conn)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(build_html(dashboard, embedded_parquet(args.data_dir, counts)))
    print(f"wrote {args.output}")
    print(f"embedded {counts['route_locations']} locations and {counts['location_games']} game placements")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
