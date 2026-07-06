import argparse
import unittest

from us_states import CONTINENTAL_US_STATES, normalize_state, selected_states


class StateSelectionTests(unittest.TestCase):
    def test_normalize_state_accepts_abbreviation_and_name(self):
        self.assertEqual(normalize_state("co"), "CO")
        self.assertEqual(normalize_state("Colorado"), "CO")

    def test_selected_states_deduplicates_csv_order(self):
        args = argparse.Namespace(all_continental_us=False, states="CO,Nevada,co", state="UT")
        self.assertEqual(selected_states(args), ["CO", "NV"])

    def test_continental_selection_excludes_alaska_and_hawaii(self):
        args = argparse.Namespace(all_continental_us=True, states=None, state="UT")
        states = selected_states(args)
        self.assertNotIn("AK", states)
        self.assertNotIn("HI", states)
        self.assertEqual(states, list(CONTINENTAL_US_STATES))


if __name__ == "__main__":
    unittest.main()
