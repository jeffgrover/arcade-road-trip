import unittest

from scrape_aurcade_locations import TableParser, parse_address, parse_index_rows


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


if __name__ == "__main__":
    unittest.main()
