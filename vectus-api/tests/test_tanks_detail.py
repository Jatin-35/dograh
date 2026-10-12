"""detail = summary | full: a single-tank answer carries layer names and
features only when they are asked for.

Run 849: "tell me about Cool / Puff / Granito" each brought back the full card
(~1.2k tokens), the bot spoke only the summary line, and Gemini re-read the
cards on every later turn: ~17% of the call's input. The summary must still
hold every fact the bot may say, and comparisons must always come in full.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app import tanks as T

SHEET = Path(__file__).resolve().parent.parent / "data" / "Tank_details.xlsx"


@pytest.fixture(scope="module")
def cat():
    return T.load_catalog(SHEET)


def _size(d) -> int:
    return len(json.dumps(d, ensure_ascii=False))


@pytest.mark.parametrize("value,want", [
    (None, None), ("", None), ("summary", False), ("short", False), ("no", False),
    ("full", True), ("layers", True), ("features", True), ("Full details", True),
    ("xyz", None),
])
def test_parse_detail(value, want):
    assert T.parse_detail(value) is want


@pytest.mark.parametrize("model", ["Cool", "Puff", "Granito", "TenX", "Smart", "Ganga Pro", "Mini"])
def test_summary_keeps_every_card_fact_and_drops_layers(cat, model):
    full = T.find(cat, model=model)
    summ = T.find(cat, model=model, detail="summary")
    tank = next(t for t in cat.tanks if t.name == full["tanks"][0]["name"])
    assert T.describe(tank) in summ["message"]           # sizes, colours, layers, guarantee, mould
    assert "detail = full" in summ["message"]            # the bot knows how to get more
    assert "layer_names" not in summ["tanks"][0]
    for name in tank.layer_names:                         # layer names stay
        assert name in summ["message"]
    for feat in tank.features:                            # what a layer does goes
        assert feat not in summ["message"]
    assert summ["status"] == full["status"] and summ["brand"] == full["brand"]


@pytest.mark.parametrize("detail", [None, "full", "layers"])
def test_full_and_not_said_are_unchanged(cat, detail):
    r = T.find(cat, model="Granito", detail=detail)
    assert r["tanks"][0]["layer_names"]
    assert "Granite-Finish Layer" in r["message"]
    assert r == T.find(cat, model="Granito")


def test_capacity_yes_keeps_the_yes(cat):
    r = T.find(cat, model="Cool", capacity=1100, detail="summary")
    assert r["message"].startswith("Yes, Vectus Cool comes in 1100 litre")


def test_comparison_is_always_full(cat):
    for detail in ("summary", None):
        r = T.find(cat, model="TenX aur Cool", detail=detail)
        assert r["message"].startswith("Compare:")
        assert all(c["layer_names"] for c in r["tanks"])
        assert "Only Cool: temperature-resistant foam" in r["message"]


@pytest.mark.parametrize("kw", [{"capacity": 1100}, {"colour": "green"}, {"capacity": 1000},
                                {"model": "Cool", "capacity": 1000}, {"model": "xyz"}])
def test_lists_and_other_answers_ignore_detail(cat, kw):
    assert T.find(cat, **kw, detail="summary") == T.find(cat, **kw)


def test_run_849_detail_turns_shrink(cat):
    """The three 'tell me about X' turns of run 849, as the bot's tool sends them."""
    for model in ("Cool", "Puff", "Granito"):
        full = T.compact(T.find(cat, model=model, state="Jammu and Kashmir"))
        summ = T.compact(T.find(cat, model=model, state="Jammu and Kashmir", detail="summary"))
        assert _size(summ) < _size(full) * 0.35, (model, _size(summ), _size(full))


# --- HTTP ---------------------------------------------------------------------

os.environ.setdefault("VECTUS_API_KEY", "test-key")
H = {"X-API-Key": "test-key"}


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from app import main
    main.API_KEY = "test-key"
    with TestClient(main.app) as c:
        yield c


def test_http_summary_is_the_tool_default(client):
    r = client.post("/tanks?compact=1&summary=1", json={"model": "Granito"}, headers=H).json()
    assert "tanks" not in r and "Granite-Finish Layer" in r["message"]
    assert "luxurious granite finish" not in r["message"] and "detail = full" in r["message"]


