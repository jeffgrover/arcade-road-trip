#!/usr/bin/env python3
"""Conservatively repair metadata and statuses written by the legacy Maps scanner."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import duckdb

from arcade_db import DEFAULT_DUCKDB, connect, has_table


RESTORED_FIELDS = ("website_url", "street_address", "latitude", "longitude")


def sql_literal(value: Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def repair_scan_damage(
    conn: duckdb.DuckDBPyConnection,
    baseline_db: Path,
    apply: bool = False,
    repaired_at: str | None = None,
    preserve_location_ids: Iterable[int] = (),
) -> dict[str, int]:
    repaired_at = repaired_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    preserve_ids = tuple(int(location_id) for location_id in preserve_location_ids)
    preserve_placeholders = ",".join("?" for _ in preserve_ids)
    explicit_preserve = (
        f"OR statuses.location_id IN ({preserve_placeholders})" if preserve_ids else ""
    )
    protected_current = ""
    protected_statuses = ""
    if has_table(conn, "location_verifications"):
        protected_current = """
            AND NOT EXISTS (
                SELECT 1 FROM location_verifications verification
                WHERE verification.location_id = current.location_id
                  AND verification.provider = 'google_maps_url'
                  AND verification.match_kind IN ('search_url_v2', 'place_id_url_v2')
            )
        """
        protected_statuses = """
            AND NOT EXISTS (
                SELECT 1 FROM location_verifications verification
                WHERE verification.location_id = statuses.location_id
                  AND verification.provider = 'google_maps_url'
                  AND verification.match_kind IN ('search_url_v2', 'place_id_url_v2')
            )
        """
    conn.execute(f"ATTACH {sql_literal(baseline_db)} AS scan_baseline (READ_ONLY)")
    try:
        baseline_website = (
            "CASE WHEN lower(COALESCE(baseline.website_url, '')) LIKE '%maps.m.en%' "
            "THEN NULL ELSE baseline.website_url END"
        )
        field_difference = " OR ".join(
            (
                f"current.{field} IS DISTINCT FROM {baseline_website}"
                if field == "website_url"
                else f"current.{field} IS DISTINCT FROM baseline.{field}"
            )
            for field in RESTORED_FIELDS
        )
        restored_locations = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM locations current
            JOIN scan_baseline.locations baseline USING (location_id)
            WHERE {field_difference}
              {protected_current}
            """
        ).fetchone()[0]
        cleared_place_ids = conn.execute(
            f"""
            SELECT COUNT(*) FROM locations current
            WHERE (current.google_place_id IS NOT NULL OR current.google_cid IS NOT NULL)
            {protected_current}
            """
        ).fetchone()[0]
        cleared_bogus_websites = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM locations current
            WHERE lower(COALESCE(current.website_url, '')) LIKE '%maps.m.en%'
            """
        ).fetchone()[0]
        preserved_closures = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM location_statuses statuses
            JOIN locations USING (location_id)
            WHERE statuses.status = 'closed'
              AND statuses.evidence = 'google_maps_url'
              AND (lower(locations.name) LIKE '%(closed)%' {explicit_preserve})
              {protected_statuses}
            """,
            preserve_ids,
        ).fetchone()[0]
        demoted_closures = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM location_statuses statuses
            JOIN locations USING (location_id)
            WHERE statuses.status = 'closed'
              AND statuses.evidence = 'google_maps_url'
              AND NOT (lower(locations.name) LIKE '%(closed)%' {explicit_preserve})
              {protected_statuses}
            """,
            preserve_ids,
        ).fetchone()[0]
        results = {
            "restored_locations": int(restored_locations),
            "cleared_place_ids": int(cleared_place_ids),
            "cleared_bogus_websites": int(cleared_bogus_websites),
            "preserved_closures": int(preserved_closures),
            "demoted_closures": int(demoted_closures),
        }
        if not apply:
            return results

        conn.execute("BEGIN TRANSACTION")
        try:
            conn.execute(
                f"""
                UPDATE locations AS current SET
                    website_url = {baseline_website},
                    street_address = baseline.street_address,
                    latitude = baseline.latitude,
                    longitude = baseline.longitude
                FROM scan_baseline.locations AS baseline
                WHERE current.location_id = baseline.location_id
                  AND (
                    current.website_url IS DISTINCT FROM {baseline_website}
                    OR current.street_address IS DISTINCT FROM baseline.street_address
                    OR current.latitude IS DISTINCT FROM baseline.latitude
                    OR current.longitude IS DISTINCT FROM baseline.longitude
                  )
                  {protected_current}
                """
            )
            conn.execute(
                f"""
                UPDATE locations AS current SET google_place_id = NULL, google_cid = NULL
                WHERE (current.google_place_id IS NOT NULL OR current.google_cid IS NOT NULL)
                {protected_current}
                """
            )
            conn.execute(
                f"""
                UPDATE locations AS current SET website_url = NULL
                WHERE lower(COALESCE(current.website_url, '')) LIKE '%maps.m.en%'
                """
            )
            conn.execute(
                f"""
                UPDATE location_statuses AS statuses SET
                    evidence = 'manual_name_audit',
                    notes = 'Source/display name explicitly records this location as closed.'
                FROM locations
                WHERE statuses.location_id = locations.location_id
                  AND statuses.status = 'closed'
                  AND statuses.evidence = 'google_maps_url'
                  AND lower(locations.name) LIKE '%(closed)%'
                  {protected_statuses}
                """
            )
            if preserve_ids:
                conn.execute(
                    f"""
                    UPDATE location_statuses SET
                        evidence = 'manual_user_confirmation',
                        notes = 'User-confirmed closure preserved during legacy Google scan repair.'
                    WHERE status = 'closed'
                      AND evidence = 'google_maps_url'
                      AND location_id IN ({preserve_placeholders})
                      {protected_statuses.replace('statuses.location_id', 'location_statuses.location_id')}
                    """,
                    preserve_ids,
                )
            conn.execute(
                f"""
                UPDATE location_statuses AS statuses SET
                    status = 'needs_review',
                    confidence = 0.5,
                    verified_at = ?,
                    notes = 'Pending exact-place revalidation; legacy whole-results-page closure evidence was demoted.'
                WHERE statuses.status = 'closed' AND statuses.evidence = 'google_maps_url'
                {protected_statuses}
                """,
                (repaired_at,),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return results
    finally:
        conn.execute("DETACH scan_baseline")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Restore pre-scan location metadata and demote untrusted Google-only closures."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DUCKDB)
    parser.add_argument("--baseline-db", type=Path, required=True)
    parser.add_argument("--preserve-location-id", type=int, action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    with connect(args.db, read_only=not args.apply) as conn:
        results = repair_scan_damage(
            conn,
            args.baseline_db,
            apply=args.apply,
            preserve_location_ids=args.preserve_location_id,
        )
    action = "repaired" if args.apply else "would repair"
    print(
        f"{action}: restored_metadata={results['restored_locations']}, "
        f"cleared_place_ids={results['cleared_place_ids']}, "
        f"cleared_bogus_websites={results['cleared_bogus_websites']}, "
        f"preserved_closures={results['preserved_closures']}, "
        f"demoted_google_closures={results['demoted_closures']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
