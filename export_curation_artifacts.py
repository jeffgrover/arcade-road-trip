#!/usr/bin/env python3
"""Export mergeable curation/provenance records from a DuckDB snapshot.

The resulting JSONL files are intended to be committed and merged. The
DuckDB file remains a generated artifact.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from arcade_db import DEFAULT_DUCKDB, rows


TABLES = (
    "game_source_records",
    "duplicate_review_candidates",
    "duplicate_review_records",
    "duplicate_review_decisions",
    "location_statuses",
    "location_verifications",
    "pinballmap_location_links",
    "ziv_location_links",
    "web_roster_reconciliation_actions",
)


def json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def has_table(conn: duckdb.DuckDBPyConnection, table: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema='main' AND table_name=?",
        (table,),
    ).fetchone())


def export_table(conn: duckdb.DuckDBPyConnection, table: str, output_dir: Path) -> int:
    path = output_dir / f"{table}.jsonl"
    if not has_table(conn, table):
        path.unlink(missing_ok=True)
        return 0
    records = rows(conn, f'SELECT * FROM "{table}" ORDER BY ALL')
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps({k: json_value(v) for k, v in record.items()}, ensure_ascii=False, sort_keys=True) + "\n")
    return len(records)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DUCKDB)
    parser.add_argument("--output-dir", type=Path, default=Path("curation"))
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(args.db), read_only=True) as conn:
        counts = {table: export_table(conn, table, args.output_dir) for table in TABLES}
    manifest = {
        "format": "arcade-road-trip-curation-v1",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "database": str(args.db),
        "files": {table: f"{table}.jsonl" for table, count in counts.items() if count},
        "counts": counts,
    }
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for table, count in counts.items():
        if count:
            print(f"wrote {table}: {count}")
    print(f"wrote {args.output_dir / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
