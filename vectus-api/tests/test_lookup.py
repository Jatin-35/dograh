from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.data import load_index
from app.matching import Lookup, lookup

KB = Path(__file__).resolve().parent.parent / "data" / "Vectus_KB_Revised.txt"


@pytest.fixture(scope="module")
def ix():
    return load_index(KB)


def q(ix, product="Water tank", city=None, district=None, state=None):
    return lookup(ix, Lookup(product=product, city=city, district=district, state=state))


def contacts(result) -> set[str]:
    if result["status"] == "found":
        return {result["contact"]}
    if result["status"] == "multiple":
        return {m["contact"] for m in result["matches"]}
    return set()


# --- the KB loads the way we expect -------------------------------------------------


def test_index_shape(ix):
    assert len(ix.records) == 753
    assert ix.lead_rows == 5
    assert ix.contacts == 149
    assert "lakhimpur" in ix.ambiguous_keys


def test_no_internal_fields_leak(ix):
    r = q(ix, city="Noida")
    assert "vectus_state" not in str(r).lower()
    assert "record" not in r


# --- every record is findable by what the KB calls it --------------------------------


def test_every_record_found_by_its_own_city(ix):
    misses = []
    for rec in ix.records:
        r = q(ix, product=rec.product, city=rec.city, state=rec.state)
        if rec.contact not in contacts(r):
            misses.append((rec.city, rec.state, rec.product, r["status"]))
    assert not misses, misses[:20]


def test_every_record_found_by_its_own_district(ix):
    misses = []
    for rec in ix.records:
        r = q(ix, product=rec.product, district=rec.district, state=rec.state)
        if rec.contact not in contacts(r) and r["status"] != "too_many":
            misses.append((rec.district, rec.state, rec.product, r["status"]))
    assert not misses, misses[:20]


# --- statuses ---------------------------------------------------------------------


@pytest.mark.parametrize("city,expected", [
    ("Noida", "Noida"),
    ("noida", "Noida"),
    ("NOIDA sector 62", "Noida"),
    ("Noida district", "Noida"),
    ("Gurugram", "Gurgaon"),
    ("नोएडा", "Noida"),
])
def test_found_exact_and_alias(ix, city, expected):
    r = q(ix, city=city)
    assert r["status"] == "found"
    assert r["city"] == expected
    assert r["contact_spoken"].replace(" ", "") == r["contact"]


@pytest.mark.parametrize("city,suggested", [
    ("Noyda", "Noida"),
    ("Bareli", "Bareily"),
    ("Gaziabad", "Ghaziabad"),
])
def test_sound_alike_is_only_suggested(ix, city, suggested):
    r = q(ix, city=city)
    assert r["status"] == "confirm"
    assert "contact" not in r
    assert suggested in [s["city"] for s in r["suggestions"]]


def test_unknown_place_not_found(ix):
    assert q(ix, city="Xyzabad")["status"] == "not_found"


@pytest.mark.parametrize("city,states", [
    ("Lakhimpur", {"Assam", "Uttar Pradesh"}),
    ("Aurangabad", {"Bihar", "Maharashtra"}),
    ("Bilaspur", {"Chhattisgarh", "Himachal Pradesh"}),
])
def test_ambiguous_place_asks_for_state(ix, city, states):
    r = q(ix, city=city)
    assert r["status"] == "need_state"
    assert set(r["options"]) == states
    assert r["place"] == city


def test_state_resolves_ambiguity(ix):
    up = q(ix, city="Lakhimpur", state="UP")
    assam = q(ix, city="Lakhimpur", state="Assam")
    assert up["status"] == assam["status"] == "found"
    assert up["contact"] != assam["contact"]


def test_wrong_state_names_the_right_one(ix):
    r = q(ix, city="Noida", state="Bihar")
    assert r["status"] == "need_state"
    assert r["options"] == ["Uttar Pradesh"]


def test_ghaziabad_accepted_under_up(ix):
    # The KB files Ghaziabad under Delhi; callers say UP.
    assert q(ix, city="Ghaziabad", state="Uttar Pradesh")["status"] == "found"


@pytest.mark.parametrize("city", ["Madurai", "Mysore"])
def test_split_territory_returns_both_reps(ix, city):
    r = q(ix, city=city)
    assert r["status"] == "multiple"
    assert len(r["matches"]) == 2


def test_city_plus_district_intersects(ix):
    r = q(ix, city="Noida", district="Gautam Buddh Nagar")
    assert r["status"] in {"found", "multiple"}


# --- state only -------------------------------------------------------------------


def test_state_only_single_rep_answers(ix):
    r = q(ix, state="Kerala")
    assert r["status"] == "found"
    assert len(r["covers"]) > 1


def test_state_only_many_reps_asks_to_narrow(ix):
    r = q(ix, state="Uttar Pradesh")
    assert r["status"] == "too_many"
    assert r["count"] > 5
    assert not {"contact", "salesperson", "matches"} & r.keys()


def test_nothing_given(ix):
    assert q(ix)["status"] == "need_location"


# --- product ----------------------------------------------------------------------


@pytest.mark.parametrize("product,canonical", [
    ("tank", "Water tank"), ("Water Tank", "Water tank"), ("पानी की टंकी", "Water tank"),
    ("moulding", "Moundling"), ("molding", "Moundling"), ("Moundling", "Moundling"),
])
def test_product_variants(ix, product, canonical):
    assert q(ix, product=product, city="Noida")["product"] == canonical


def test_product_changes_the_answer(ix):
    assert q(ix, city="Noida")["contact"] != q(ix, product="moulding", city="Noida")["contact"]


def test_invalid_product(ix):
    assert q(ix, product="pipe", city="Noida")["status"] == "invalid_product"
