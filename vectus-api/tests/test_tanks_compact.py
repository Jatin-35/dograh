"""Compact replies (?compact=1) must say exactly what the full reply says.

For every question — the QA scenarios, the real-call cases and thousands of
random mixes — the compact reply is checked against the full one: same status,
brand, closest sizes and message, and every fact on every data card of the full
reply (sizes, colours, layers, guarantee, layer names, features, extra tank
names) still present in what the compact reply keeps.
"""

from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path

import pytest

from app import tanks as T

HERE = Path(__file__).resolve().parent
SHEET = HERE.parent / "data" / "Tank_details.xlsx"
sys.path.insert(0, str(HERE.parent / "qa"))
sys.path.insert(0, str(HERE))


@pytest.fixture(scope="module")
def cat():
    return T.load_catalog(SHEET)


def _tank(cat, name):
    return next(t for t in cat.tanks if t.name == name)


def assert_no_fact_lost(cat, full: dict):
    comp = T.compact(full)
    for key in ("status", "brand", "message", "closest_capacities", "ignored", "heard"):
        assert comp.get(key) == full.get(key), key
    cards = list(full.get("tanks", [])) + [full[k] for k in ("same_tank",) if k in full]
    if any(c.get("layer_names") is not None for c in cards):
        # detailed answers stay whole (A/B: the LLM needs the card for these)
        assert {k: v for k, v in full.items() if k not in T._INTERNAL} == comp
        return
    said = comp["message"]
    cards += full.get("alternatives", []) + full.get("other_brands", []) +         [full[k] for k in ("tank",) if k in full]
    for card in cards:
        tank = _tank(cat, card["name"])
        if tank.key in {"group"} and full["status"] == "catalogue":
            continue
        assert T.describe(tank) in said, (card["name"], said[:300])
    for name in full.get("more", []):
        assert name in said, name
    for name in full.get("models", []) + full.get("other_models", []):
        short = name.split(" ", 1)[1] if name.split(" ", 1)[0] in T.BRANDS else name
        assert short in said, name  # replies list models by name, without the brand
    assert set(comp) <= set(T._COMPACT_KEEP)
    assert len(json.dumps(comp, ensure_ascii=False)) <= len(json.dumps(full, ensure_ascii=False))


# --- the hand-written QA scenarios ----------------------------------------------

import qa_tanks  # noqa: E402


@pytest.mark.parametrize("s", qa_tanks.SCENARIOS, ids=[s.id for s in qa_tanks.SCENARIOS])
def test_qa_scenarios_lose_nothing(cat, s):
    body = {("colour" if k == "color" else k): v for k, v in s.body.items()
            if k in {"brand", "state", "model", "capacity", "colour", "color", "layers",
                     "guarantee", "mould", "need"}}
    assert_no_fact_lost(cat, T.find(cat, **body))


# --- the real-call cases ----------------------------------------------------------

from test_real_calls import CASES  # noqa: E402


@pytest.mark.parametrize("call,said,send,check,bot_said", CASES,
                         ids=[f"{c[0]}:{c[1][:30]}" for c in CASES])
def test_real_calls_lose_nothing(cat, call, said, send, check, bot_said):
    full = T.find(cat, **send)
    assert_no_fact_lost(cat, full)
    comp = T.compact(full)
    # what the real-call test expects is still sayable from the compact reply
    for name in check.get("names", []) + ([check["first"]] if "first" in check else []):
        assert name in comp["message"], (name, comp["message"][:200])
    if "caps" in check:
        assert ", ".join(str(c) for c in check["caps"]) in comp["message"]


# --- random mixes ---------------------------------------------------------------


def test_random_questions_lose_nothing(cat):
    rnd = random.Random(60)
    pick = rnd.choice
    for _ in range(5000):
        full = T.find(
            cat,
            brand=pick([None, "Vectus", "Ganga", "Waterwell", "all"]),
            state=pick([None, "TN", "UP", "Kerala"]),
            model=pick([None, "cool", "puff", "Pro", "Mini", "Cool aur Puff", "granito",
                        "vector school", "Smart vs Safe vs TenX", "Waterwell Cool", "xyz"]),
            capacity=pick([None, 500, 1000, 1100, 1200, 3000, 7000, "1000 ya 1500"]),
            colour=pick([None, "green", "peela", "red", "white"]),
            layers=pick([None, 3, 4, 5, 7]),
            need=pick([None, "cooling", "weather", "underground"]),
        )
        assert_no_fact_lost(cat, full)


# --- HTTP -------------------------------------------------------------------------

os.environ.setdefault("VECTUS_API_KEY", "test-key")
H = {"X-API-Key": "test-key"}


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from app import main
    main.API_KEY = "test-key"
    with TestClient(main.app) as c:
        yield c


def test_http_compact_switch(client):
    body = {"model": "Cool", "capacity": 1000}
    full = client.post("/tanks", json=body, headers=H).json()
    comp = client.post("/tanks?compact=1", json=body, headers=H).json()
    assert "alternatives" in full and "alternatives" not in comp
    assert comp["message"] == full["message"] and comp["status"] == "not_available"
    assert len(json.dumps(comp)) < len(json.dumps(full)) * 0.6


@pytest.mark.parametrize("flag", ["0", "false", ""])
def test_http_compact_off_values(client, flag):
    r = client.post(f"/tanks?compact={flag}", json={"capacity": 1100}, headers=H).json()
    assert "tanks" in r


def test_detailed_answers_keep_their_cards(cat):
    """A/B with the agent's LLM: asked which layers Granito has, it named them
    8/8 from the full reply, 1/8 once the card was removed. Detailed answers keep it."""
    for kw in ({"model": "Granito"}, {"model": "Cool", "capacity": 1100},
               {"model": "Pro"}, {"model": "Cool aur Puff"}):
        comp = T.compact(T.find(cat, **kw))
        cards = comp.get("tanks") or [comp["same_tank"]]
        assert all(c["layer_names"] for c in cards), kw
