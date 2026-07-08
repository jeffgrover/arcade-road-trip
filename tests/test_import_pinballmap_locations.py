import csv
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import duckdb

from arcade_db import execute_script
from import_pinballmap_locations import (
    ExistingLocation,
    ImportBundle,
    PinballMapLocation,
    import_bundle,
    best_location_match,
    read_pinballmap_csv,
    source_key_to_db_id,
)


HEADERS = [
    "Id",
    "Name",
    "Street",
    "City",
    "State",
    "Zip",
    "Phone",
    "Lat",
    "Lon",
    "Website",
    "Description",
    "Date last updated",
    "Machine count",
    "Name [Location type]",
    "Id [Location machine xrefs]",
    "Ic enabled [Location machine xrefs]",
    "Id [Machines]",
    "Name [Machines]",
    "Year [Machines]",
    "Manufacturer [Machines]",
    "Ipdb [Machines]",
    "Opdb [Machines]",
    "Machine type [Machines]",
    "Machine display [Machines]",
]


def memory_db() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(":memory:")


def setup_schema(conn: duckdb.DuckDBPyConnection, script: str) -> None:
    execute_script(conn, script)


class PinballMapImportTests(unittest.TestCase):
    def test_source_key_to_db_id_uses_negative_namespace(self):
        self.assertEqual(source_key_to_db_id(4426), -1000004426)

    def test_read_pinballmap_csv_flattens_location_machine_lists(self):
        row = {
            "Id": "4426",
            "Name": "Nickel Mania",
            "Street": "6051 State St",
            "City": "Murray",
            "State": "UT",
            "Zip": "84107",
            "Phone": "801-685-9229",
            "Lat": "40.6394026",
            "Lon": "-111.8886712",
            "Website": "http://nickelmaniagames.com/",
            "Description": "Nickel arcade",
            "Date last updated": "May 09, 2026",
            "Machine count": "2",
            "Name [Location type]": "Family Fun Center",
            "Id [Location machine xrefs]": "168674,153101",
            "Ic enabled [Location machine xrefs]": "nil,true",
            "Id [Machines]": "663,3519",
            "Name [Machines]": "Spider-Man,Toy Story 4 (LE)",
            "Year [Machines]": "2007,2022",
            "Manufacturer [Machines]": "Stern,Jersey Jack",
            "Ipdb [Machines]": "5237,6950",
            "Opdb [Machines]": "G5D94-MLnXq,GJ2o0-MrRye-ARNwE",
            "Machine type [Machines]": "ss,ss",
            "Machine display [Machines]": "dmd,lcd",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "pinballmap.csv"
            with path.open("w", newline="") as csv_file:
                writer = csv.DictWriter(csv_file, fieldnames=HEADERS)
                writer.writeheader()
                writer.writerow(row)

            bundle = read_pinballmap_csv(path)

        self.assertEqual(len(bundle.locations), 1)
        self.assertEqual(bundle.locations[0].machine_count, 2)
        self.assertEqual(bundle.locations[0].location_type, "Family Center")
        self.assertEqual(len(bundle.machines), 2)
        self.assertEqual(bundle.machines[3519].name, "Toy Story 4 (LE)")
        self.assertEqual(len(bundle.placements), 2)
        self.assertEqual(bundle.placements[1].pinballmap_lmx_id, 153101)
        self.assertTrue(bundle.placements[1].ic_enabled)

    def test_location_match_accepts_same_address_when_city_differs(self):
        pinballmap_location = PinballMapLocation(
            pinballmap_location_id=4428,
            name="Arcade Galactic",
            street_address="3601 Constitution Blvd Suite G114",
            city="Salt Lake City",
            state="UT",
            postal_code="84119",
            phone=None,
            website_url=None,
            description=None,
            latitude=None,
            longitude=None,
            location_type="Arcade",
            machine_count=8,
            updated_text=None,
        )
        aurcade_locations = [
            ExistingLocation(
                location_id=120,
                name="Arcade Galactic",
                city="West Valley City",
                state="UT",
                street_address="3601 Constitution Blvd. STE G114",
                postal_code="84119",
            )
        ]

        match = best_location_match(pinballmap_location, aurcade_locations, threshold=0.78)

        self.assertIsNotNone(match)
        self.assertEqual(match.location_id, 120)

    def test_location_match_rejects_chain_name_with_different_address(self):
        pinballmap_location = PinballMapLocation(
            pinballmap_location_id=10933,
            name="Nickel Mania",
            street_address="7800 South, 3245 West",
            city="West Jordan",
            state="UT",
            postal_code="84088",
            phone=None,
            website_url=None,
            description=None,
            latitude=None,
            longitude=None,
            location_type="Family Center",
            machine_count=69,
            updated_text=None,
        )
        aurcade_locations = [
            ExistingLocation(
                location_id=695,
                name="Nickel Mania",
                city="West Jordan",
                state="UT",
                street_address="3763 Center Park Drive, Suite 110",
                postal_code="84084",
            )
        ]

        match = best_location_match(pinballmap_location, aurcade_locations, threshold=0.78)

        self.assertIsNone(match)

    def test_import_uses_manual_location_override(self):
        conn = memory_db()
        setup_schema(
            conn,
            """
            CREATE TABLE location_types (type VARCHAR);
            CREATE TABLE locations (
                location_id BIGINT,
                name VARCHAR NOT NULL,
                type VARCHAR,
                city VARCHAR,
                state VARCHAR,
                street_address VARCHAR,
                postal_code VARCHAR,
                phone VARCHAR,
                address_text VARCHAR,
                website_url VARCHAR,
                is_public BIGINT,
                game_count BIGINT,
                unique_game_count BIGINT,
                world_record_count BIGINT,
                updated_text VARCHAR,
                description VARCHAR,
                latitude DOUBLE,
                longitude DOUBLE,
                detail_fetched_at VARCHAR,
                source_url VARCHAR NOT NULL
            );
            CREATE TABLE games (
                game_id BIGINT,
                name VARCHAR NOT NULL,
                manufacturer VARCHAR
            );
            CREATE TABLE location_games (
                location_id BIGINT NOT NULL,
                game_id BIGINT NOT NULL,
                cabinet_type VARCHAR,
                year BIGINT,
                players BIGINT,
                controls_condition BIGINT,
                screen_condition BIGINT,
                cabinet_condition BIGINT,
                fetched_at VARCHAR NOT NULL
            );
            INSERT INTO locations(location_id, name, city, state, street_address, postal_code, game_count, source_url)
            VALUES (695, 'Nickel Mania', 'West Jordan', 'UT', '3763 Center Park Drive, Suite 110', '84084', 71, 'https://www.aurcade.com/locations/view.aspx?id=695');
            """
        )
        bundle = ImportBundle(
            locations=[
                PinballMapLocation(
                    pinballmap_location_id=10933,
                    name="Nickel Mania",
                    street_address="7800 South, 3245 West",
                    city="West Jordan",
                    state="UT",
                    postal_code="84088",
                    phone=None,
                    website_url=None,
                    description=None,
                    latitude=None,
                    longitude=None,
                    location_type="Family Center",
                    machine_count=0,
                    updated_text=None,
                )
            ],
            machines={},
            placements=[],
        )

        with contextlib.redirect_stdout(io.StringIO()):
            stats = import_bundle(
                conn,
                bundle,
                apply=False,
                insert_unmatched_locations=True,
                insert_unmatched_games=True,
                location_match_threshold=0.78,
                game_match_threshold=0.86,
                verbose=False,
            )

        self.assertEqual(stats.locations_matched, 1)
        self.assertEqual(stats.locations_inserted, 0)

    def test_location_match_rejects_same_name_in_different_city(self):
        pinballmap_location = PinballMapLocation(
            pinballmap_location_id=1,
            name="Arcade Galactic",
            street_address=None,
            city="Salt Lake City",
            state="UT",
            postal_code=None,
            phone=None,
            website_url=None,
            description=None,
            latitude=None,
            longitude=None,
            location_type="Arcade",
            machine_count=1,
            updated_text=None,
        )
        aurcade_locations = [
            ExistingLocation(
                location_id=120,
                name="Arcade Galactic",
                city="Ogden",
                state="UT",
                street_address=None,
                postal_code=None,
            )
        ]

        match = best_location_match(pinballmap_location, aurcade_locations, threshold=0.78)

        self.assertIsNone(match)

    def test_import_preserves_existing_aurcade_counts_and_fetched_at(self):
        conn = memory_db()
        setup_schema(
            conn,
            """
            CREATE TABLE location_types (type VARCHAR);
            CREATE TABLE locations (
                location_id BIGINT,
                name VARCHAR NOT NULL,
                type VARCHAR,
                city VARCHAR,
                state VARCHAR,
                street_address VARCHAR,
                postal_code VARCHAR,
                phone VARCHAR,
                address_text VARCHAR,
                website_url VARCHAR,
                is_public BIGINT,
                game_count BIGINT,
                unique_game_count BIGINT,
                world_record_count BIGINT,
                updated_text VARCHAR,
                description VARCHAR,
                latitude DOUBLE,
                longitude DOUBLE,
                detail_fetched_at VARCHAR,
                source_url VARCHAR NOT NULL
            );
            CREATE TABLE games (
                game_id BIGINT,
                name VARCHAR NOT NULL,
                manufacturer VARCHAR
            );
            CREATE TABLE location_games (
                location_id BIGINT NOT NULL,
                game_id BIGINT NOT NULL,
                cabinet_type VARCHAR,
                year BIGINT,
                players BIGINT,
                controls_condition BIGINT,
                screen_condition BIGINT,
                cabinet_condition BIGINT,
                fetched_at VARCHAR NOT NULL
            );
            INSERT INTO locations(location_id, name, city, state, street_address, postal_code, game_count, source_url)
            VALUES (700, 'Nickel Mania', 'Murray', 'UT', '6051 South State', '84107', 105, 'https://www.aurcade.com/locations/view.aspx?id=700');
            INSERT INTO games(game_id, name, manufacturer)
            VALUES (640, 'Spider-man', 'Stern Pinball');
            INSERT INTO location_games(location_id, game_id, cabinet_type, year, fetched_at)
            VALUES (700, 640, 'Pinball', 2007, 'aurcade-fetch');
            """
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "pinballmap.csv"
            row = {
                "Id": "4426",
                "Name": "Nickel Mania",
                "Street": "6051 State St",
                "City": "Murray",
                "State": "UT",
                "Zip": "84107",
                "Phone": "",
                "Lat": "",
                "Lon": "",
                "Website": "",
                "Description": "",
                "Date last updated": "",
                "Machine count": "1",
                "Name [Location type]": "Family Fun Center",
                "Id [Location machine xrefs]": "168674",
                "Ic enabled [Location machine xrefs]": "nil",
                "Id [Machines]": "663",
                "Name [Machines]": "Spider-Man",
                "Year [Machines]": "2007",
                "Manufacturer [Machines]": "Stern",
                "Ipdb [Machines]": "5237",
                "Opdb [Machines]": "G5D94-MLnXq",
                "Machine type [Machines]": "ss",
                "Machine display [Machines]": "dmd",
            }
            with path.open("w", newline="") as csv_file:
                writer = csv.DictWriter(csv_file, fieldnames=HEADERS)
                writer.writeheader()
                writer.writerow(row)
            bundle = read_pinballmap_csv(path)

        with contextlib.redirect_stdout(io.StringIO()):
            import_bundle(
                conn,
                bundle,
                apply=True,
                insert_unmatched_locations=True,
                insert_unmatched_games=True,
                location_match_threshold=0.78,
                game_match_threshold=0.86,
                verbose=False,
            )
            import_bundle(
                conn,
                bundle,
                apply=True,
                insert_unmatched_locations=True,
                insert_unmatched_games=True,
                location_match_threshold=0.78,
                game_match_threshold=0.86,
                verbose=False,
            )

        game_count = conn.execute(
            "SELECT game_count FROM locations WHERE location_id = 700"
        ).fetchone()[0]
        fetched_at = conn.execute(
            "SELECT fetched_at FROM location_games WHERE location_id = 700 AND game_id = 640"
        ).fetchone()[0]

        self.assertEqual(game_count, 105)
        self.assertEqual(fetched_at, "aurcade-fetch")

    def test_import_bundle_skips_ambiguous_location_inserts(self):
        conn = memory_db()
        setup_schema(
            conn,
            """
            CREATE TABLE location_types (type VARCHAR);
            CREATE TABLE locations (
                location_id BIGINT,
                name VARCHAR,
                type VARCHAR,
                city VARCHAR,
                state VARCHAR,
                street_address VARCHAR,
                postal_code VARCHAR,
                phone VARCHAR,
                address_text VARCHAR,
                website_url VARCHAR,
                is_public BIGINT,
                game_count BIGINT,
                unique_game_count BIGINT,
                world_record_count BIGINT,
                updated_text VARCHAR,
                description VARCHAR,
                latitude DOUBLE,
                longitude DOUBLE,
                detail_fetched_at VARCHAR,
                source_url VARCHAR
            );
            CREATE TABLE games (
                game_id BIGINT,
                name VARCHAR,
                manufacturer VARCHAR
            );
            CREATE TABLE location_games (
                location_id BIGINT,
                game_id BIGINT,
                cabinet_type VARCHAR,
                year BIGINT,
                players BIGINT,
                controls_condition BIGINT,
                screen_condition BIGINT,
                cabinet_condition BIGINT,
                fetched_at VARCHAR
            );
            INSERT INTO locations(location_id, name, city, state, street_address, postal_code, source_url)
            VALUES (10, 'Quarters Arcade Bar', 'Salt Lake City', 'UT', '5 E 400 S', '84111', 'aurcade');
            """
        )
        bundle = ImportBundle(
            locations=[
                PinballMapLocation(
                    pinballmap_location_id=123,
                    name="Quarters Arcade",
                    street_address="99 W Different St",
                    city="Salt Lake City",
                    state="UT",
                    postal_code="84111",
                    phone=None,
                    website_url=None,
                    description=None,
                    latitude=None,
                    longitude=None,
                    location_type="Arcade",
                    machine_count=0,
                    updated_text=None,
                )
            ],
            machines={},
            placements=[],
        )

        with contextlib.redirect_stdout(io.StringIO()):
            stats = import_bundle(
                conn,
                bundle,
                apply=True,
                insert_unmatched_locations=True,
                insert_unmatched_games=True,
                location_match_threshold=0.95,
                game_match_threshold=0.86,
                verbose=False,
                ambiguous_location_threshold=0.65,
            )

        self.assertEqual(stats.locations_skipped, 1)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM locations WHERE location_id < 0").fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
