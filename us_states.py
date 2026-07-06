"""Shared U.S. state selection helpers for curation scripts."""

from __future__ import annotations

import argparse


US_STATES: dict[str, str] = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
    "DC": "District of Columbia",
}

CONTINENTAL_US_STATES: tuple[str, ...] = tuple(
    state for state in US_STATES if state not in {"AK", "HI"}
)

STATE_ALIASES = {abbr.lower(): abbr for abbr in US_STATES}
STATE_ALIASES.update({name.lower(): abbr for abbr, name in US_STATES.items()})
STATE_ALIASES.update({"washington dc": "DC", "d.c.": "DC"})


def normalize_state(value: str) -> str:
    state = STATE_ALIASES.get(value.strip().lower())
    if not state:
        raise argparse.ArgumentTypeError(f"Unknown U.S. state: {value}")
    return state


def add_state_selection_args(parser: argparse.ArgumentParser, *, default_state: str = "UT") -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--state", type=normalize_state, default=default_state)
    group.add_argument(
        "--states",
        help="Comma-separated U.S. state abbreviations/names, e.g. CO,NV,AZ.",
    )
    group.add_argument(
        "--all-continental-us",
        action="store_true",
        help="Run for the lower 48 states plus DC.",
    )


def selected_states(args: argparse.Namespace) -> list[str]:
    if getattr(args, "all_continental_us", False):
        return list(CONTINENTAL_US_STATES)
    states_arg = getattr(args, "states", None)
    if states_arg:
        seen: set[str] = set()
        states: list[str] = []
        for raw_state in states_arg.split(","):
            state = normalize_state(raw_state)
            if state not in seen:
                states.append(state)
                seen.add(state)
        return states
    return [normalize_state(getattr(args, "state", "UT"))]
