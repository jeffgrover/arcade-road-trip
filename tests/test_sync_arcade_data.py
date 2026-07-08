import unittest
from argparse import Namespace
from pathlib import Path

from sync_arcade_data import build_sync_steps


def args(**overrides):
    defaults = {
        "legacy_sqlite_db": Path("legacy.sqlite"),
        "duckdb": Path("arcade.duckdb"),
        "report_dir": Path("reports"),
        "output": Path("static/arcade_road_trip.html"),
        "source": "all",
        "validation": "all",
        "state": "UT",
        "states": None,
        "all_continental_us": False,
        "cache_hours": 168,
        "delay_seconds": 1,
        "validation_limit": 100,
        "osm_limit": 25,
        "include_aurcade_scrape": False,
        "aurcade_delay": 0.5,
        "aurcade_index_only": False,
        "aurcade_include_games": False,
        "aurcade_limit": None,
        "apply": False,
        "plan_only": False,
        "skip_source_sync": False,
        "skip_canonicalization": False,
        "skip_validation": False,
        "include_osm_validation": False,
        "refresh_from_sqlite": False,
        "skip_migration": False,
        "skip_build": False,
    }
    defaults.update(overrides)
    return Namespace(**defaults)


class SyncArcadeDataTests(unittest.TestCase):
    def test_default_plan_keeps_phases_separate_and_builds_artifact(self):
        steps = build_sync_steps(args(), python="python")

        self.assertEqual(
            ["source-sync", "validation", "validation", "curation", "artifact-build"],
            [step.phase for step in steps],
        )
        self.assertEqual("curate source updates", steps[0].name)
        self.assertIn("arcade.duckdb", steps[0].command)
        self.assertFalse(any("migrate_sqlite_to_duckdb.py" in step.command for step in steps))
        self.assertIn("canonicalize_games.py", steps[-2].command)
        self.assertIn("arcade.duckdb", steps[-2].command)
        self.assertIn("generate_static_app.py", steps[-1].command)
        self.assertNotIn("--apply", steps[0].command)

    def test_legacy_sqlite_bootstrap_is_optional(self):
        steps = build_sync_steps(args(refresh_from_sqlite=True), python="python")
        bootstrap = next(step for step in steps if step.phase == "database-bootstrap")

        self.assertIn("migrate_sqlite_to_duckdb.py", bootstrap.command)
        self.assertIn("legacy.sqlite", bootstrap.command)
        self.assertIn("arcade.duckdb", bootstrap.command)

    def test_apply_is_forwarded_to_mutating_wrapped_steps(self):
        steps = build_sync_steps(args(apply=True), python="python")

        curate = next(step for step in steps if step.phase == "curation")
        source_sync = next(step for step in steps if step.phase == "source-sync")
        validation_steps = [step for step in steps if step.phase == "validation"]
        self.assertIn("--apply", source_sync.command)
        self.assertIn("--apply", curate.command)
        self.assertTrue(all("--apply" in step.command for step in validation_steps))

    def test_can_limit_source_and_skip_build(self):
        steps = build_sync_steps(args(source="pinballmap", skip_build=True), python="python")
        commands = [" ".join(step.command) for step in steps]

        self.assertIn("--skip-ziv", steps[0].command)
        self.assertTrue(any("validate_pinballmap_locations.py" in command for command in commands))
        self.assertFalse(any("validate_ziv_locations.py" in command for command in commands))
        self.assertFalse(any("generate_static_app.py" in command for command in commands))

    def test_osm_validation_is_opt_in(self):
        default_steps = build_sync_steps(args(validation="osm"), python="python")
        osm_steps = build_sync_steps(args(validation="osm", include_osm_validation=True), python="python")

        self.assertFalse(any("verify_locations_osm.py" in step.command for step in default_steps))
        self.assertTrue(any("verify_locations_osm.py" in step.command for step in osm_steps))

    def test_aurcade_scrape_is_explicit_source_sync(self):
        steps = build_sync_steps(
            args(
                source="aurcade",
                aurcade_limit=5,
                skip_validation=True,
                skip_build=True,
            ),
            python="python",
        )
        commands = [" ".join(step.command) for step in steps]

        self.assertTrue(any("scrape_aurcade_locations.py" in command for command in commands))
        self.assertFalse(any("curate_us_sources.py" in command for command in commands))
        self.assertIn("--limit", steps[0].command)


if __name__ == "__main__":
    unittest.main()
