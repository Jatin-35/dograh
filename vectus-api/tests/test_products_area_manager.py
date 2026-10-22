"""Which Area Manager each product goes to, as Vectus confirmed on 2026-10-22.

Household and garden items go to the Moulding Area Manager; water tanks, tank
accessories and bath-ware to the Water tank one. The garden pipe is listed
under bath-ware but stays with Moulding, as Vectus said. Road safety has no
answer from Vectus yet, so the bot must not look an Area Manager up for it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import products as P

DATA = Path(__file__).resolve().parent.parent / "data" / "products.json"


@pytest.fixture(scope="module")
def cat():
    return P.load_catalog(DATA)


@pytest.mark.parametrize("said,want", [
    ("toilet seat cover", "Water tank"), ("flush tank", "Water tank"), ("angle cock", "Water tank"),
    ("kitchen sink", "Water tank"), ("health faucet", "Water tank"), ("manhole cover", "Water tank"),
    ("tank stand", "Water tank"), ("tank filter", "Water tank"),
    ("garden pipe", "Moulding"),
    ("balti", "Moulding"), ("mug", "Moulding"), ("tub", "Moulding"), ("dustbin", "Moulding"),
    ("stool", "Moulding"), ("tasla", "Moulding"), ("jug", "Moulding"), ("drum", "Moulding"),
    ("gamla", "Moulding"), ("watering can", "Moulding"),
])
def test_each_product_carries_its_area_manager(cat, said, want):
    r = P.compact(P.find(cat, product=said))
    assert r["area_manager_product"] == want, (said, r)


def test_garden_pipe_is_listed_under_bathware_but_goes_to_moulding(cat):
    r = P.find(cat, product="garden pipe")
    assert r["category"] == "Bath-ware" and r["area_manager_product"] == "Moulding"
    assert "Garden pipe" in cat.categories["Bath-ware"]["products"]
    assert "Garden pipe" not in cat.categories["Maali planters and garden"]["products"]


def test_road_safety_has_no_area_manager_and_says_so(cat):
    r = P.compact(P.find(cat, product="speed breaker"))
    assert r["area_manager_product"] is None
    assert "do not call vectus_area_manager_lookup" in r["message"]


@pytest.mark.parametrize("said,want", [
    ("bucket aur mug", "Moulding"),
    ("seat cover aur bucket", "Water tank"),
    ("gamla aur garden pipe", "Moulding"),
])
def test_two_products_get_one_area_manager(cat, said, want):
    r = P.compact(P.find(cat, product=said))
    assert r["status"] == "multiple" and r["area_manager_product"] == want


@pytest.mark.parametrize("said", ["silk", "ppr pipe", "household", "geyser", "pipe"])
def test_answers_without_a_product_carry_none(cat, said):
    assert "area_manager_product" not in P.find(cat, product=said)


def test_a_bad_area_manager_value_is_refused(tmp_path):
    raw = json.loads(DATA.read_text(encoding="utf-8"))
    raw["categories"][0]["area_manager"] = "Pipes"
    f = tmp_path / "products.json"
    f.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(P.ProductCatalogError, match="area_manager"):
        P.load_catalog(f)
