from __future__ import annotations

import os

import pytest

os.environ["VECTUS_API_KEY"] = "test-key"

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

main.API_KEY = "test-key"
H = {"X-API-Key": "test-key"}


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["records"] == 753


def test_lookup(client):
    r = client.post("/lookup", json={"product": "tank", "city": "Noida"}, headers=H)
    assert r.status_code == 200
    assert r.json()["status"] == "found"


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}])
def test_auth_required(client, headers):
    r = client.post("/lookup", json={"product": "tank", "city": "Noida"}, headers=headers)
    assert r.status_code == 401


def test_missing_product_is_a_status_not_a_422(client):
    r = client.post("/lookup", json={"city": "Noida"}, headers=H)
    assert r.status_code == 200
    assert r.json()["status"] == "invalid_product"


def test_llm_null_strings_are_treated_as_absent(client):
    r = client.post("/lookup", json={"product": "tank", "city": "Noida",
                                     "district": "", "state": "null"}, headers=H)
    assert r.json()["status"] == "found"


def test_no_docs_exposed(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
