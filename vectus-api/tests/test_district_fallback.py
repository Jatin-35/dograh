"""A town the KB lacks while its district is listed: ask for the district.

The tester's calls showed the bot giving up on a small town. The old reply told
it to "tell the caller our team will call them back", and the bot obeyed that
over the prompt's district step. Simulated calls went from 0/3 to 9/9 once the
reply asked for the district instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.data import load_index
from app.matching import Lookup, lookup

KB = Path(__file__).resolve().parent.parent / "data" / "Vectus_KB_Revised.txt"


@pytest.fixture(scope="module")
def ix():
    return load_index(str(KB))


def ask(ix, **k):
    return lookup(ix, Lookup(product="Water tank", **k))


@pytest.mark.parametrize("city,state,district,manager", [
    ("Khatauli", "Uttar Pradesh", "Muzaffarnagar", "Lalit Sharma"),
    ("Bassi", "Rajasthan", "Jaipur", "Ashok Nirwan"),
])
def test_unknown_town_asks_for_its_district_then_finds_it(ix, city, state, district, manager):
    first = ask(ix, city=city, state=state)
    assert first["status"] == "not_found" and first.get("ask_district") is True
    assert "district" in first["message"] and "call them back" not in first["message"]
    second = ask(ix, district=district, state=state)
    assert second["status"] == "found" and second["salesperson"] == manager


def test_town_and_district_both_unknown_gives_the_no_match(ix):
    r = ask(ix, city="Palanpur", district="Banaskantha", state="Gujarat")
    assert r["status"] == "not_found" and "ask_district" not in r
    assert "call them back" in r["message"]


def test_unknown_district_alone_gives_the_no_match(ix):
    r = ask(ix, district="Banaskantha", state="Gujarat")
    assert r["status"] == "not_found" and "ask_district" not in r
