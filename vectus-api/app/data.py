"""Load the Vectus KB file into an in-memory index.

The KB text file stays the source of truth, in the format Vectus maintains it.
Loading parses it, validates every record, cleans the fields that carry markers
(``Noida -M``, ``Buxar Bihar``, ``Madurai-V``), and builds the lookup keys.
Anything malformed stops startup: serving a wrong phone number is worse than
not serving at all.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import aliases as A
from .text import fold, norm

log = logging.getLogger("vectus.data")

_FIELDS = ("City", "District", "State", "VectusState", "Product", "SalesPerson",
           "ContactNumber")
_KNOWN_PRODUCTS = {"water tank": A.WATER_TANK, "moundling": A.MOULDING}

# Suffixes the KB appends to City/District to keep otherwise-equal names apart.
# The information they carry is already in the Product or State field.
_MOULDING_MARK = re.compile(r"\s*-\s*m$", re.I)
_TERRITORY_MARK = re.compile(r"-(v|g)$", re.I)  # Madurai-V / Mysore-G
_STATE_TAG = re.compile(r"\s+(bihar|up|cg|hp|mh|rj)$", re.I)
_OTHER_TAG = re.compile(r"\s+gpp$", re.I)

# Parts that must never become a key on their own after splitting a name.
_TOO_GENERIC = {"east", "west", "north", "south", "central", "nagar", "city",
                "rural", "urban", "lead", "and"}


class KBError(ValueError):
    """The KB or the alias tables are inconsistent; the service must not start."""


@dataclass(frozen=True)
class Record:
    city: str            # display form, markers removed
    district: str        # display form, markers removed
    state: str           # geographical state, as in the KB
    vectus_state: str    # Vectus's internal territory; never used for matching
    product: str         # "Water tank" | "Moundling"
    salesperson: str     # one canonical spelling per contact number
    contact: str
    keys: frozenset[str] = field(compare=False)
    states: frozenset[str] = field(compare=False)  # normalised, with corrections


def _clean_place(raw: str) -> str:
    s = raw.strip()
    s = _MOULDING_MARK.sub("", s)
    s = _TERRITORY_MARK.sub("", s)
    s = _STATE_TAG.sub("", s)
    s = _OTHER_TAG.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def _place_keys(raw: str) -> set[str]:
    """Every normalised form a place can be looked up by.

    The whole cleaned name, plus the parts of names that list two places:
    "Sas Nagar (Mohali)", "Ambedkar/Sidharth Nagar", "Dharwad-Hubli".
    """
    cleaned = _clean_place(raw)
    keys = {norm(cleaned)}
    for part in re.split(r"[/()\-]", cleaned):
        k = norm(part)
        if len(k) >= 3 and k not in _TOO_GENERIC:
            keys.add(k)
    return {k for k in keys if k}


def _parse(text: str) -> list[dict[str, str]]:
    blocks = [b for b in re.split(r"\n\s*\n", text) if "Record:" in b]
    rows = []
    for block in blocks:
        row: dict[str, str] = {}
        for line in block.strip().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                row[k.strip()] = re.sub(r"\s+", " ", v).strip()
        rows.append(row)
    return rows


def _is_lead_type(row: dict[str, str]) -> bool:
    """Routing rows that are lead categories, not places ("Project Lead",
    "Maali Lead", the East UP moulding catch-all). Excluded from place lookup."""
    return norm(row["State"]) == "india" or norm(row["City"]).startswith("east up")


def _canonical_names(rows: list[dict[str, str]]) -> dict[str, str]:
    """One display name per contact number.

    The KB spells some people several ways ("Kousik Ghosh" / "Kaushik ghosh").
    The number is the real identity; the most common spelling wins, ties going
    to the longer (more complete) form.
    """
    by_number: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        by_number[r["ContactNumber"]][r["SalesPerson"]] += 1
    names = {}
    for number, counts in by_number.items():
        best = max(counts.items(), key=lambda kv: (kv[1], len(kv[0])))[0]
        names[number] = best.title()
        if len(counts) > 1:
            log.info("merged name spellings for one contact: %s -> %s",
                     sorted(counts), names[number])
    return names


@dataclass
class Index:
    records: list[Record]
    by_key: dict[tuple[str, str], list[Record]]     # (product, key) -> records
    fuzzy_names: dict[str, dict[str, set[str]]]      # product -> folded name -> keys
    lead_rows: int
    ambiguous_keys: dict[str, set[str]]              # key -> states it spans

    @property
    def contacts(self) -> int:
        return len({r.contact for r in self.records})


def load_index(path: str | Path) -> Index:
    text = Path(path).read_text(encoding="utf-8")
    rows = _parse(text)
    if not rows:
        raise KBError(f"no records found in {path}")

    for i, row in enumerate(rows, 1):
        missing = [f for f in _FIELDS if not row.get(f)]
        if missing:
            raise KBError(f"record #{i} is missing {missing}: {row}")
        if norm(row["Product"]) not in _KNOWN_PRODUCTS:
            raise KBError(f"record #{i} has unknown product {row['Product']!r}")
        if not re.fullmatch(r"\d{10}", row["ContactNumber"]):
            raise KBError(f"record #{i} has a malformed number {row['ContactNumber']!r}")

    place_rows = [r for r in rows if not _is_lead_type(r)]
    names = _canonical_names(rows)

    records = []
    for r in place_rows:
        keys = _place_keys(r["City"]) | _place_keys(r["District"])
        for source, extra in A.EXTRA_PLACE_KEYS.items():
            if source in keys:
                keys |= set(extra)
        states = {norm(r["State"])}
        for k in keys:
            states |= A.STATE_CORRECTIONS.get(k, set())
        records.append(Record(
            city=_clean_place(r["City"]),
            district=_clean_place(r["District"]),
            state=r["State"],
            vectus_state=r["VectusState"],
            product=_KNOWN_PRODUCTS[norm(r["Product"])],
            salesperson=names[r["ContactNumber"]],
            contact=r["ContactNumber"],
            keys=frozenset(keys),
            states=frozenset(states),
        ))

    by_key: dict[tuple[str, str], list[Record]] = defaultdict(list)
    fuzzy_names: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for rec in records:
        for k in rec.keys:
            by_key[(rec.product, k)].append(rec)
            f = fold(k)
            if f:
                fuzzy_names[rec.product][f].add(k)

    index = Index(
        records=records,
        by_key=dict(by_key),
        fuzzy_names={p: dict(v) for p, v in fuzzy_names.items()},
        lead_rows=len(rows) - len(place_rows),
        ambiguous_keys=_ambiguous_keys(by_key),
    )
    _validate_aliases(index)
    _report_near_duplicates(index)
    _report_conflicts(place_rows, names)
    log.info("loaded %d place records, %d lead-type rows excluded, %d contacts",
             len(records), index.lead_rows, index.contacts)
    return index


def _ambiguous_keys(by_key) -> dict[str, set[str]]:
    spans: dict[str, set[str]] = defaultdict(set)
    for (_product, key), recs in by_key.items():
        for r in recs:
            spans[key].add(r.state)
    return {k: s for k, s in spans.items() if len(s) > 1}


def _validate_aliases(index: Index) -> None:
    known = {key for (_p, key) in index.by_key}
    problems = []
    for alias, targets in A.PLACE_ALIASES.items():
        a = norm(alias)
        if a in known:
            problems.append(f"alias {alias!r} is already a KB place and can never fire")
        for t in targets:
            key, _, state = t.partition("@")
            if norm(key) not in known:
                problems.append(f"alias {alias!r} -> {t!r}: no such place in the KB")
            elif state and not any(
                norm(state) in r.states
                for (_p, k), recs in index.by_key.items() if k == norm(key)
                for r in recs
            ):
                problems.append(f"alias {alias!r} -> {t!r}: place not in that state")
    for source in A.EXTRA_PLACE_KEYS:
        if norm(source) not in known:
            problems.append(f"EXTRA_PLACE_KEYS source {source!r}: no such place")
    if problems:
        raise KBError("alias tables do not match the KB:\n  " + "\n  ".join(problems))


def _report_conflicts(rows: list[dict[str, str]], names: dict[str, str]) -> None:
    """Warn when one place + product has several reps without a territory mark.

    Real splits are marked in the KB ("Madurai-V" / "Madurai-G"). An unmarked
    second row is most likely an edit mistake, and it would be served silently
    as "multiple" — so it is logged loudly on every start until resolved.
    """
    groups: dict[tuple, set[str]] = defaultdict(set)
    marked: set[tuple] = set()
    for r in rows:
        k = (norm(_clean_place(r["City"])), norm(_clean_place(r["District"])),
             norm(r["State"]), norm(r["Product"]))
        groups[k].add(r["ContactNumber"])
        if _TERRITORY_MARK.search(r["City"].strip()) or _TERRITORY_MARK.search(r["District"].strip()):
            marked.add(k)
    for k, numbers in groups.items():
        if len(numbers) > 1 and k not in marked:
            log.warning("CONFLICT: %s / %s (%s, %s) has %d reps with no territory mark: %s",
                        k[0], k[1], k[2], k[3], len(numbers),
                        sorted(names[n] for n in numbers))


def _report_near_duplicates(index: Index) -> None:
    """Warn about names where one is contained in another across states.

    "Lakhimpur" (UP) vs "North Lakhimpur" (Assam) is the case that motivated
    this: a caller can say the short form meaning either. Each warning should be
    resolved in EXTRA_PLACE_KEYS or consciously left alone.
    """
    states_of: dict[str, set[str]] = defaultdict(set)
    for (_p, key), recs in index.by_key.items():
        for r in recs:
            states_of[key].add(r.state)
    keys = sorted(states_of)
    for short in keys:
        for long in keys:
            if short == long or f" {short} " not in f" {long} ":
                continue
            if states_of[short] != states_of[long] and not (
                states_of[short] & states_of[long]
            ):
                log.warning("near-duplicate place names across states: %r %s vs %r %s",
                            short, sorted(states_of[short]),
                            long, sorted(states_of[long]))
