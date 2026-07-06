# Arcade Road Trip

Local arcade-road-trip planner and curated arcade database tooling.

This repository grew out of an Aurcade scrape, then merged in Utah Pinball Map and Zenius -I- vanisher data. The current SQLite database keeps the original Aurcade-compatible schema while using sidecar tables for provenance, status curation, and external validation links.

## Quick Checks

```bash
python3 arcade_query.py summary
python3 -m unittest discover -s tests
python3 -m py_compile arcade_query.py import_pinballmap_locations.py import_ziv_locations.py merge_ziv_machines.py validate_pinballmap_locations.py validate_ziv_locations.py verify_locations_osm.py scrape_aurcade_locations.py arcade_roadtrip_app.py
sqlite3 aurcade_locations.sqlite "PRAGMA integrity_check; PRAGMA foreign_key_check;"
```

## Local Route Prototype

```bash
python3 arcade_roadtrip_app.py
```

Then open <http://127.0.0.1:5000>.

The prototype uses Leaflet/OpenStreetMap tiles, cached Nominatim geocoding for explicit typed searches, and OSRM demo routing for local/light use only.
