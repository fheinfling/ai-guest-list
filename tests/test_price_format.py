"""The price a person reads, and the guarantee that both surfaces read it the same.

The table below is duplicated verbatim in app/web/render.test.mjs. Two languages format prices —
Python for the CLI and terminal consent, JS for the popover — and they must never print different
numbers for the same model. Asserting one shared table in both suites is what stops them drifting;
a change on one side fails on the other.
"""
from __future__ import annotations

import pytest

from acctsw.pricing import format_rate

# raw catalog value -> what a person should see (no currency symbol; callers add it)
TABLE = {
    "3.000000": "3",          # full precision from a catalog must not reach the screen
    "15": "15",
    "150": "150",
    "0.5": "0.5",
    "0.075": "0.075",
    "0.0002": "0.0002",
    "0.00000025": "0.00000025",  # sub-cent rates stay comparable, not collapsed to 0.00
    "0": "0",                 # a KNOWN zero is a real price (a :free model), not an unknown
    "0.3125": "0.313",        # half-up, matching money convention and the JS formatter
    "1.005": "1.01",
    "0.6496": "0.65",         # ~0.06% apart from 0.65 — collapsing these is intended
    "1.027": "1.03",
}


@pytest.mark.parametrize("raw,shown", TABLE.items())
def test_rates_render_the_way_people_read_prices(raw, shown):
    assert format_rate(raw) == shown


def test_unknown_never_becomes_a_number():
    for bad in (None, "", "oops", "-1", "NaN", "Infinity", float("nan")):
        assert format_rate(bad) is None, f"{bad!r} must not format as a price"
