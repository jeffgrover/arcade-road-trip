import unittest

import duckdb

from arcade_query import search_locations
from export_static_data import load_location_games, load_route_locations
from generate_dashboard import load_location_metrics


class ActiveLocationFilteringTests(unittest.TestCase):
    def setUp(self):
        self.conn = duckdb.connect(":memory:")
        self.conn.execute(
            """
            CREATE TABLE locations (
                location_id BIGINT,
                name VARCHAR,
                type VARCHAR,
                city VARCHAR,
                state VARCHAR,
                street_address VARCHAR,
                postal_code VARCHAR,
                latitude DOUBLE,
                longitude DOUBLE,
                game_count INTEGER,
                source_url VARCHAR,
                website_url VARCHAR,
                google_place_id VARCHAR,
                google_cid VARCHAR
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE games (
                game_id BIGINT,
                name VARCHAR,
                manufacturer VARCHAR
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE location_games (
                location_id BIGINT,
                game_id BIGINT,
                cabinet_type VARCHAR
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE location_statuses (
                location_id BIGINT,
                status VARCHAR,
                replacement_name VARCHAR
            )
            """
        )
        self.conn.execute("CREATE TABLE pinballmap_location_links(location_id BIGINT)")
        self.conn.execute("CREATE TABLE ziv_location_links(location_id BIGINT)")
        self.conn.execute(
            """
            INSERT INTO locations VALUES
                (1, 'Open Arcade', 'Arcade', 'Orlando', 'FL', '1 Main', '32830', 28.5, -81.4, 1, 'https://example.test/open', 'https://open.example', 'PlaceOpen', 'CidOpen'),
                (2, 'Closed Arcade', 'Arcade', 'Orlando', 'FL', '2 Main', '32830', 28.6, -81.5, 1, 'https://example.test/closed', 'https://closed.example', 'PlaceClosed', 'CidClosed')
            """
        )
        self.conn.execute("INSERT INTO games VALUES (10, 'Test Pinball', 'Williams')")
        self.conn.execute(
            """
            INSERT INTO location_games VALUES
                (1, 10, 'Pinball'),
                (2, 10, 'Pinball')
            """
        )
        self.conn.execute(
            """
            INSERT INTO location_statuses VALUES
                (1, 'active', NULL),
                (2, 'closed', NULL)
            """
        )

    def tearDown(self):
        self.conn.close()

    def test_cli_search_excludes_closed_locations(self):
        result = search_locations(self.conn, "Arcade", limit=10)

        self.assertEqual([row["location_id"] for row in result.rows], [1])

    def test_static_export_excludes_closed_locations_and_counts(self):
        route_locations = load_route_locations(self.conn)
        location_games = load_location_games(self.conn)

        self.assertEqual([row["location_id"] for row in route_locations], [1])
        self.assertEqual(route_locations[0]["website_url"], "https://open.example")
        self.assertEqual(route_locations[0]["google_place_id"], "PlaceOpen")
        self.assertEqual([row["location_id"] for row in location_games], [1])
        self.assertEqual(location_games[0]["us_location_count"], 1)

    def test_dashboard_metrics_exclude_closed_locations_and_counts(self):
        metrics = load_location_metrics(self.conn)

        self.assertEqual([row["location_id"] for row in metrics], [1])
        self.assertEqual(metrics[0]["machine_count"], 1)
        self.assertEqual(metrics[0]["website_url"], "https://open.example")
        self.assertEqual(metrics[0]["google_place_id"], "PlaceOpen")


if __name__ == "__main__":
    unittest.main()
