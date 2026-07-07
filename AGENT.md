# Arcade Road Trip Agent Notes

This workspace contains the Arcade Road Trip static atlas plus a curated
DuckDB arcade-location database. The data began with Aurcade, then merged
Pinball Map and Zenius -I- vanisher sources for trip-planning and arcade
discovery. The legacy SQLite database remains as a migration source while the
older writer scripts are ported.

The product direction is one static artifact. The primary user-facing artifact
is `static/arcade_road_trip.html`, a one-file HTML app with embedded Parquet
data queried in-browser by DuckDB-WASM. The Flask client/server prototype
remains as a legacy/reference implementation only; do not add new product
features there unless they are specifically needed for comparison or local
debugging.

Public GitHub Pages URL:

`https://jeffgrover.github.io/arcade-road-trip/`

## Important Files

- `arcade_roadtrip.duckdb`: canonical working database for static generation.
- `migrate_sqlite_to_duckdb.py`: one-way migration from the legacy SQLite
  snapshot.
- `aurcade_locations.sqlite`: legacy SQLite source retained while import and
  curation writers are being ported to DuckDB.
- `aurcade_locations.baseline_2026-07-05_pinballmap.sqlite`: backup made before
  the Pinball Map import.
- `scrape_aurcade_locations.py`: Aurcade scraper and original schema creator.
- `import_pinballmap_locations.py`: Pinball Map CSV transformer/importer.
- `import_pinballmap_api.py`: public Pinball Map API importer for national,
  cached, rate-limited region pulls.
- `import_ziv_locations.py`: Zenius -I- vanisher Utah location/machine importer.
- `merge_ziv_machines.py`: second-pass ZIv machine inventory merger for
  already-linked Utah locations.
- `canonicalize_games.py`: conservative game-title canonicalization pass that
  writes source-specific duplicate mappings to a sidecar table.
- `curate_us_sources.py`: conservative national source-curation orchestrator.
- `sync_arcade_data.py`: operations wrapper that keeps source sync, curation,
  validation, DuckDB refresh, and static artifact generation as explicit phases.
- `arcade_query.py`: read-only query CLI intended for Codex/LLM use.
- `arcade_roadtrip_app.py`: legacy Flask route-planning prototype/reference and
  local static server.
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
- `validate_pinballmap_locations.py`: Pinball Map API validation for locations
  that have a known Pinball Map id.
- `validate_ziv_locations.py`: Zenius -I- vanisher validation for U.S.
  arcade/rhythm/motion-game location coverage.
- `location_2026-07-05_15h22m53.csv`: Pinball Map Utah export that was imported.
- `tests/`: unit tests for parser/import/query behavior.

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

Do not add columns casually. Current tooling intentionally uses the existing
tables and columns. DuckDB is canonical for the static build path; several
older import/validation writers still target the legacy SQLite file and should
be ported rather than extended in their current form.

Location verification/status data lives in sidecar tables so the imported source
schema stays intact:

- `location_verifications`: append-only-ish evidence from external probes such
  as Nominatim. These rows are review leads, not automatic closure decisions.
- `location_statuses`: current curated status used by `arcade_query.py`.
  Locations with `closed` or `replaced` status are excluded by default from
  canned query commands.
- `pinballmap_location_links`: local location id to Pinball Map location id
  links discovered from Pinball Map source URLs, manual overrides, and the
  imported CSV.
- `ziv_location_links`: local location id to Zenius -I- vanisher arcade id
  links discovered by fuzzy matching ZIv's U.S. arcade directory against local
  U.S. locations.
- `game_canonical_links`: source-specific game rows that should be interpreted
  as the same canonical game for counts, rarity, and matching. This preserves
  original `games` and `location_games` rows instead of rewriting imports.

Game canonicalization is intentionally conservative:

```bash
python3 canonicalize_games.py --report
python3 canonicalize_games.py --apply --report
python3 arcade_query.py game-aliases "Arabian"
```

Auto-links should be boring exact compact-title matches with no conflicting
manufacturer evidence. Risky edition, sequel, short-name, and fuzzy-spelling
pairs stay in the generated report for human review.

Known curated status:

- Aurcade location `1505` Atomic Arcade, The in Holladay is marked `replaced`
  by Cruzrs. The user confirmed Cruzrs took over the same space.
- Aurcade locations `797` Hollywood Connection in West Valley City, `1461`
  Planet Play & Buffet in Draper, and `4087` Funky Munky Arcade in Cedar City
  are marked `closed`. The user confirmed all three have been closed for
  several years.
- Pinball Map-only location `-1000022804` Nomad Cafe in Kanab is marked
  `closed`. Pinball Map validation returned `pinballmap_not_found` for
  Pinball Map id `22804`, and the user researched that the cafe closed in
  February 2026.


## Legacy Flask Reference

