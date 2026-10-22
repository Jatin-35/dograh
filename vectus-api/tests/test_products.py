"""POST /products: Vectus bath-ware, household and garden items (data/products.json).

Covers the questions the Vectus tester asked on live calls, every product and
model name in the data (each must find itself), Hindi and misheard spellings,
tap items across series, the statuses the bot branches on, and the endpoint.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("VECTUS_API_KEY", "test-key")

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app import products as P  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data" / "products.json"
H = {"X-API-Key": "test-key"}
PRICE = re.compile(r"₹|\brs\.?\s*\d|\brupee|\bmrp\b|\bprice\b|\bcost\b", re.I)


@pytest.fixture(scope="module")
def cat():
    return P.load_catalog(DATA)


def ask(cat, **q):
    r = P.find(cat, **q)
    assert r["message"] and not PRICE.search(r["message"]), r
    return r


# ── The data ──────────────────────────────────────────────────────────


def test_the_data_has_no_prices_at_all():
    assert not PRICE.search(json.dumps(json.loads(DATA.read_text(encoding="utf-8"))["categories"],
                                       ensure_ascii=False))


def test_every_product_has_models_and_every_tap_series_its_items_and_warranty(cat):
    assert len(cat.products) == 29
    for p in cat.products:
        assert p.models, p.name
        for m in p.models:
            assert m.details, (p.name, m.name)
    assert all(m.items and m.warranty for m in cat.taps.models)


def test_the_three_categories(cat):
    assert set(cat.categories) == {"Bath-ware", "Household", "Maali planters and garden"}


# ── What the tester asked ─────────────────────────────────────────────


def test_toilet_seat_cover_is_sold_with_its_models(cat):
    r = ask(cat, product="toilet seat cover")
    assert r["status"] == "product" and r["product"] == "Toilet seat cover"
    assert r["models"] == ["Silk", "Smart", "Pro without jet", "Pro with jet", "Hush"]
    assert "24 months" in r["message"]


def test_cistern_is_sold(cat):
    r = ask(cat, product="cistern")
    assert r["status"] == "product" and "Chic" in r["models"]


@pytest.mark.parametrize("said", ["ppr pipe", "CPVC pipe", "upvc", "plumbing pipes", "cpvc fitting",
                                  "pipe fittings", "tee joint", "GI pipe"])
def test_plumbing_supply_pipes_are_not_sold(cat, said):
    assert ask(cat, product=said)["status"] == "not_sold"


def test_garden_and_washing_machine_pipes_are_sold(cat):
    assert ask(cat, product="garden pipe")["product"] == "Garden pipe"
    r = ask(cat, product="washing machine pipe")
    assert r["status"] == "found" and r["model"] == "Washing machine pipe"


def test_a_bare_pipe_asks_which(cat):
    r = ask(cat, product="pipe chahiye")
    assert r["status"] == "clarify" and "Garden pipe" in r["products"]


def test_bathware_and_household_list_their_products(cat):
    r = ask(cat, product="bathware")
    assert r["status"] == "category" and "Toilet seat cover" in r["products"]
    r = ask(cat, category="household items")
    assert r["status"] == "category" and "Bucket" in r["products"]


# ── Every name in the data finds itself ───────────────────────────────


def _all(cat):
    for p in cat.products:
        for m in p.models:
            yield p, m


def test_every_product_alias_finds_its_product(cat):
    for p in cat.products:
        for alias in p.aliases:
            r = ask(cat, product=alias)
            if r["status"] in ("product", "found"):
                assert r["product"] == p.name or r.get("item"), (alias, r)
            else:
                # a product alias that is also a model name elsewhere ("seat")
                assert r["status"] == "ambiguous", (alias, r)


def test_every_model_with_its_product_is_found(cat):
    for p, m in _all(cat):
        r = ask(cat, product=p.name, model=m.name)
        assert r["status"] == "found", (p.name, m.name, r)
        assert r["product"] == p.name and r["model"] == m.name, (p.name, m.name, r)
        assert m.details in r["message"] or m.details[1:] in r["message"]
        warranty = m.warranty or p.warranty
        if warranty:
            assert r["warranty"] == warranty and warranty in r["message"]


def test_every_model_said_inside_the_product_text_is_found(cat):
    # "seat cover hush", "balti ganga": the bot often sends it all as product.
    for p, m in _all(cat):
        if P._key(m.name) == P._key(p.name):
            continue  # "mug" lists every mug; the model field reaches this one
        models = {P._key(a) for x in p.models for a in x.aliases}
        word = next((a for a in p.aliases[1:] if P._key(a) not in models), p.name)
        r = ask(cat, product=f"{word} {m.name}")
        assert r["status"] == "found" and r["model"] == m.name, (p.name, m.name, r)


# ── Spellings: Hindi, Hinglish, misheard ──────────────────────────────


@pytest.mark.parametrize("said,product", [
    ("flush tank", "Cistern tank"), ("system tank", "Cistern tank"), ("सिस्टम टैंक", "Cistern tank"),
    ("commode cover", "Toilet seat cover"), ("सीट कवर", "Toilet seat cover"),
    ("tonti", "PTMT taps"), ("नल", "PTMT taps"), ("टोंटी", "PTMT taps"), ("faucets", "PTMT taps"),
    ("balti", "Bucket"), ("बाल्टी", "Bucket"), ("doodh ki balti", "Milk bucket"),
    ("gamla", "Planter"), ("गमला", "Planter"), ("jhari", "Watering can"),
    ("kooda daan", "Dustbin"), ("patla", "Stool"), ("muslim shower", "Health faucet"),
    ("sistern", "Cistern tank"), ("helth fasset", "Health faucet"), ("toilet seet cover", "Toilet seat cover"),
    ("Vectus ka seat cover milta hai kya", "Toilet seat cover"),
])
def test_spoken_names(cat, said, product):
    r = ask(cat, product=said)
    assert r["status"] in ("product", "found") and r["product"] == product, r


def test_soft_close_seat_cover_is_hush(cat):
    assert ask(cat, product="seat cover soft close")["model"] == "Hush"


def test_a_name_only_in_the_details(cat):
    assert ask(cat, product="ujjwal dustbin")["model"] == "Pedal bin"
    assert ask(cat, product="sudhakar tasla")["model"] == "Agri plastic taslas"
    assert ask(cat, product="maria gamla")["model"] == "Europa range"


def test_a_model_said_alone(cat):
    assert ask(cat, product="Kaveri")["product"] == "Bucket"
    assert ask(cat, product="ball cock")["model"] == "PTMT ball cock"
    assert ask(cat, product="Skymist")["product"] == "Shower"


def test_a_name_several_products_share_is_asked_back(cat):
    r = ask(cat, product="silk")
    assert r["status"] == "ambiguous"
    assert {"Toilet seat cover", "Cistern tank", "PTMT taps"} <= {o["product"] for o in r["options"]}


# ── Taps ──────────────────────────────────────────────────────────────


def test_a_tap_item_lists_the_series_that_make_it(cat):
    r = ask(cat, product="angle cock")
    assert r["status"] == "found" and r["item"] == "angle cock"
    names = [s["model"] for s in r["series_with_item"]]
    assert "Chic Dual" in names and "Sony" not in names
    assert "36 months for Chic Dual" in r["message"] and "12 months for Eco" in r["message"]


@pytest.mark.parametrize("said", ["two way bib cock", "2-way bib cock", "do way bib cock"])
def test_two_way_spellings(cat, said):
    assert ask(cat, product=said)["item"] == "two way bib cock"


def test_a_series_with_the_item(cat):
    r = ask(cat, product="chic dual angle cock")
    assert r["status"] == "found" and r["model"] == "Chic Dual" and r["warranty"] == "36 months"
    assert "has an angle cock" in r["message"]


def test_a_series_without_the_item(cat):
    r = ask(cat, product="two way bib cock", model="casa")
    assert r["status"] == "item_not_in_model" and r["model"] == "Casa"
    assert "Casa" not in [s["model"] for s in r["series_with_item"]]


def test_item_in_its_own_field(cat):
    r = ask(cat, product="tap", model="pro", item="stop cock")
    assert r["status"] == "found" and r["model"] == "Pro"


def test_long_body_bib_cock_is_long_bib_cock(cat):
    r = ask(cat, product="long body bib cock")
    assert r["item"] == "long bib cock" and len(r["series_with_item"]) >= 10


def test_wall_mixer_covers_both_kinds(cat):
    r = ask(cat, product="wall mixer")
    assert r["status"] == "found" and "Pro" in [s["model"] for s in r["series_with_item"]]


def test_sink_cock_is_a_tap_not_the_kitchen_sink(cat):
    assert ask(cat, product="sink cock")["product"] == "PTMT taps"
    assert ask(cat, product="kitchen sink")["product"] == "Kitchen sink"


def test_a_series_alone_lists_its_items(cat):
    r = ask(cat, product="tap", model="rhino")
    assert r["status"] == "found" and r["warranty"] == "24 months" and "swan neck" in r["items"]


# ── Other statuses ────────────────────────────────────────────────────


def test_nothing_asked_gives_the_catalogue(cat):
    r = ask(cat)
    assert r["status"] == "catalogue" and len(r["categories"]) == 3


@pytest.mark.parametrize("said", ["water tank", "ganga tank", "1000 litre tanki", "टंकी"])
def test_tank_questions_go_to_the_tank_lookup(cat, said):
    assert ask(cat, product=said)["status"] == "use_tank_lookup"


def test_tank_filter_and_flush_tank_stay_here(cat):
    assert ask(cat, product="tank filter")["product"] == "Tank filter"
    assert ask(cat, product="flush tank")["product"] == "Cistern tank"


@pytest.mark.parametrize("said", ["sofa", "geyser", "water purifier", "xyzzy"])
def test_unknown_items_never_say_yes_or_no(cat, said):
    r = ask(cat, product=said)
    assert r["status"] == "no_match" and "Area Manager will confirm" in r["message"]


def test_an_unknown_model_of_a_sold_product(cat):
    r = ask(cat, product="seat cover", model="Royal")
    assert r["status"] == "model_not_found" and "Hush" in r["models"]


def test_a_number_is_accepted(cat):
    assert ask(cat, product="bucket", model=16)["status"] in ("found", "model_not_found", "product")


# ── The endpoint ──────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def client():
    main.API_KEY = "test-key"
    with TestClient(main.app) as c:
        yield c


def test_health_counts_products(client):
    body = client.get("/health").json()
    assert body["products"] == 29 and body["products_error"] is None


@pytest.mark.parametrize("path", ["/products", "/products/"])
def test_post(client, path):
    r = client.post(path, json={"product": "toilet seat cover", "model": "hush"}, headers=H)
    assert r.status_code == 200 and r.json()["status"] == "found"


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}])
def test_auth_required(client, headers):
    assert client.post("/products", json={"product": "tap"}, headers=headers).status_code == 401


def test_blank_fields_from_the_llm(client):
    r = client.post("/products", json={"product": "cistern", "model": "", "item": "null",
                                       "category": None}, headers=H)
    assert r.json()["status"] == "product"


def test_a_list_is_refused(client):
    assert client.post("/products", json={"product": ["tap"]}, headers=H).status_code == 422


def test_a_bad_file_disables_only_products(client, tmp_path, monkeypatch):
    bad = tmp_path / "products.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(main, "PRODUCTS_PATH", str(bad))
    with TestClient(main.app) as c:
        assert c.get("/health").json()["products_error"]
        r = c.post("/products", json={"product": "tap"}, headers=H)
        assert r.json()["status"] == "unavailable"
        assert c.post("/lookup", json={"product": "tank", "city": "Noida"}, headers=H).status_code == 200
