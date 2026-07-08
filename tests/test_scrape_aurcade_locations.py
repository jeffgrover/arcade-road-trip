import unittest

import duckdb

from scrape_aurcade_locations import (
    LocationDetail,
    LocationGame,
    LocationIndexRow,
    TableParser,
    ensure_schema,
    existing_detail_ids,
    next_scrape_run_id,
    parse_address,
    parse_index_rows,
    upsert_detail,
    upsert_games,
    upsert_index_row,
)


class AurcadeParserTests(unittest.TestCase):
    def test_table_parser_reads_target_table(self):
        parser = TableParser(table_id="tblItems")
        parser.feed(
            """
            <table id="tblItems">
              <tr><td>#</td><td>Name</td></tr>
              <tr><td>1.</td><td><a href="/locations/view.aspx?id=1">Test</a></td></tr>
            </table>
            """
        )

        self.assertEqual(parser.rows[1][1]["text"], "Test")
        self.assertEqual(parser.rows[1][1]["links"], ["/locations/view.aspx?id=1"])

    def test_parse_index_rows(self):
        rows = parse_index_rows(
            """
            <table id="tblItems">
              <tr>
                <td>#</td><td>Name</td><td>Games</td><td>Type</td>
                <td>City</td><td>State</td><td>Public?</td><td>Links</td>
              </tr>
              <tr class="list-odd">
                <td>1.</td>
                <td><a href="/locations/view.aspx?id=323">ABC Family Bowl</a></td>
                <td>1</td><td>Bowling Alley</td><td>Moreno Valley</td>
                <td>CA</td><td>Yes</td>
                <td><a href="http://www.abcmovalbowl.com/">web</a></td>
              </tr>
            </table>
            """
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].location_id, 323)
        self.assertEqual(rows[0].name, "ABC Family Bowl")
        self.assertTrue(rows[0].is_public)

    def test_parse_address(self):
        parsed = parse_address("23750 Alessandro Blvd\nMoreno Valley, CA 92553\n(951) 656-9088")

        self.assertEqual(parsed["street_address"], "23750 Alessandro Blvd")
        self.assertEqual(parsed["city"], "Moreno Valley")
        self.assertEqual(parsed["state"], "CA")
        self.assertEqual(parsed["postal_code"], "92553")
        self.assertEqual(parsed["phone"], "(951) 656-9088")


class AurcadeDuckDBWriteTests(unittest.TestCase):
    def setUp(self):
        self.conn = duckdb.connect(":memory:")
        ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_next_scrape_run_id_uses_existing_max(self):
        self.assertEqual(next_scrape_run_id(self.conn), 1)

        self.conn.execute(
            "INSERT INTO scrape_runs(id, started_at, source_url, include_games) VALUES (7, 'now', 'url', 0)"
        )

        self.assertEqual(next_scrape_run_id(self.conn), 8)

    def test_upsert_index_row_is_idempotent_and_preserves_detail_fields(self):
        row = LocationIndexRow(
            location_id=323,
            name="ABC Family Bowl",
            game_count=1,
            location_type="Bowling Alley",
            city="Moreno Valley",
            state="CA",
            is_public=True,
            website_url="https://example.test",
        )

        upsert_index_row(self.conn, row, "Bowling Alley", "first")
        self.conn.execute(
            "UPDATE locations SET street_address = '23750 Alessandro Blvd' WHERE location_id = 323"
        )
        updated = LocationIndexRow(
            location_id=323,
            name="ABC Bowl",
            game_count=2,
            location_type="Arcade",
            city="Riverside",
            state="CA",
            is_public=False,
            website_url="https://new.example.test",
        )
        upsert_index_row(self.conn, updated, "Arcade", "second")

        location = self.conn.execute(
            """
            SELECT name, type, city, state, website_url, is_public, game_count, street_address
            FROM locations
            WHERE location_id = 323
            """
        ).fetchone()
        index_rows = self.conn.execute(
            "SELECT filter_type, seen_at FROM location_index_sources WHERE location_id = 323 ORDER BY filter_type"
        ).fetchall()

        self.assertEqual(location, ("ABC Bowl", "Bowling Alley", "Moreno Valley", "CA", "https://example.test", 1, 1, "23750 Alessandro Blvd"))
        self.assertEqual(index_rows, [("Arcade", "second"), ("Bowling Alley", "first")])

    def test_upsert_detail_updates_detail_fields_and_existing_detail_ids(self):
        detail = LocationDetail(
            location_id=323,
            name="ABC Family Bowl",
            location_type="Bowling Alley",
            updated_text="Updated",
            website_url="https://example.test",
            address_text="23750 Alessandro Blvd\nMoreno Valley, CA 92553",
            street_address="23750 Alessandro Blvd",
            city="Moreno Valley",
            state="CA",
            postal_code="92553",
            phone="(951) 656-9088",
            game_count=3,
            unique_game_count=2,
            world_record_count=1,
            description="Family bowling center",
            latitude=33.917,
            longitude=-117.245,
        )

        upsert_detail(self.conn, detail, "fetched")

        row = self.conn.execute(
            """
            SELECT name, street_address, postal_code, game_count, unique_game_count,
                   world_record_count, detail_fetched_at
            FROM locations
            WHERE location_id = 323
            """
        ).fetchone()
        self.assertEqual(row, ("ABC Family Bowl", "23750 Alessandro Blvd", "92553", 3, 2, 1, "fetched"))
        self.assertEqual(existing_detail_ids(self.conn), {323})

    def test_upsert_games_is_idempotent(self):
        game = LocationGame(
            location_id=323,
            game_id=99,
            name="Galaga",
            cabinet_type="Upright",
            manufacturer="Namco",
            year=1981,
            players=2,
            controls_condition=5,
            screen_condition=4,
            cabinet_condition=3,
        )

        self.assertEqual(upsert_games(self.conn, [game], "first"), 1)
        changed = LocationGame(
            location_id=323,
            game_id=99,
            name="Galaga",
            cabinet_type="Cabaret",
            manufacturer=None,
            year=1981,
            players=2,
            controls_condition=4,
            screen_condition=4,
            cabinet_condition=4,
        )
        self.assertEqual(upsert_games(self.conn, [changed], "second"), 1)

        game_rows = self.conn.execute("SELECT game_id, name, manufacturer FROM games").fetchall()
        location_game_rows = self.conn.execute(
            """
            SELECT location_id, game_id, cabinet_type, controls_condition,
                   cabinet_condition, fetched_at
            FROM location_games
            """
        ).fetchall()

        self.assertEqual(game_rows, [(99, "Galaga", "Namco")])
        self.assertEqual(location_game_rows, [(323, 99, "Cabaret", 4, 4, "second")])


if __name__ == "__main__":
    unittest.main()
