"""Mavis as the live source: merge rules, paging, guards, refresh fallback."""

import asyncio
from pathlib import Path

import httpx
import pytest

from app import main, mavis
from app.data import build_index, read_kb_rows
from app.matching import Lookup, lookup

KB = str(Path(__file__).resolve().parent.parent / "data" / "Vectus_KB_Revised.txt")
KB_ROWS = read_kb_rows(KB)

# Shapes copied from the live table (names/numbers as Mavis returned them).
MAVIS = [
    {"SalesPerson": "Sonu Kumar", "ContactNumber": "9536551180", "City": "Noida", "District": "Noida", "State": "Delhi"},
    {"SalesPerson": "Akhilesh Singh", "ContactNumber": "7503844211", "City": "Noida-Mould", "District": "Noida", "State": "Delhi"},
    {"SalesPerson": "Sagar Enterprises", "ContactNumber": "9999999999", "City": "West Delhi-DB", "District": "West Delhi", "State": "Delhi"},
    {"SalesPerson": "Senthilandavar", "ContactNumber": "+91 98765 43210", "City": "Madurai-V", "District": "Madurai", "State": "Tamilnadu"},
    {"SalesPerson": "", "ContactNumber": "9000000000", "City": "Nowhere", "District": "", "State": "X"},
    {"SalesPerson": "New Rep", "ContactNumber": "9111111111", "City": "Brandnewtown", "District": "Brandnew", "State": "Bihar"},
]


def rows():
    return {r["City"]: r for r in mavis.to_kb_rows(MAVIS, KB_ROWS)}


def test_products_come_from_the_city_suffix():
    r = rows()
    assert r["Noida"]["Product"] == "Water tank"
    assert r["Noida-Mould"]["Product"] == "Moundling"


def test_dealer_and_incomplete_rows_are_skipped():
    r = rows()
    assert "West Delhi-DB" not in r
    assert "Nowhere" not in r  # no salesperson


def test_geographic_state_comes_from_the_kb_not_the_sales_territory():
    r = rows()
    # Mavis files Noida under the "Delhi" territory; callers say Uttar Pradesh.
    assert r["Noida"]["State"] == "Uttar Pradesh"
    assert r["Noida"]["VectusState"] == "Delhi"


def test_a_place_new_to_mavis_keeps_mavis_state():
    assert rows()["Brandnewtown"]["State"] == "Bihar"


def test_numbers_are_normalised():
    assert rows()["Madurai-V"]["ContactNumber"] == "9876543210"


def test_lookup_serves_mavis_people_with_district():
    ix = build_index(mavis.to_kb_rows(MAVIS, KB_ROWS), strict_aliases=False, source="mavis")
    tank = lookup(ix, Lookup(product="Water tank", city="Noida", state="Uttar Pradesh"))
    assert tank["status"] == "found" and tank["salesperson"] == "Sonu Kumar"
    assert tank["district"] == "Noida" and tank["state"] == "Uttar Pradesh"
    mould = lookup(ix, Lookup(product="Moundling", city="Noida"))
    assert mould["status"] == "found" and mould["salesperson"] == "Akhilesh Singh"


def test_kb_moulding_mark_and_mavis_mould_mark_mean_the_same():
    from app.data import _clean_place
    assert _clean_place("Noida -M") == _clean_place("Noida-Mould") == "Noida"


class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("x", request=None, response=None)

    def json(self):
        return self._data


class _Client:
    def __init__(self, pages):
        self.pages, self.bodies = pages, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, headers, json):
        self.bodies.append(json)
        return self.pages[json["Paging"]["PageIndex"] - 1]


def test_fetch_pages_until_the_count(monkeypatch):
    page1 = _Resp({"Data": [{"City": f"c{i}"} for i in range(500)], "Count": 769})
    page2 = _Resp({"Data": [{"City": f"d{i}"} for i in range(269)], "Count": 769})
    client = _Client([page1, page2])
    monkeypatch.setattr(mavis.httpx, "Client", lambda timeout: client)
    got = mavis.fetch_rows("https://mavis", "k")
    assert len(got) == 769
    assert [b["Paging"]["PageIndex"] for b in client.bodies] == [1, 2]


def test_short_read_is_refused(monkeypatch):
    client = _Client([_Resp({"Data": [{"City": "a"}], "Count": 769})])
    monkeypatch.setattr(mavis.httpx, "Client", lambda timeout: client)
    with pytest.raises(mavis.MavisError):
        mavis.fetch_rows("https://mavis", "k")


def test_http_error_is_a_mavis_error(monkeypatch):
    client = _Client([_Resp({}, status=500)])
    monkeypatch.setattr(mavis.httpx, "Client", lambda timeout: client)
    with pytest.raises(mavis.MavisError):
        mavis.fetch_rows("https://mavis", "k")


def test_a_much_smaller_table_is_refused(monkeypatch):
    monkeypatch.setattr(mavis, "fetch_rows", lambda url, key: MAVIS)  # 4 usable rows
    with pytest.raises(mavis.MavisError):
        mavis.load_rows("https://mavis", "k", KB)


class _App:
    class state:
        pass


def test_a_failed_refresh_keeps_serving_the_current_data(monkeypatch):
    app = _App()
    kb_index = build_index(KB_ROWS)
    app.state.index, app.state.loaded_at, app.state.refresh_error = kb_index, "t0", None

    def boom():
        raise mavis.MavisError("down")

    monkeypatch.setattr(main, "_load_from_mavis", boom)
    assert asyncio.run(main.refresh_index(app)) is False
    assert app.state.index is kb_index and app.state.loaded_at == "t0"
    assert "down" in app.state.refresh_error


def test_a_good_refresh_swaps_the_index(monkeypatch):
    app = _App()
    app.state.index, app.state.loaded_at, app.state.refresh_error = build_index(KB_ROWS), "t0", "old"
    fresh = build_index(mavis.to_kb_rows(MAVIS, KB_ROWS), strict_aliases=False, source="mavis")
    monkeypatch.setattr(main, "_load_from_mavis", lambda: fresh)
    assert asyncio.run(main.refresh_index(app)) is True
    assert app.state.index is fresh and app.state.refresh_error is None


@pytest.mark.parametrize("product", ["Household", "household", "Household products", "ghar ka product"])
def test_household_goes_to_the_moulding_reps(product):
    ix = build_index(mavis.to_kb_rows(MAVIS, KB_ROWS), strict_aliases=False, source="mavis")
    for city in ("Noida", "Noida-M"):
        r = lookup(ix, Lookup(product=product, city=city))
        assert r["status"] == "found" and r["salesperson"] == "Akhilesh Singh", r
