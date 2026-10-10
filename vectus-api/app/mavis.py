"""Area-manager records from Vectus's live Mavis table in LeadSquared.

Mavis holds the same table the KB file was exported from, kept current by
Vectus: one row per place, suffix-tagged by product line —

    "Noida"        water tank         -> Product "Water tank"
    "Noida-Mould"  moulding/household -> Product "Moundling"
    "Agra-DB"      dealer rows        -> not used yet (skipped)
    "Madurai-V/-G" territory split    -> kept, as in the KB

Mavis has a single ``State`` column, which for about a quarter of places is
Vectus's sales territory ("Noida" -> "Delhi", towns -> "East_UP"), not the
geographic state callers say. So Mavis supplies who to call (salesperson,
number, district) and the KB file supplies where a place is: a Mavis row that
matches a KB place takes the KB's geographic state; a place new to Mavis keeps
Mavis's state until the KB learns it.
"""

from __future__ import annotations

import logging
import re

import httpx

from .data import _MOULDING_MARK, KBError, read_kb_rows
from .text import norm

log = logging.getLogger("vectus.mavis")

_DEALER_MARK = re.compile(r"\s*-\s*db$", re.I)
PAGE_SIZE = 500
MAX_PAGES = 20
# A response this much smaller than the KB is a partial or broken reply, not a
# real change; refreshing from it would silently drop salespeople.
MIN_SHARE_OF_KB = 0.7

FIELDS = ["SalesPerson", "ContactNumber", "City", "District", "State"]


class MavisError(RuntimeError):
    """Mavis could not be read, or returned something we refuse to serve."""


def fetch_rows(url: str, api_key: str, timeout: float = 30.0) -> list[dict]:
    """Every row of the Mavis table, paging until the reported count."""
    rows: list[dict] = []
    total = None
    headers = {"x-api-key": api_key, "Content-Type": "application/json"}
    with httpx.Client(timeout=timeout) as client:
        for page in range(1, MAX_PAGES + 1):
            body = {"Select": FIELDS, "Paging": {"PageIndex": page, "PageSize": PAGE_SIZE}}
            try:
                r = client.post(url, headers=headers, json=body)
                r.raise_for_status()
                data = r.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise MavisError(f"page {page}: {type(exc).__name__}: {exc}") from exc
            batch = data.get("Data") or []
            total = data.get("Count", total)
            rows.extend(batch)
            if not batch or len(batch) < PAGE_SIZE or (total and len(rows) >= total):
                break
    if total is not None and len(rows) < total:
        raise MavisError(f"got {len(rows)} of {total} rows")
    return rows


def _phone(raw) -> str:
    digits = re.sub(r"\D", "", str(raw or ""))
    return digits[-10:] if len(digits) >= 10 else ""


def _place_key(city: str) -> str:
    """Product-free place identity, keeping territory marks (Madurai-V)."""
    return norm(_MOULDING_MARK.sub("", city.strip()))


def to_kb_rows(mavis_rows: list[dict], kb_rows: list[dict]) -> list[dict]:
    """Mavis rows in the KB's record shape, with geography from the KB."""
    geo: dict[tuple[str, str], list[dict]] = {}
    for r in kb_rows:
        product = "Moundling" if norm(r.get("Product", "")) == "moundling" else "Water tank"
        geo.setdefault((_place_key(r["City"]), product), []).append(r)

    out, skipped = [], {"dealer": 0, "incomplete": 0}
    new_places = []
    for m in mavis_rows:
        city = str(m.get("City") or "").strip()
        if not city:
            skipped["incomplete"] += 1
            continue
        if _DEALER_MARK.search(city):
            skipped["dealer"] += 1
            continue
        phone = _phone(m.get("ContactNumber"))
        person = str(m.get("SalesPerson") or "").strip()
        district = str(m.get("District") or "").strip() or city
        state = str(m.get("State") or "").strip()
        if not (phone and person and state):
            skipped["incomplete"] += 1
            continue
        product = "Moundling" if _MOULDING_MARK.search(city) else "Water tank"
        matches = geo.get((_place_key(city), product), [])
        # A name in two states (Aurangabad MH/Bihar): the KB row whose state or
        # territory is the one Mavis gives.
        kb = next((k for k in matches if norm(state) in (norm(k["State"]), norm(k["VectusState"]))),
                  matches[0] if len(matches) == 1 else None)
        if kb is None:
            new_places.append(f"{city} ({state})")
        out.append({
            "City": city,
            "District": district,
            "State": kb["State"] if kb else state,
            "VectusState": state,
            "Product": product,
            "SalesPerson": person,
            "ContactNumber": phone,
        })
    if new_places:
        log.info("Mavis places not in the KB (using Mavis's state): %s", new_places)
    if any(skipped.values()):
        log.info("Mavis rows skipped: %s", skipped)
    return out


def load_rows(url: str, api_key: str, kb_path: str) -> list[dict]:
    """Mavis records ready for ``build_index``; raises MavisError if unusable."""
    kb_rows = read_kb_rows(kb_path)
    rows = to_kb_rows(fetch_rows(url, api_key), kb_rows)
    floor = int(len(kb_rows) * MIN_SHARE_OF_KB)
    if len(rows) < floor:
        raise MavisError(f"only {len(rows)} usable rows (expected at least {floor})")
    return rows


__all__ = ["MavisError", "KBError", "fetch_rows", "load_rows", "to_kb_rows"]
