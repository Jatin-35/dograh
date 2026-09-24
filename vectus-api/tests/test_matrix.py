"""Every KB record x every way of asking for it.

For each generated request an independent oracle computes which reps could
legitimately answer it (same product, the place's key, and — if a state was
given — that state). Two properties are checked over the whole matrix:

  safety  a returned number is always one the oracle allows
  recall  a request that names the place plainly gets an answer
          (found / multiple, or need_state when the place is ambiguous
          and no state was given)
"""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

import pytest

from app import aliases as A
from app.data import load_index
from app.matching import Lookup, lookup
from app.text import norm

KB = Path(__file__).resolve().parent.parent / "data" / "Vectus_KB_Revised.txt"

ABBR = {"uttar pradesh": "UP", "madhya pradesh": "MP", "himachal pradesh": "HP",
        "andhra pradesh": "AP", "maharashtra": "MH", "tamil nadu": "TN", "west bengal": "WB",
        "jammu and kashmir": "J&K", "chhattisgarh": "CG", "rajasthan": "RJ", "gujarat": "GJ",
        "punjab": "PB", "haryana": "HR", "bihar": "BR", "jharkhand": "JH", "telangana": "TS",
        "odisha": "Orissa", "uttarakhand": "UK", "delhi": "NCT of Delhi", "karnataka": "KA",
        "kerala": "KL", "assam": "Asam"}
NEIGHBOUR = {"uttar pradesh": "Bihar", "bihar": "Jharkhand", "madhya pradesh": "Rajasthan",
             "rajasthan": "Gujarat", "haryana": "Punjab", "punjab": "Haryana",
             "maharashtra": "Karnataka", "karnataka": "Tamil Nadu", "tamil nadu": "Kerala",
             "kerala": "Karnataka", "andhra pradesh": "Telangana", "telangana": "Andhra Pradesh",
             "west bengal": "Odisha", "odisha": "West Bengal", "assam": "West Bengal",
             "gujarat": "Maharashtra", "jharkhand": "Bihar", "chhattisgarh": "Odisha",
             "himachal pradesh": "Uttarakhand", "uttarakhand": "Himachal Pradesh",
             "jammu and kashmir": "Punjab", "delhi": "Rajasthan"}
TANK_PRODUCTS = ["Water tank", "water tank", "TANK", "tanki", "टंकी", "Puff", "Granito",
                 "Vectus Cool", "cool tank"]
MOULD_PRODUCTS = ["Moundling", "moulding", "Molding", "मोल्डिंग"]


@pytest.fixture(scope="module")
def ix():
    return load_index(KB)


def oracle(ix, product, key, state):
    """Contacts that may legitimately answer (product, place key, state)."""
    allowed = A.STATE_REGIONS.get(state, {state}) if state else None
    return {r.contact for r in ix.by_key.get((product, key), [])
            if allowed is None or r.states & allowed}


def variations(rec):
    """(label, product, city, district, state, plain?) for one record."""
    c, d, st = rec.city, rec.district, rec.state
    n = norm(st)
    prods = TANK_PRODUCTS if rec.product == A.WATER_TANK else MOULD_PRODUCTS
    out = []
    for p in prods:
        out.append(("product-variant", p, c, None, st, True))
    p = rec.product
    out += [
        ("city", p, c, None, None, True),
        ("district", p, None, d, None, True),
        ("city+district", p, c, d, None, True),
        ("city+state", p, c, None, st, True),
        ("district+state", p, None, d, st, True),
        ("all three", p, c, d, st, True),
        ("upper", p, c.upper(), None, st, True),
        ("lower+spaces", p, f"  {c.lower()}  ", None, st, True),
        ("punctuation", p, f"{c}.", None, st, True),
        ("district word", p, f"{c} district", None, st, True),
        ("zila", p, f"zila {c}", None, st, True),
        ("city word", p, f"{c} city", None, st, True),
        ("ke paas", p, f"{c} ke paas", None, st, True),
        ("mein", p, f"{c} mein", None, st, True),
        ("pincode", p, f"{c} 123456", None, st, True),
        ("-M marker", p, f"{c} -M", None, st, True),
        ("area manager for", p, f"Area Manager for {c}", None, st, True),
        ("state appended", p, f"{c}, {st}", None, None, True),
        ("state prefixed", p, f"{st} {c}", None, None, True),
        ("state in district", p, c, st, None, True),
        ("state lower", p, c, None, st.lower(), True),
        ("old vectus zone", p, c, None, rec.vectus_state.replace("_", " "), False),
    ]
    if n in ABBR:
        out.append(("state abbr", p, c, None, ABBR[n], True))
        out.append(("abbr appended", p, f"{c} {ABBR[n]}", None, None, True))
    if n in NEIGHBOUR:
        out.append(("wrong state", p, c, None, NEIGHBOUR[n], False))
    other = A.MOULDING if rec.product == A.WATER_TANK else A.WATER_TANK
    out.append(("other product", other, c, None, st, False))
    return out


def _state_for_oracle(state_text):
    from app.matching import canonical_state
    return canonical_state(state_text)[0] if state_text else None


def test_matrix(ix):
    stats: Counter = Counter()
    by_label: dict[str, Counter] = defaultdict(Counter)
    unsafe, missed = [], []
    key_of = {}
    for rec in ix.records:
        key_of[rec] = norm(rec.city)

    for rec in ix.records:
        for label, product, city, district, state, plain in variations(rec):
            r = lookup(ix, Lookup(product=product, city=city, district=district, state=state))
            status = r["status"]
            stats[status] += 1
            by_label[label][status] += 1
            got = ({r.get("contact")} | {m["contact"] for m in r.get("matches", [])}) - {None}
            if not got:
                if plain and status not in ("need_state", "too_many", "confirm") and \
                        not (label == "other product"):
                    missed.append((label, product, city, district, state, status))
                continue
            # Safety: the numbers must be allowed for the product actually
            # asked and the state actually said (or implied in the city text).
            prod = A.WATER_TANK if product in TANK_PRODUCTS else (
                A.MOULDING if product in MOULD_PRODUCTS else product)
            said_state = _state_for_oracle(state) or (
                _state_for_oracle(district) if district == rec.state else None) or (
                norm(rec.state) if city and rec.state.lower() in city.lower() else None)
            ok = set()
            for k in rec.keys | ({norm(district)} if district else set()):
                ok |= oracle(ix, prod, k, said_state)
            if not got <= ok:
                unsafe.append((label, product, city, district, state, got - ok, r["status"]))

    total = sum(stats.values())
    print(f"\n{total} requests over {len(ix.records)} records: {dict(stats)}")
    for label, c in sorted(by_label.items()):
        print(f"  {label:18} {dict(c)}")
    assert not unsafe, unsafe[:15]
    assert not missed, missed[:25]