def test_http_full_when_asked(client):
    r = client.post("/tanks?compact=1&summary=1", json={"model": "Granito", "detail": "full"},
                    headers=H).json()
    assert r["tanks"][0]["layer_names"] and "luxurious granite finish" in r["message"]


def test_http_old_url_unchanged(client):
    r = client.post("/tanks?compact=1", json={"model": "Granito"}, headers=H).json()
    assert r["tanks"][0]["layer_names"]


# --- deep sweep: summary drops only layer names and features, nothing else --------

import random  # noqa: E402
import sys  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "qa"))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _assert_summary_safe(cat, kw):
    full = T.find(cat, **kw)
    summ = T.find(cat, **kw, detail="summary")
    one = full.get("status") == "found" and len(full.get("tanks", [])) == 1 \
        and not full["message"].startswith("Compare:")
    if not one:
        assert summ == full, kw                      # everything else: byte-identical
        return
    assert summ["status"] == full["status"] and summ.get("need_not_met") == full.get("need_not_met")
    tank = next(t for t in cat.tanks if t.name == full["tanks"][0]["name"])
    assert T.describe(tank) in summ["message"], kw   # every card fact still sayable
    assert "detail = full" in summ["message"]
    if full["message"].startswith("Yes, "):
        assert summ["message"].startswith(full["message"].split(". ")[0])
    if "No tank here is listed" in full["message"]:
        assert summ["message"].startswith(full["message"].split(": ")[0])
    comp = T.compact(summ)
    assert set(comp) <= set(T._COMPACT_KEEP)        # the tool reply is the small one
    assert len(json.dumps(comp)) < 1000


import qa_tanks  # noqa: E402
from test_real_calls import CASES  # noqa: E402

_FIELDS = {"brand", "state", "model", "capacity", "colour", "layers", "guarantee", "mould", "need"}


@pytest.mark.parametrize("s", qa_tanks.SCENARIOS, ids=[s.id for s in qa_tanks.SCENARIOS])
def test_qa_scenarios_summary_safe(cat, s):
    _assert_summary_safe(cat, {("colour" if k == "color" else k): v
                               for k, v in s.body.items() if k in _FIELDS | {"color"}})


@pytest.mark.parametrize("case", CASES, ids=[f"{c[0]}:{c[1][:30]}" for c in CASES])
def test_real_calls_summary_safe(cat, case):
    _assert_summary_safe(cat, case[2])


def test_every_model_every_brand_summary_safe(cat):
    for t in cat.tanks:
        for extra in ({}, {"capacity": t.capacities[0]}, {"colour": t.colours[0]},
                      {"capacity": 1234}, {"need": "cooling"}):
            _assert_summary_safe(cat, {"brand": t.brand, "model": t.model, **extra})


def test_random_questions_summary_safe(cat):
    rnd = random.Random(849)
    pick = rnd.choice
    for _ in range(5000):
        _assert_summary_safe(cat, dict(
            brand=pick([None, "Vectus", "Ganga", "Waterwell", "all"]),
            state=pick([None, "TN", "Jammu and Kashmir", "Kerala"]),
            model=pick([None, "cool", "puff", "Pro", "Mini", "Cool aur Puff", "granito",
                        "vector school", "TenX vs Cool", "Waterwell Italian", "xyz", "chemical"]),
            capacity=pick([None, 500, 1000, 1100, 1200, 1500, 7000, "1000 ya 1500"]),
            colour=pick([None, "green", "black", "red", "white"]),
            layers=pick([None, 3, 4, 5, 6, 10]),
            need=pick([None, "cooling", "weather", "commercial"])))


@pytest.mark.parametrize("body,status", [
    ({"model": "Cool", "detail": 1}, 200), ({"model": "Cool", "detail": "FULL"}, 200),
    ({"model": "Cool", "detail": "x" * 500}, 200), ({"model": "Cool", "detail": ["full"]}, 422),
    ({"model": "Cool", "detail": None}, 200), ({"model": "Cool", "detail": ""}, 200),
])
def test_http_detail_odd_values(client, body, status):
    r = client.post("/tanks?compact=1&summary=1", json=body, headers=H)
    assert r.status_code == status
    if status == 200:
        assert r.json()["status"] == "found"
