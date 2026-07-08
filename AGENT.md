# Arcade Road Trip Agent Notes

This workspace contains the Arcade Road Trip static atlas plus its canonical
DuckDB arcade-location database. The data began with Aurcade, then merged
Pinball Map and Zenius -I- vanisher sources for trip-planning and arcade
discovery.

The product direction is one static artifact. The primary user-facing artifact
is `static/arcade_road_trip.html`, a one-file HTML app with embedded Parquet
data queried in-browser by DuckDB-WASM. There is no client/server runtime in
the product path.

Public GitHub Pages URL:

`https://jeffgrover.github.io/arcade-road-trip/`

## Important Files

- `arcade_roadtrip.duckdb`: canonical working database for static generation.
- `sync_arcade_data.py`: operations wrapper for source sync, validation,
  curation, and static artifact generation.
- `arcade_db.py`: shared DuckDB connection/query helpers.
- `arcade_query.py`: read-only query CLI intended for Codex/LLM use.
- `scrape_aurcade_locations.py`: DuckDB-native Aurcade scraper and original
  schema creator.
- `import_pinballmap_api.py`: public Pinball Map API importer for national,
  cached, rate-limited region pulls.
- `import_pinballmap_locations.py`: optional local Pinball Map CSV transformer
  for privileged/admin exports. CSV snapshots are local inputs, not tracked
  source artifacts.
- `import_ziv_locations.py`: Zenius -I- vanisher location/machine importer.
- `merge_ziv_machines.py`: second-pass ZIv machine inventory merger for
  already-linked locations.
- `canonicalize_games.py`: conservative game-title canonicalization pass that
  writes source-specific duplicate mappings to a sidecar table.
- `curate_us_sources.py`: conservative national source-curation orchestrator.
- `maintain_duckdb.py`: checkpoint helper used before static export; can also
  compact the database with `--compact` after deletes/removals.
- `generate_dashboard.py`: destination-dashboard data builder used by the
  one-file atlas. Its standalone HTML output is historical/development-only.
- `export_static_data.py`: shared Parquet snapshot builder used by the one-file
  atlas. The `static/data/` files are generated intermediates, not product
  artifacts.
- `generate_static_app.py`: generates the primary one-file static atlas at
  `static/arcade_road_trip.html`, combining dashboard, planner, game search,
  and embedded Parquet data.
- `verify_locations_osm.py`: OpenStreetMap/Nominatim verification probe that
  records evidence in sidecar tables.
- `scan_google_maps_closures.py`: explicit slow Google Maps URL closure probe
  for review-led status curation.
- `validate_pinballmap_locations.py`: Pinball Map API validation for locations
  that have a known Pinball Map id.
- `validate_ziv_locations.py`: Zenius -I- vanisher validation for U.S.
  arcade/rhythm/motion-game location coverage.
- `tests/`: fast unit tests for parser/import/query/sync behavior.

## Database Conventions

The original schema is Aurcade-native:

- `locations.location_id` is an Aurcade location id for positive ids.
- `games.game_id` is an Aurcade game id for positive ids.
- `location_games` joins locations to games.

Imported source-only rows use deterministic negative ids so they do not collide
with Aurcade ids:

- Pinball Map location id `N` becomes `-(1000000000 + N)`.
- Pinball Map machine id `N` becomes `-(1000000000 + N)`.
- ZIv location id `N` becomes `-(2000000000 + N)`.
- ZIv game id `N` becomes `-(2000000000 + N)`.

DuckDB is canonical. Do not add migration/bootstrap paths for obsolete local
database formats.

Location verification/status data lives in sidecar tables so the imported source
schema stays intact:

- `location_verifications`: evidence from external probes such as Nominatim,
  Pinball Map, and ZIv. These rows are review leads, not automatic closure
  decisions.
- `location_statuses`: current curated status used by `arcade_query.py`.
  Locations with `closed` or `replaced` status are excluded by default from
  canned query commands.
- Active/inactive status vocabulary is centralized in `arcade_db.py`; UI data
  builders and CLI queries should import it rather than redefining it.
