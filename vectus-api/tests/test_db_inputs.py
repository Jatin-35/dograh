"""Test inputs taken from the Dograh database.

1. ``db_kb_v1_records.json`` — every record recovered from the KB as it was
   uploaded for RAG (knowledge_base_documents id 1, Vectus_KB_Flat_Records.txt).
   That version wrote moulding cities as "Agra - m" and used Vectus's internal
   zones as the state ("west _ up"; "delhi" for Noida/Gurgaon). Each record is
   looked up exactly as it was written there and must return the same number.

2. ``db_call_inputs.json`` — what real callers said in the Vectus calls, and
   the queries the bot built from it, including speech-to-text noise
   ("कटुआ", "कट हुआ" for Kathua).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.data import load_index
from app.matching import Lookup, canonical_product, lookup

HERE = Path(__file__).resolve().parent
KB = HERE.parent / "data" / "Vectus_KB_Revised.txt"
V1 = json.loads((HERE / "fixtures" / "db_kb_v1_records.json").read_text(encoding="utf-8"))
CALLS = json.loads((HERE / "fixtures" / "db_call_inputs.json").read_text(encoding="utf-8"))

# Lead categories, not places — excluded from lookup by design (data.py).
EXCLUDED = {"east _ up - m", "maali lead", "project lead jhp", "project lead rcmg"}


@pytest.fixture(scope="module")
def ix():
    return load_index(KB)


def contacts(r) -> set[str]:
    return ({r.get("contact")} | {m["contact"] for m in r.get("matches", [])}) - {None}


def test_every_old_kb_record_as_written_in_the_db(ix):
    wrong, missed = [], []
    for rec in V1:
        if rec["city"] in EXCLUDED:
            continue
        r = lookup(ix, Lookup(product=rec["product"], city=rec["city"], state=rec["state"]))
        got = contacts(r)
        if got and rec["contact"] not in got:
            wrong.append((rec, r))
        elif not got:
            missed.append((rec["city"], rec["state"], r["status"]))
    assert not wrong, wrong[:5]
    assert not missed, missed[:20]
    assert len(V1) - len(EXCLUDED) == 576


def test_rag_numbers_from_the_calls_are_not_in_the_kb(ix):
    """Documents why this API exists: RAG read out numbers that do not exist."""
    kb_numbers = {r.contact for r in ix.records}
    for said in CALLS["rag_gave"]:
        if said["said_number"]:
            assert (said["said_number"] in kb_numbers) == said["in_kb"], said


@pytest.mark.parametrize("case", [c for c in CALLS["cases"] if c["field"] != "product"],
                         ids=lambda c: f"run{c['run']}:{c['said'][:40]}")
def test_what_callers_said(ix, case):
    samba = CALLS["samba_rep"]["contact"]  # Samba and Kathua share this rep
    r = lookup(ix, Lookup(product=case["product"], **{case["field"]: case["said"]}))
    s, expect = r["status"], case["expect"]
    sugg = {x["city"] for x in r.get("suggestions", [])}

    if expect == "state_answer":
        # Only the state was said: every J&K water-tank rep, per the contract.
        assert s in ("found", "multiple", "too_many") and r.get("match") in ("state", None), r
        # Ladakh is included: callers there still say J&K (STATE_CORRECTIONS).
        assert all(loc["state"] in ("Jammu and Kashmir", "Ladakh")
                   for m in r.get("matches", []) for loc in m["locations"]), r
        return

    # A place was said: never a number other than the right one.
    assert contacts(r) <= {samba}, r

    if expect == "found_samba":
        assert s == "found" and r["city"] == "Samba", r
    elif expect == "found_kathua":
        assert s == "found" and r["city"] == "Kathua", r
    elif expect == "confirm_samba":
        assert s == "confirm" and "Samba" in sugg, r
    elif expect == "confirm_kathua":
        assert s == "confirm" and "Kathua" in sugg, r
    elif expect == "confirm_kathua_or_nothing":
        assert s in ("confirm", "not_found") and (s == "not_found" or "Kathua" in sugg), r
    elif expect in ("no_number", "no_wrong_number"):
        assert s != "found" or r.get("city") in ("Samba", "Kathua"), r
    else:
        raise AssertionError(f"unknown expectation {expect}")


@pytest.mark.parametrize("case", [c for c in CALLS["cases"] if c["field"] == "product"],
                         ids=lambda c: c["said"])
def test_products_callers_named(case):
    got = canonical_product(case["said"])
    if case["expect"] == "invalid_product":
        assert got is None
    else:
        assert got == "Water tank"
