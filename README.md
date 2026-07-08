# Arcade Road Trip

Arcade Road Trip is a portable static atlas for finding arcade destinations,
rare machines, and playable stops along a road trip.

The current product direction is **one static artifact**: the user-facing app is
`static/arcade_road_trip.html`, a single generated HTML file with embedded
Parquet data queried in the browser by DuckDB-WASM. There is no client/server
runtime in the product path.

Published app:

<https://jeffgrover.github.io/arcade-road-trip/>

## Primary Static App

Build the one-file atlas from the canonical DuckDB database:

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

## Data Sync

The operations entrypoint is `sync_arcade_data.py`. It keeps the major pipeline
concerns separate while wrapping the current source-specific scripts:

- source sync: poll/fetch upstream source changes;
- curation: canonicalize obvious game aliases;
- validation: check source links and review queues;
- database: sync and validate source data directly in canonical DuckDB;
- curation: apply deterministic DuckDB-native cleanup such as game aliases;
- database maintenance: checkpoint DuckDB before export, with optional
  compaction after deletes/removals;
- artifact build: regenerate Parquet intermediates and the one-file atlas.

Preview the plan without running anything:

```bash
.venv/bin/python sync_arcade_data.py --plan-only
```

Run the full default pipeline. Source and validation writers are dry-run unless
`--apply` is passed; the DuckDB refresh and static build still run from the
current local data:

```bash
.venv/bin/python sync_arcade_data.py
```

Apply upstream source/validation writes and rebuild the atlas:

```bash
.venv/bin/python sync_arcade_data.py --apply
```

Useful narrowing flags:

```bash
.venv/bin/python sync_arcade_data.py --source pinballmap --state CO
.venv/bin/python sync_arcade_data.py --source ziv --states CO,NV,AZ
.venv/bin/python sync_arcade_data.py --all-continental-us --skip-build
.venv/bin/python sync_arcade_data.py --compact-db
.venv/bin/python sync_arcade_data.py --skip-db-maintenance
.venv/bin/python sync_arcade_data.py --include-osm-validation --osm-limit 10
```

## Data Tooling

DuckDB is now the canonical database for the static build. The generation
pipeline flows from `arcade_roadtrip.duckdb`, through narrow data builders, into
one deployable HTML artifact:

- `arcade_roadtrip.duckdb`: canonical working database for static generation.
- `sync_arcade_data.py`: operations wrapper for source sync, validation,
  curation, and static artifact generation.
- `arcade_db.py`: shared DuckDB connection/query helpers for pipeline scripts.
- `arcade_query.py`: read-only DuckDB CLI for analysis.
- `curate_us_sources.py`: national source-enrichment orchestrator.
- `maintain_duckdb.py`: checkpoint helper used before static export; can also
  compact the database with `--compact` after deletes/removals.
- `export_static_data.py`: shared Parquet snapshot builders used by the atlas.
- `generate_dashboard.py`: shared destination-summary builder used by the atlas.
- `generate_static_app.py`: primary generator for `static/arcade_road_trip.html`.
- `scan_google_maps_closures.py`: explicit slow Google Maps URL closure probe
  for review-led status curation.

This repository grew out of an Aurcade scrape, then merged in Pinball Map and
Zenius -I- vanisher data. The database keeps the original Aurcade-compatible
schema while using sidecar tables for provenance, status curation, validation
links, and canonical game mappings.

Active/inactive location status vocabulary is centralized in `arcade_db.py`.
The CLI and static atlas data builders use that shared definition so closed or
replaced locations do not drift into UI counts by accident.

## Static Serving

The generated atlas can be opened directly from the filesystem, published by
GitHub Pages, or served by any static web server:

```bash
.venv/bin/python generate_static_app.py
python3 -m http.server 8000
```

Then open <http://127.0.0.1:8000/static/arcade_road_trip.html>.

Earlier transitional outputs, including the standalone dashboard, standalone
DuckDB planner, and separate `static/data/` Parquet bundle, have been folded
into the one-file atlas. They may be regenerated locally while developing, but
they are no longer checked-in product artifacts.

## National Data Curation

Use the conservative orchestrator for source enrichment. It is dry-run by
default and writes review reports under `reports/`:

```bash
.venv/bin/python sync_arcade_data.py --plan-only
.venv/bin/python sync_arcade_data.py --states CO,NV,AZ --plan-only
.venv/bin/python sync_arcade_data.py --all-continental-us --plan-only
```

The source curation and validation pipeline writes directly to DuckDB. Apply
mode makes a timestamped DuckDB backup unless `--skip-backup` is passed;
`arcade_query.py` keeps lazy source verification disabled for DuckDB query
sessions, so run validation through `sync_arcade_data.py` instead.

```bash
.venv/bin/python sync_arcade_data.py --all-continental-us --apply
```

Pinball Map national ingestion is DuckDB-native and uses the public API with
cached, rate-limited region calls. The CSV importer still accepts local
privileged/admin exports, but those snapshots are not checked in and are not
the national path.

The Aurcade browser scrape is also DuckDB-native, but remains explicit because
it is slow and network-heavy:

```bash
.venv/bin/python sync_arcade_data.py --source aurcade --aurcade-limit 25 --plan-only
.venv/bin/python sync_arcade_data.py --include-aurcade-scrape --aurcade-index-only --plan-only
```

Google Maps closure scanning is separate from the normal sync pipeline. It
opens one official Maps search URL per location, reads multiple rendered page
signals for explicit closure labels, and records evidence only when `--apply`
is passed. It also captures Google place ids, website URLs, rendered addresses,
and coordinates for review. Missing `locations.website_url`,
`locations.google_place_id`, `locations.google_cid`, address, and coordinate
fields are filled in apply mode; existing values are only overwritten with
`--overwrite-existing-details`. The default automatic scan is deliberately
slow, with a random 45-150 second delay between requests:

```bash
.venv/bin/python -m playwright install chromium
.venv/bin/python scan_google_maps_closures.py --sample
.venv/bin/python scan_google_maps_closures.py --state FL --limit 25 --apply
.venv/bin/python scan_google_maps_closures.py --loop --max-runtime-minutes 240 --apply
```

## Quick Checks

```bash
.venv/bin/python arcade_query.py summary
.venv/bin/python sync_arcade_data.py --plan-only
.venv/bin/python generate_static_app.py
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m py_compile arcade_db.py arcade_query.py canonicalize_games.py import_pinballmap_locations.py import_pinballmap_api.py import_ziv_locations.py merge_ziv_machines.py validate_pinballmap_locations.py validate_ziv_locations.py verify_locations_osm.py scan_google_maps_closures.py scrape_aurcade_locations.py curate_us_sources.py us_states.py sync_arcade_data.py maintain_duckdb.py generate_static_app.py export_static_data.py generate_dashboard.py
.venv/bin/python arcade_query.py sql "SELECT COUNT(*) AS locations FROM locations"
```
