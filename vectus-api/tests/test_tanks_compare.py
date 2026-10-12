"""Comparisons state only what the sheet supports, and the Chemical Tank is
never pitched as a home tank.

Every pair (and trio) of tanks in each brand is compared, and each claim in
"Both have / Only X / X and Y" is checked against the tanks' own layer names
through an independent keyword reading of the sheet.
"""

from __future__ import annotations

import itertools
import re
from pathlib import Path

import pytest

from app import tanks as T

SHEET = Path(__file__).resolve().parent.parent / "data" / "Tank_details.xlsx"


@pytest.fixture(scope="module")
def cat():
    return T.load_catalog(SHEET)


# An independent reading of what each protection means in the sheet's layer names.
SIGNS = {
    "titanium-loaded outer layer": ("titanium",),
    "granite-finish outer layer": ("granite",),
    "aesthetic outer layer": ("aesthetic",),
    "heat-reflective layer": ("heat-reflective",),
    "temperature-resistant foam": ("foam",),
    "weather-resistant layer": ("weather",),
    "stress-crack-resistant layer": ("stress-crack", "stress crack"),
    "extra-strength layer": ("strength",),
    "UV / sun-shield protection": ("uv", "sun-shield"),
    "anti-bacterial / anti-microbial inner layer": ("anti-bacterial", "anti-microbial"),
}


def has(tank, kind):
    names = " ".join(tank.layer_names).lower()
    return any(s in names for s in SIGNS[kind])


def claims(message):
    """[(owners, kinds)] from 'Both have: …. Only Cool: …. Cool and Puff: ….'"""
    start = max(message.find("Both have:"), message.find("All have:"))
    body = message[start:message.find(" Answer the customer's comparison")]
    out = []
    for part in body.split(". "):
        if ":" not in part or part.startswith("Guarantee"):
            continue
        who, what = part.split(":", 1)
        items = re.split(r", (?![^()]*\))", what)  # commas inside "(Cool 2, Puff 1)" stay
        kinds = [re.sub(r" \(.*?\)$", "", k.strip().rstrip(".")) for k in items]
        kinds = [k for k in kinds if k in SIGNS]
        out.append((who.strip(), kinds))
    return out


def pairs_and_trios(cat):
    for brand in T.BRANDS:
        tanks = [t for t in cat.by_brand(brand) if t.layer_names and not t.special]
        for n in (2, 3):
            for combo in itertools.combinations(tanks, n):
                if len({t.key for t in combo}) == n:
                    yield combo


def test_every_comparison_claim_matches_the_sheet(cat):
    checked = 0
    for combo in pairs_and_trios(cat):
        names = " aur ".join(t.model for t in combo)
        r = T.find(cat, brand=combo[0].brand, model=names)
        assert r["message"].startswith("Compare:"), names
        short = {t.model: t for t in combo}
        for who, kinds in claims(r["message"]):
            if who in ("Both have", "All have"):
                owners, others = list(combo), []
            elif who.startswith("Only "):
                owners = [short[who[5:]]]
                others = [t for t in combo if t is not owners[0]]
            else:
                owners = [short[x] for x in who.split(" and ")]
                others = [t for t in combo if t not in owners]
            for kind in kinds:
                for t in owners:
                    assert has(t, kind), (names, who, kind, t.name)   # claimed, so present
                for t in others:
                    assert not has(t, kind), (names, who, kind, t.name)  # "only", so absent
                checked += 1
    assert checked > 500


def test_no_feature_of_one_tank_goes_unmentioned(cat):
    """Each tank's protections all appear somewhere in the comparison, so the
    bot is never left to guess (the run-849 false contrast)."""
    for combo in pairs_and_trios(cat):
        r = T.find(cat, brand=combo[0].brand, model=" aur ".join(t.model for t in combo))
        said = {k for _, kinds in claims(r["message"]) for k in kinds}
        for t in combo:
            for kind in SIGNS:
                if has(t, kind):
                    assert kind in said, (t.name, kind)


def test_run_849_cool_vs_tenx(cat):
    m = T.find(cat, model="TenX aur Cool")["message"]
    assert "Both have:" in m and "anti-bacterial / anti-microbial inner layer" in \
        m.split("Both have:")[1].split(". ")[0]
    assert "Only Cool: temperature-resistant foam" in m
    assert "weather-resistant layer" in m.split("Only TenX:")[1]
    assert "never imply a tank lacks a feature" in m


def test_guarantee_difference_is_stated(cat):
    m = T.find(cat, model="Cool aur Puff")["message"]
    assert "Guarantee: Cool 15 years, Puff 10 years" in m


# --- Chemical Tank ----------------------------------------------------------------


def _names(r):
    return [t["name"] for t in r.get("tanks", [])] + r.get("more", [])


@pytest.mark.parametrize("kw", [
    {"colour": "black"}, {"colour": "black", "capacity": 1500}, {"capacity": 10000},
    {"guarantee": "no"},
])
def test_chemical_tank_left_out_when_others_match(cat, kw):
    r = T.find(cat, **kw)
    assert "Vectus Chemical Tank" not in _names(r), _names(r)
    assert "Chemical" not in r["message"], r["message"][:300]


@pytest.mark.parametrize("kw", [{"layers": 1}, {"model": "chemical"}, {"need": "chemical"}])
def test_chemical_tank_labelled_when_it_is_the_answer(cat, kw):
    r = T.find(cat, **kw)
    assert _names(r) == ["Vectus Chemical Tank"]
    assert T.CHEMICAL_NOTE in r["message"]


def test_commercial_need_lists_chemical_last_and_labelled(cat):
    r = T.find(cat, need="commercial", capacity=5000)
    names = _names(r)
    assert names[-1] == "Vectus Chemical Tank", names
    assert T.CHEMICAL_NOTE in r["message"]


def test_home_black_tank_is_isi_only(cat):
    assert _names(T.find(cat, colour="black", capacity=1500)) == ["Vectus ISI tank"]
