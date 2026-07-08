#!/usr/bin/env python3
"""One-command Arcade Road Trip data sync orchestration.

This is the operations entrypoint. It syncs source data into canonical DuckDB,
runs validation/curation phases, and rebuilds the static atlas artifact.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


DEFAULT_LEGACY_SQLITE_DB = Path("aurcade_locations.sqlite")
DEFAULT_DUCKDB = Path("arcade_roadtrip.duckdb")
DEFAULT_REPORT_DIR = Path("reports")


@dataclass(frozen=True)
class SyncStep:
    phase: str
    name: str
    command: tuple[str, ...]
    networked: bool = False
    mutates: bool = False


def state_args(args: argparse.Namespace) -> list[str]:
    if args.all_continental_us:
        return ["--all-continental-us"]
    if args.states:
        return ["--states", args.states]
    return ["--state", args.state]


def apply_flag(args: argparse.Namespace) -> list[str]:
    return ["--apply"] if args.apply else []


def source_skip_args(source: str) -> list[str]:
    if source == "pinballmap":
        return ["--skip-ziv"]
    if source == "ziv":
        return ["--skip-pinballmap"]
    return []


def build_sync_steps(args: argparse.Namespace, python: str = sys.executable) -> list[SyncStep]:
    steps: list[SyncStep] = []
    scoped = state_args(args)

    if not args.skip_source_sync:
        steps.append(
            SyncStep(
                phase="source-sync",
                name="curate source updates",
                command=(
                    python,
                    "curate_us_sources.py",
                    "--db",
                    str(args.duckdb),
                    "--report-dir",
                    str(args.report_dir),
                    *scoped,
                    *apply_flag(args),
                    *source_skip_args(args.source),
                    "--cache-hours",
                    str(args.cache_hours),
                    "--delay-seconds",
                    str(args.delay_seconds),
                    "--log-file",
                    str(args.report_dir / "sync_arcade_data.log"),
                ),
                networked=True,
                mutates=args.apply,
            )
        )

    if not args.skip_validation:
        if args.validation in ("all", "pinballmap") and args.source in ("all", "pinballmap"):
            steps.append(
                SyncStep(
                    phase="validation",
                    name="validate Pinball Map links",
                    command=(
                        python,
                        "validate_pinballmap_locations.py",
                        "--db",
                        str(args.duckdb),
                        *scoped,
                        "--limit",
                        str(args.validation_limit),
                        *apply_flag(args),
                    ),
                    networked=True,
                    mutates=args.apply,
                )
            )
        if args.validation in ("all", "ziv") and args.source in ("all", "ziv"):
            steps.append(
                SyncStep(
                    phase="validation",
                    name="validate ZIv links",
                    command=(
                        python,
                        "validate_ziv_locations.py",
                        "--db",
                        str(args.duckdb),
                        *scoped,
                        "--limit",
                        str(args.validation_limit),
                        *apply_flag(args),
                    ),
                    networked=True,
                    mutates=args.apply,
                )
            )
        if args.validation in ("all", "osm") and args.include_osm_validation:
            steps.append(
                SyncStep(
                    phase="validation",
                    name="validate OSM/Nominatim locations",
                    command=(
                        python,
                        "verify_locations_osm.py",
                        "--db",
                        str(args.duckdb),
                        *scoped,
                        "--limit",
                        str(args.osm_limit),
                        *apply_flag(args),
                    ),
                    networked=True,
                    mutates=args.apply,
                )
            )

    if args.refresh_from_sqlite and not args.skip_migration:
        steps.append(
            SyncStep(
                phase="database-bootstrap",
                name="refresh canonical DuckDB from legacy SQLite",
                command=(
                    python,
                    "migrate_sqlite_to_duckdb.py",
                    "--sqlite",
                    str(args.legacy_sqlite_db),
                    "--duckdb",
                    str(args.duckdb),
                    "--replace",
                ),
                mutates=True,
            )
        )

    if not args.skip_canonicalization and args.source in ("all", "pinballmap", "ziv"):
        steps.append(
            SyncStep(
                phase="curation",
                name="canonicalize game aliases",
                command=(
                    python,
                    "canonicalize_games.py",
                    "--db",
                    str(args.duckdb),
                    "--report",
                    *apply_flag(args),
                ),
                mutates=args.apply,
            )
        )

    if not args.skip_build:
        steps.append(
            SyncStep(
                phase="artifact-build",
                name="generate static atlas",
                command=(
                    python,
                    "generate_static_app.py",
                    "--db",
                    str(args.duckdb),
                    "--output",
                    str(args.output),
                ),
                mutates=True,
            )
        )

    return steps


def run_steps(steps: list[SyncStep], dry_run: bool) -> None:
    for index, step in enumerate(steps, start=1):
        command_text = " ".join(step.command)
        print(f"[{index}/{len(steps)}] {step.phase}: {step.name}")
        print(f"  {command_text}")
        if dry_run:
            continue
        subprocess.run(step.command, check=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync source arcade data into DuckDB and rebuild the static atlas.")
    parser.add_argument("--legacy-sqlite-db", type=Path, default=DEFAULT_LEGACY_SQLITE_DB)
    parser.add_argument("--duckdb", type=Path, default=DEFAULT_DUCKDB)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--output", type=Path, default=Path("static/arcade_road_trip.html"))
    parser.add_argument("--source", choices=("all", "pinballmap", "ziv"), default="all")
    parser.add_argument("--validation", choices=("all", "pinballmap", "ziv", "osm"), default="all")
    parser.add_argument("--state", default="UT")
    parser.add_argument("--states")
    parser.add_argument("--all-continental-us", action="store_true")
    parser.add_argument("--cache-hours", type=float, default=24 * 7)
    parser.add_argument("--delay-seconds", type=float, default=1.0)
    parser.add_argument("--validation-limit", type=int, default=100)
    parser.add_argument("--osm-limit", type=int, default=25)
    parser.add_argument("--apply", action="store_true", help="Apply source/validation writes. Without this, wrapped source steps dry-run.")
    parser.add_argument("--plan-only", action="store_true", help="Print the phase plan without running commands.")
    parser.add_argument("--skip-source-sync", action="store_true")
    parser.add_argument("--skip-canonicalization", action="store_true")
    parser.add_argument("--skip-validation", action="store_true")
    parser.add_argument("--include-osm-validation", action="store_true", help="Include rate-limited Nominatim validation.")
    parser.add_argument("--refresh-from-sqlite", action="store_true", help="Bootstrap DuckDB from the legacy SQLite snapshot before curation/build.")
    parser.add_argument("--skip-migration", action="store_true", help="Deprecated alias for suppressing --refresh-from-sqlite.")
    parser.add_argument("--skip-build", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.report_dir.mkdir(parents=True, exist_ok=True)
    steps = build_sync_steps(args)
    run_steps(steps, dry_run=args.plan_only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