- `pinballmap_location_links`: local location id to Pinball Map location id
  links discovered from source URLs, manual overrides, and optional local CSV
  imports.
- `ziv_location_links`: local location id to Zenius -I- vanisher arcade id
  links discovered by fuzzy matching ZIv's U.S. arcade directory against local
  U.S. locations.
- `game_canonical_links`: source-specific game rows that should be interpreted
  as the same canonical game for counts, rarity, and matching. This preserves
  original `games` and `location_games` rows instead of rewriting imports.

## Operations

Preview the full sync/build plan:

```bash
.venv/bin/python sync_arcade_data.py --plan-only
```

Run the default pipeline. Source and validation writers are dry-run unless
`--apply` is passed; curation and static artifact generation still run from the
current local DuckDB data:

```bash
.venv/bin/python sync_arcade_data.py
```

Apply upstream source/validation writes and rebuild the atlas:

```bash
.venv/bin/python sync_arcade_data.py --apply
```

Useful scoped runs:

```bash
.venv/bin/python sync_arcade_data.py --source pinballmap --state CO
.venv/bin/python sync_arcade_data.py --source ziv --states CO,NV,AZ
.venv/bin/python sync_arcade_data.py --all-continental-us --skip-build
.venv/bin/python sync_arcade_data.py --compact-db
.venv/bin/python sync_arcade_data.py --skip-db-maintenance
.venv/bin/python sync_arcade_data.py --include-osm-validation --osm-limit 10
```

## Source Notes

Pinball Map public API import is the national path. It fetches and caches
region payloads under `cache/pinballmap_api/`, then converts them into the same
internal import bundle as the CSV importer.

Pinball Map CSV import remains available only for local privileged/admin
exports:

```bash
.venv/bin/python import_pinballmap_locations.py path/to/location_export.csv --db arcade_roadtrip.duckdb --verbose
```

Zenius -I- vanisher is useful for non-pinball arcade, rhythm, Japanese, motion,
and amusement-game coverage that Pinball Map misses. It is not a general
business-status authority, and a ZIv miss is not closure evidence.

The Aurcade browser scrape is DuckDB-native, but remains explicit because it is
slow and network-heavy:

```bash
.venv/bin/python sync_arcade_data.py --source aurcade --aurcade-limit 25 --plan-only
.venv/bin/python sync_arcade_data.py --include-aurcade-scrape --aurcade-index-only --plan-only
```

Google Maps closure scanning is also explicit and not part of the normal sync
pipeline. It opens one official Maps search URL per location, reads multiple
rendered page signals for explicit closure labels, and only writes
evidence/status rows with `--apply`. It also captures Google place ids,
website URLs, rendered addresses, and coordinates. Missing location metadata is
filled in apply mode; existing values are only overwritten with
`--overwrite-existing-details`. Default automatic scans wait a random 45-150
seconds between requests:

```bash
.venv/bin/python -m playwright install chromium
.venv/bin/python scan_google_maps_closures.py --sample
.venv/bin/python scan_google_maps_closures.py --loop --max-runtime-minutes 240 --apply
```

## Verification

Run these after code or database changes:

```bash
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m py_compile arcade_db.py arcade_query.py canonicalize_games.py import_pinballmap_locations.py import_pinballmap_api.py import_ziv_locations.py merge_ziv_machines.py validate_pinballmap_locations.py validate_ziv_locations.py verify_locations_osm.py scan_google_maps_closures.py scrape_aurcade_locations.py curate_us_sources.py us_states.py sync_arcade_data.py maintain_duckdb.py generate_static_app.py export_static_data.py generate_dashboard.py
.venv/bin/python arcade_query.py summary
```

## Cautions

- Use read-only access for analysis unless the user explicitly asks to mutate
  the database.
- Use DuckDB backups before imports or schema changes when applying writes.
- Keep public-source probes rate-limited and cached.
- Be careful with fuzzy matches. City is a meaningful location-match gate; an
  address plus ZIP match may override city-name drift.
- Pinball Map CSV contains superfluous and sensitive export fields. The importer
  intentionally ignores user emails, IPs, tokens, and similar fields.
