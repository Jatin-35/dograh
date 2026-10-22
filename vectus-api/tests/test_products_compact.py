"""/products works the way /tanks does for the bot's tool.

The tool URL is /products?compact=1&summary=1, like /tanks?compact=1&summary=1.
Compact replies must say exactly what the full reply says: for every QA case
and thousands of random mixes, status and message are unchanged and every fact
the full reply carried (model names, details, warranty, items, series,
products) is still in the compact message. Also: detail = "full", strict
loading that names the bad product, and the endpoint's query parameters.
"""

from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path

import pytest

os.environ.setdefault("VECTUS_API_KEY", "test-key")

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app import products as P  # noqa: E402

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data" / "products.json"
sys.path.insert(0, str(HERE.parent / "qa"))
from qa_products import CASES  # noqa: E402

H = {"X-API-Key": "test-key"}


@pytest.fixture(scope="module")
def cat():
    return P.load_catalog(DATA)


def _facts(full: dict) -> list[str]:
    """Every speakable fact in the structured parts of a full reply."""
    out: list[str] = []
    for k in ("details", "warranty"):
        if full.get(k):
            out.append(full[k])
    out += full.get("models", []) + full.get("items", []) + full.get("products", [])
    out += [s["model"] for s in full.get("series_with_item", [])]
    out += [s["warranty"] for s in full.get("series_with_item", []) if s.get("warranty")]
    out += full.get("features", [])
    for prods in full.get("categories", {}).values():
        out += prods
    out += [o["product"] for o in full.get("options", [])]
    return out


def assert_no_fact_lost(full: dict):
    comp = P.compact(full)
    for key in ("status", "message", "product", "model", "item", "heard", "unmatched"):
        assert comp.get(key) == full.get(key), key
    said = comp["message"].lower()
    for fact in _facts(full):
        assert fact.lower() in said, (fact, comp["message"])


@pytest.mark.parametrize("req", [c[0] for c in CASES], ids=[str(c[0]) for c in CASES])
def test_compact_keeps_every_fact_for_each_caller_question(cat, req):
    assert_no_fact_lost(P.find(cat, **req))
    assert_no_fact_lost(P.find(cat, **req, detail="full"))


def test_compact_keeps_every_fact_for_random_mixes(cat):
    rng = random.Random(3)
    words = [a for p in cat.products for a in p.aliases[:3]]
    models = [m.name for p in cat.products for m in p.models]
    noise = ["kya", "chahiye", "16 litre", "white", "brass", "xyz", "tanki", "pipe"]
    for _ in range(3000):
        parts = rng.sample(words, rng.randint(0, 2)) + rng.sample(models, rng.randint(0, 2)) \
            + rng.sample(noise, rng.randint(0, 2))
        rng.shuffle(parts)
        assert_no_fact_lost(P.find(cat, product=" ".join(parts),
                                   detail=rng.choice([None, "full"])))


def test_every_model_card_survives_compaction(cat):
    for p in cat.products:
        for m in p.models:
            assert_no_fact_lost(P.find(cat, product=p.name, model=m.name))


def test_compact_is_smaller(cat):
    full = P.find(cat, product="angle cock")
    assert len(json.dumps(P.compact(full))) < len(json.dumps(full))


# ── detail = "full" ───────────────────────────────────────────────────


def test_features_only_when_asked(cat):
    plain = P.find(cat, product="cistern")
    assert "features" not in plain and "polypropylene" not in plain["message"]
    full = P.find(cat, product="cistern", detail="full")
    assert "virgin polypropylene" in full["message"]


@pytest.mark.parametrize("detail", ["full", "Full", "features", "details"])
def test_detail_spellings(cat, detail):
    assert "Features:" in P.find(cat, product="health faucet", detail=detail)["message"]


@pytest.mark.parametrize("detail", [None, "", "summary", "no", "short"])
def test_detail_off(cat, detail):
    assert "Features:" not in P.find(cat, product="health faucet", detail=detail)["message"]


def test_per_series_warranty_is_said(cat):
    msg = P.find(cat, product="angle cock")["message"]
    assert "36 months for Chic Dual" in msg and "24 months for Rhino and Presto" in msg \
        and "12 months for Eco" in msg
    sink = P.find(cat, product="kitchen sink")["message"]
    assert "5 years for Silk single bowl" in sink and "12 months for Brass" in sink


# ── Strict loading, like the tank sheet ───────────────────────────────


def _broken(tmp_path, change):
    raw = json.loads(DATA.read_text(encoding="utf-8"))
    change(raw)
    f = tmp_path / "products.json"
    f.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return f


def _first(raw):
    return raw["categories"][0]["products"][0]


@pytest.mark.parametrize("change,says", [
    (lambda r: _first(r).update(models=[]), "no models"),
    (lambda r: _first(r).update(warranty="two years"), "warranty"),
    (lambda r: _first(r)["models"][0].update(details=" "), "no name or details"),
    (lambda r: _first(r)["models"][0].update(details="8 litre, MRP 1204"), "price"),
    (lambda r: _first(r)["models"].append(dict(_first(r)["models"][0])), "appears twice"),
    (lambda r: r["categories"][0]["products"].append(dict(_first(r))), "repeated product name"),
    (lambda r: r["categories"][0]["products"][2]["models"][0].update(items=[]), "tap series"),
    (lambda r: r.pop("not_sold"), "cannot read"),
    (lambda r: r.update(categories=[]), "no products"),
])
def test_a_bad_file_is_refused_and_names_the_problem(tmp_path, change, says):
    with pytest.raises(P.ProductCatalogError, match=says):
        P.load_catalog(_broken(tmp_path, change))


def test_the_shipped_file_loads(cat):
    assert len(cat.products) == 29


# ── The endpoint, as the tool calls it ────────────────────────────────


@pytest.fixture(scope="module")
def client():
    main.API_KEY = "test-key"
    with TestClient(main.app) as c:
        yield c


def test_the_tool_url(client):
    r = client.post("/products?compact=1&summary=1", json={"product": "angle cock"}, headers=H)
    body = r.json()
    assert r.status_code == 200 and set(body) <= set(P._COMPACT_KEEP)
    assert "series_with_item" not in body and "12 months for Eco" in body["message"]


def test_without_compact_the_full_reply(client):
    body = client.post("/products", json={"product": "angle cock"}, headers=H).json()
    assert "series_with_item" in body


def test_detail_over_http(client):
    body = client.post("/products?compact=1&summary=1",
                       json={"product": "cistern", "detail": "full"}, headers=H).json()
    assert "polypropylene" in body["message"]


def test_same_shape_as_tanks(client):
    """Both answer 200 with status + message, and both refuse a bad key."""
    for path, body in (("/tanks?compact=1&summary=1", {"capacity": 1000}),
                       ("/products?compact=1&summary=1", {"product": "tap"})):
        r = client.post(path, json=body, headers=H)
        assert r.status_code == 200 and {"status", "message"} <= set(r.json())
        assert client.post(path, json=body, headers={"X-API-Key": "no"}).status_code == 401