The client/server model is deprecated as the product runtime. Use Flask only as
a local static server or a reference implementation for behavior comparisons.

Run the legacy reference with:

```bash
.venv/bin/python generate_static_app.py
.venv/bin/python arcade_roadtrip_app.py
```

Then open `http://127.0.0.1:5000`. The site root serves the generated static
atlas when present. The legacy Flask route planner is at
`http://127.0.0.1:5000/planner`.
The app uses Leaflet/OpenStreetMap tiles, cached Nominatim geocoding for
explicit typed searches, and OSRM demo routing. Treat these public services as
local/light-use prototype dependencies only; keep geocoding cached and avoid
autocomplete or bulk requests.

## Static Atlas Pipeline

The primary user-facing app is a one-file static atlas:

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/python generate_static_app.py
```

Open `static/arcade_road_trip.html` directly from the filesystem, or serve it
from any static web server. It includes destination dashboards, route planning,
game search, and embedded Parquet data queried in-browser with DuckDB-WASM.
Flask remains useful as a legacy/reference implementation and as a convenient
local static server, but it is no longer required for the app runtime.

The generator reads `arcade_roadtrip.duckdb` and exports a browser-readable
snapshot during the build. These generated intermediates are ignored by git and
folded into the final HTML:

- `static/data/route_locations.parquet`: active continental U.S. locations with
  coordinates and scoring metrics.
- `static/data/location_games.parquet`: active continental U.S. machine rows
  with canonical game identity, U.S. rarity, and state uniqueness counts.

Earlier standalone outputs, including `static/dashboard.html`,
`static/duckdb_planner.html`, and `static/duckdb_planner_embedded.html`, were
transitional artifacts. Do not revive them as product surfaces; fold useful
behavior into `generate_static_app.py` instead.

## Sync Orchestration

Use `sync_arcade_data.py` as the low-maintenance operations entrypoint. It is
currently a wrapper around the proven source-specific scripts, then refreshes
`arcade_roadtrip.duckdb` and rebuilds the static atlas. Keep its phase
boundaries clean:

- `source-sync`: upstream polling/fetching and one-way source imports.
- `curation`: deterministic local cleanup such as game canonicalization.
- `validation`: source confidence checks and review queues.
- `database`: canonical DuckDB refresh/maintenance.
- `artifact-build`: Parquet intermediates and `static/arcade_road_trip.html`.

Useful commands:

```bash
python3 sync_arcade_data.py --plan-only
python3 sync_arcade_data.py --apply
python3 sync_arcade_data.py --source pinballmap --state CO
python3 sync_arcade_data.py --all-continental-us --skip-build
```

Do not hide validation work inside source-sync implementations. A default sync
may run both phases, but the concerns should remain independently testable and
replaceable.

## Querying the Data

Prefer `arcade_query.py` for interactive analysis. It opens the database in
read-only mode and returns Markdown by default.

Examples:

```bash
python3 arcade_query.py summary
python3 arcade_query.py city-summary --state UT --limit 10
python3 arcade_query.py locations "Quarters"
python3 arcade_query.py games "Godzilla"
python3 arcade_query.py game-aliases "Arabian"
python3 arcade_query.py where Godzilla --limit 10
python3 arcade_query.py inventory "Nickel Mania Murray"
python3 arcade_query.py nearby --lat 40.7608 --lon -111.891 --miles 10
python3 arcade_query.py compare-locations "Quarters" "Kiitos"
python3 arcade_query.py inactive --state UT
python3 arcade_query.py verification-report --state UT --limit 40
```

Raw SQL is available for one read-only statement at a time:

```bash
python3 arcade_query.py sql "SELECT city, COUNT(*) AS locations FROM locations WHERE state='UT' GROUP BY city"
```

Use `--format json` when downstream computation is easier:

```bash
python3 arcade_query.py games "Star Wars Comic Art" --format json --limit 5
```

Global flags such as `--format` and `--db` may appear before or after the
subcommand.

By default, canned query commands exclude locations marked inactive in
`location_statuses`. Use `--include-inactive` to inspect raw/historical rows:

```bash
python3 arcade_query.py summary --include-inactive
python3 arcade_query.py inventory "Atomic Arcade" --include-inactive
```

Raw SQL remains raw and does not automatically apply the active-location filter.

Lazy verification is opt-in for canned queries that return location ids. It
checks `location_verifications` first, probes only missing/stale locations, then
reruns the query with the cache refreshed:

```bash
python3 arcade_query.py locations "Kiitos" --verify-missing
python3 arcade_query.py where Godzilla --state UT --verify-stale-days 30
python3 arcade_query.py nearby --lat 40.7608 --lon -111.891 --miles 10 --verify-missing --verify-limit 5
```

Keep `--verify-limit` modest when using public Nominatim. Verification evidence
does not automatically mark a place closed/replaced unless curated into
`location_statuses`.

## Importing Pinball Map CSV Data

`import_pinballmap_locations.py` is for privileged/admin CSV exports, such as
the Utah export available to the user. It is NOT the national Pinball Map path.
The script defaults to dry-run mode. Always inspect the plan before applying.

```bash
python3 import_pinballmap_locations.py location_2026-07-05_15h22m53.csv --db aurcade_locations.sqlite --verbose
```

Apply only after making a backup and when no scraper/import process is writing:

```bash
sqlite3 aurcade_locations.sqlite ".backup 'aurcade_locations.backup.sqlite'"
python3 import_pinballmap_locations.py location_2026-07-05_15h22m53.csv --db aurcade_locations.sqlite --apply
```

The importer is idempotent. On the already-imported Utah CSV it should report:

- 71 CSV locations
- 329 CSV location-machine placements
- 5 locations matched to positive Aurcade ids
- 66 existing Pinball Map-only locations reused
- 161 machines matched to positive Aurcade game ids
- 15 existing Pinball Map-only games reused
- 0 placements skipped

Known manual location override:

- Pinball Map `10933` Nickel Mania, West Jordan -> Aurcade `695`. The source
  addresses differ, but the user confirmed these are the same location.

## Importing Pinball Map Public API Data

For national Pinball Map data, use measured public API calls through
`import_pinballmap_api.py`. It fetches `/api/v1/regions.json`,
`/api/v1/location_types.json`, and selected
`/api/v1/region/<region>/locations.json` payloads, caches responses under
`cache/pinballmap_api/`, and converts them into the same internal import bundle
as the CSV importer.

Examples:

```bash
python3 import_pinballmap_api.py --state CO
python3 import_pinballmap_api.py --states CO,NV,AZ
python3 import_pinballmap_api.py --all-continental-us
```

Use `--delay-seconds` to keep region fetches gentle. Dry-run is the default;
use `--apply` only after reviewing the plan and making a backup. The API path
uses an ambiguity guard: if a Pinball Map location nearly matches an existing
local location but not confidently enough, it is skipped for review instead of
being inserted as a likely duplicate.

Attribution and rate caution: Pinball Map asks API users to include attribution
and warns that thousands of requests in a short time may get blocked. Prefer
cached region pulls over repeated live probes.

## National Source Curation

Use `curate_us_sources.py` to coordinate the conservative U.S. enrichment pass.
It is dry-run by default and writes review artifacts under `reports/`:

```bash
python3 curate_us_sources.py --state CO
python3 curate_us_sources.py --states CO,NV,AZ
python3 curate_us_sources.py --all-continental-us
```

Apply mode creates a SQLite `.backup` first unless `--skip-backup` is passed:

```bash
python3 curate_us_sources.py --all-continental-us --apply
```

The orchestrator:

- links only high-confidence ZIv matches (`confidence >= 0.84`);
- leaves ZIv possible matches in `reports/ziv_possible_matches_<date>.md`;
- imports clear unmatched ZIv rows as ZIv-only locations;
- merges ZIv machines into linked/imported locations;
- imports Pinball Map API locations/machines with the ambiguity guard;
- writes `pinballmap_possible_matches`, `ziv_unmatched_source_locations`, and
  `national_data_quality` reports.

Google Places/business-status validation remains a later layer; source absence
from Pinball Map or ZIv is not closure evidence.

## Scraping Aurcade

The scraper writes to the same SQLite database. Avoid running import scripts
while the scraper is active.

Useful scraper examples:

```bash
python3 scrape_aurcade_locations.py --db aurcade_locations.sqlite --include-games
python3 scrape_aurcade_locations.py --db aurcade_locations.sqlite --index-only
```

Check active/completed scrape runs with:

```bash
sqlite3 aurcade_locations.sqlite "SELECT * FROM scrape_runs ORDER BY id DESC LIMIT 3;"
```

## Verification

Run these after code or database changes:

```bash
python3 -m unittest discover -s tests
python3 -m py_compile arcade_query.py import_pinballmap_locations.py scrape_aurcade_locations.py
sqlite3 aurcade_locations.sqlite "PRAGMA integrity_check; PRAGMA foreign_key_check;"
python3 arcade_query.py summary
```

Expected current summary after the Utah Pinball Map import, ZIv Utah import,
and closed-location status curation:

- `locations`: 2210
- `active_locations`: 2205
- `locations_ut`: 90
- `active_locations_ut`: 85
- `pinballmap_only_locations`: 66
- `ziv_only_locations`: 10
- `games`: 3409
- `pinballmap_only_games`: 15
- `ziv_only_games`: 121
- `location_games`: 38935
- `active_location_games`: 38798
- `pinball_rows`: 6967
- `active_pinball_rows`: 6962

To run external OSM/Nominatim verification, use a rate-limited batch. Public
Nominatim service should be treated gently; keep the default delay unless you
are using a different compliant endpoint.

```bash
python3 verify_locations_osm.py --state UT --limit 25 --min-game-count 3 --apply
python3 verify_locations_osm.py --location-id 1505 --include-inactive --apply
```

Recent Nominatim check status counts, including Atomic plus the top Utah batch:

- `matched`: 13
- `possible_replaced`: 12
- `not_found`: 1

Treat `possible_replaced` as a review queue. Many results are malls, suites,
street labels, or nearby POIs where OSM found the address but not the exact
arcade name.

For Utah pinball locations, Pinball Map community/admin data is high-trust. The
user is an administrator for Utah Pinball Map records; recent Pinball Map API
updates, `ic_active`, and user submission history should generally outweigh
Nominatim reverse-geocode mismatches.

Zenius -I- vanisher (ZIv) is useful for non-pinball arcade, rhythm, Japanese,
motion, and amusement-game coverage that Pinball Map misses. It is not a
general business-status authority, and a ZIv miss is not closure evidence.

Use the ZIv validator like this:

```bash
python3 validate_ziv_locations.py --limit 40
sqlite3 aurcade_locations.sqlite ".backup 'aurcade_locations.backup.sqlite'"
python3 validate_ziv_locations.py --apply --limit 40
```

The script writes only sidecar data:

- `ziv_location_links`
- `location_verifications` rows with `provider = 'ziv'`

It does not alter Aurcade-native `locations`, `games`, or `location_games`, and
it does not auto-curate `location_statuses`.

Use the ZIv importer for Utah-only source additions after a dry run:

```bash
python3 import_ziv_locations.py
sqlite3 aurcade_locations.sqlite ".backup 'aurcade_locations.backup.sqlite'"
python3 import_ziv_locations.py --apply
```

The ZIv importer also supports national state selection:

```bash
python3 import_ziv_locations.py --state CO
python3 import_ziv_locations.py --states CO,NV,AZ
python3 import_ziv_locations.py --all-continental-us
```

Current ZIv import baseline:

- 2 manual duplicate/alias links:
  - ZIv `1783` Sandy Nicklecade -> local `1569`.
  - ZIv `6007` Arcade Galactic -> local `120`.
- 10 ZIv-only Utah locations imported.
- 74 ZIv machine placements imported.
- 54 ZIv-only games imported.

Use the ZIv machine merger after location links/imports to bring ZIv inventory
into existing matched locations:

```bash
python3 merge_ziv_machines.py
sqlite3 aurcade_locations.sqlite ".backup 'aurcade_locations.backup.sqlite'"
python3 merge_ziv_machines.py --apply
```

The merger supports the same state selectors:

```bash
python3 merge_ziv_machines.py --state CO
python3 merge_ziv_machines.py --all-continental-us --include-ziv-only
```

The merger creates/updates:

- `ziv_machine_links`: audit table for each ZIv machine reviewed.
- `games`: exact ZIv titles using the ZIv negative id namespace when no
  near-exact existing game title is found.
- `location_games`: missing placements for existing linked locations.

Current ZIv machine merge baseline:

- 231 ZIv machine rows reviewed for existing linked Utah locations.
- 158 machine placements inserted.
- 73 machines linked as already present in local inventory.
- 67 additional ZIv-only game rows created by the second pass.

Current ZIv U.S. validation baseline:

- 2,929 ZIv U.S. arcades fetched.
- 1,968 active local U.S. locations considered.
- 523 local/source locations linked to ZIv after Utah imports and overrides.
- 474 high/probable matches.
- 37 possible matches.
- 25 Utah ZIv rows accounted for: 13 matched existing local rows, 2 linked by
  manual duplicate/alias override, and 10 imported as ZIv-only locations.

Pinball Map validation is intentionally source-scoped: it is strong evidence for
locations with pinball machines, but it will miss arcades or amusement venues
that do not have pins. Absence from Pinball Map is not closure evidence by
itself.

Run Pinball Map validation with:

```bash
python3 validate_pinballmap_locations.py --state UT --limit 100 --apply
```

Recent Pinball Map validation results:

- Known Pinball Map links: 71
- `fresh_pinballmap`: 70
- `pinballmap_not_found`: 1

## Cautions

- Use read-only access for analysis unless the user explicitly asks to mutate
  the database.
- Make a SQLite `.backup` before imports or schema changes.
- Do not use raw file copy as the only backup while WAL files may exist.
- Be careful with fuzzy matches. City is a meaningful location-match gate; an
  address plus ZIP match may override city-name drift.
- Pinball Map CSV contains superfluous and sensitive export fields. The importer
  intentionally ignores user emails, IPs, tokens, and similar fields.
