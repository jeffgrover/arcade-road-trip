# Arcade Road Trip

Arcade Road Trip is a portable static atlas for finding arcade destinations,
rare machines, and playable stops along a road trip.

The current product direction is **one static artifact**: the user-facing app is
`static/arcade_road_trip.html`, a single generated HTML file with embedded
Parquet data queried in the browser by DuckDB-WASM. The old Flask client/server
prototype is retained only as a legacy reference and convenient local static
server; it is no longer the product runtime.

Published app:

<https://jeffgrover.github.io/arcade-road-trip/>

## Primary Static App

Build the one-file atlas from the curated SQLite database:

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/python generate_static_app.py
```

Open `static/arcade_road_trip.html` directly from the filesystem, or serve the
repo from any static web server. The generated file includes:

- destination dashboard
- hotspot map
- top cities, states, and arcades
- route planner
- game search/explore view
- embedded Parquet arcade and machine data

The arcade dataset is baked into the HTML. Network access is still used for map
tiles, typed geocoding, OSRM route geometry, and the DuckDB-WASM CDN.

## Data Tooling

The SQLite database remains the curation source of truth. The generation
pipeline flows from SQLite, through narrow data builders, into one deployable
HTML artifact:

- `aurcade_locations.sqlite`: curated working database.
- `arcade_query.py`: read-only CLI for analysis.
- `curate_us_sources.py`: national source-enrichment orchestrator.
- `export_static_data.py`: shared Parquet snapshot builders used by the atlas.
- `generate_dashboard.py`: shared destination-summary builder used by the atlas.
- `generate_static_app.py`: primary generator for `static/arcade_road_trip.html`.
- `arcade_roadtrip_app.py`: legacy Flask reference/local static server.

This repository grew out of an Aurcade scrape, then merged in Pinball Map and
Zenius -I- vanisher data. The database keeps the original Aurcade-compatible
schema while using sidecar tables for provenance, status curation, validation
links, and canonical game mappings.

## Legacy Reference

Flask is useful for comparison and local development, but the client/server app
is deprecated as the product runtime.

```bash
.venv/bin/python generate_static_app.py
.venv/bin/python arcade_roadtrip_app.py
```

Then open <http://127.0.0.1:5000>. The root serves the generated static atlas
when present. The old Flask route planner remains available at
<http://127.0.0.1:5000/planner> for reference.

Earlier transitional outputs, including the standalone dashboard, standalone
DuckDB planner, and separate `static/data/` Parquet bundle, have been folded
into the one-file atlas. They may be regenerated locally while developing, but
they are no longer checked-in product artifacts.

## National Data Curation

Use the conservative orchestrator for source enrichment. It is dry-run by
default and writes review reports under `reports/`:

```bash
.venv/bin/python curate_us_sources.py --state CO
.venv/bin/python curate_us_sources.py --states CO,NV,AZ
.venv/bin/python curate_us_sources.py --all-continental-us
```

Apply mode makes a SQLite backup unless `--skip-backup` is passed:

```bash
.venv/bin/python curate_us_sources.py --all-continental-us --apply
```

Pinball Map national ingestion uses the public API with cached, rate-limited
region calls. The CSV importer is still available for privileged/admin exports,
but it is not the national path.

## Quick Checks

```bash
.venv/bin/python arcade_query.py summary
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m py_compile arcade_query.py import_pinballmap_locations.py import_pinballmap_api.py import_ziv_locations.py merge_ziv_machines.py validate_pinballmap_locations.py validate_ziv_locations.py verify_locations_osm.py scrape_aurcade_locations.py arcade_roadtrip_app.py curate_us_sources.py us_states.py generate_static_app.py export_static_data.py generate_dashboard.py
sqlite3 aurcade_locations.sqlite "PRAGMA integrity_check; PRAGMA foreign_key_check;"
```
