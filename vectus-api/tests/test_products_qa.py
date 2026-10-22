"""The realistic-caller table (qa/qa_products.py) and the false-yes sweep.

A wrong "yes, Vectus sells that" is the costly mistake on a live call, so the
sweep asks for ~100 everyday items that are not in the catalogue and allows no
plain yes for any of them.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from app import products as P

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "qa"))

from qa_products import CASES, check  # noqa: E402

DATA = HERE.parent / "data" / "products.json"
PRICE = re.compile(r"₹|\brs\.?\s*\d|\brupee|\bmrp\b|\bprice\b|\bcost\b", re.I)


@pytest.fixture(scope="module")
def cat():
    return P.load_catalog(DATA)


@pytest.mark.parametrize("req,expected", CASES, ids=[str(c[0]) for c in CASES])
def test_caller_question(cat, req, expected):
    r = P.find(cat, **req)
    assert not check(r, expected), (r["status"], r.get("product"), r.get("model"), r["message"])
    assert r["message"] and not PRICE.search(r["message"])


NOT_IN_CATALOGUE = """refrigerator, fridge, television, tv, ceiling fan, cooler, air cooler, ac,
air conditioner, chair, plastic chair, table, dining table, bed, almirah, cupboard,
pressure cooker, tiffin, lunch box, water bottle, bottle, broom, jhadu, mop, pocha, wiper,
bucket stand, cloth hanger, hanger, clothes stand, iron, water cooler, water dispenser,
dispenser bottle, mattress, pillow, curtain, door, window, tiles, cement, paint, putty,
wire, switch, mcb, bulb, led, inverter, battery, solar panel, submersible pump, motor,
water pump, tullu pump, pvc door, water meter, valve, gate valve, ball valve,
non return valve, tap washer, washer, sealant, adhesive, m seal, glue, sink stand,
mirror stand, toothbrush holder, shower curtain, bath mat, towel, soap, shampoo,
detergent, phenyl, harpic, acid, toilet cleaner, urinal, urinal pot, squatting pan,
indian seat, orissa pan, flush valve, concealed cistern, diverter, thermostatic mixer,
sensor tap, chrome tap, steel tap, ss tap, kitchen chimney, gas stove, rainwater harvesting,
chamber, inspection chamber, storage box, container, dabba, kitchen container,
rack stand, shoe rack, mug stand, geyser, wash basin, commode, mixer grinder""".replace("\n", " ")


@pytest.mark.parametrize("item", [w.strip() for w in NOT_IN_CATALOGUE.split(",")])
def test_no_plain_yes_for_items_vectus_does_not_list(cat, item):
    r = P.find(cat, product=item)
    assert r["status"] not in ("found", "product", "category", "ambiguous"), (item, r["message"])
