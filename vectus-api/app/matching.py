"""Resolve a lookup request to sales contacts.

Order, stopping at the first step that finds something:
  1. exact     — the caller's words are a KB city or district
  2. filler    — the same, after dropping "district", "sector 62", …
  3. alias     — a curated variant (Gurugram -> Gurgaon, नोएडा -> Noida)
  4. fuzzy     — a sound-alike, returned only as a *suggestion to confirm*

A phone number is only ever returned from steps 1-3. The KB's own rule is
"never infer, substitute, or guess"; a fuzzy match is a guess, so it becomes a
question for the caller instead.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass

from rapidfuzz import fuzz, process

from . import aliases as A
from .data import Index, Record
from .text import HINDI_FILLER, fold, norm, spoken_digits, strip_filler, translit

MAX_CONTACTS = 5          # more than this and the bot asks the caller to narrow down
# Sound-alike suggestions. Measured on real misspellings vs junk: no threshold
# separates them ("Nodia"->Noida and "Dubai"->Dhubri both score 80). A missed
# city loses the lead; a wrong suggestion costs one "no". So the threshold
# favours recall, and two cheap guards drop most of the junk: short words
# ("goa"->Goda, "kuch"->Kutch) and a different first sound.
FUZZY_MIN_SCORE = 80
FUZZY_MIN_LEN = 5
FUZZY_BAND = 7            # suggest everything within this many points of the best
FUZZY_MAX_SUGGESTIONS = 3


@dataclass
class Lookup:
    product: str
    city: str | None = None
    district: str | None = None
    state: str | None = None


# The tables are written by hand; normalise their keys exactly as input is
# normalised, or a key with a nukta (ग़ाज़ियाबाद) never matches its own spelling.
_PRODUCTS = {norm(k): v for k, v in A.PRODUCT_ALIASES.items()}
_PLACES = {norm(k): v for k, v in A.PLACE_ALIASES.items()}
_STATE_NAMES = {norm(k): norm(v) for k, v in A.STATE_ALIASES.items()}
_STATE_NAMES.update({s: s for s in A.ALL_STATES})
# Longest first, so "west bengal" is tried before "bengal".
_STATE_SUFFIXES = sorted(_STATE_NAMES, key=len, reverse=True)
# By sound, for transliterated or misspelt states. Short forms ("up", "ka") are
# left out: as sounds they are too easy to hit by accident.
_FOLDED_STATES = {fold(k): v for k, v in _STATE_NAMES.items() if len(fold(k)) >= 4}


# ---------------------------------------------------------------------------
# Field canonicalisation
# ---------------------------------------------------------------------------


def canonical_product(raw: str) -> str | None:
    p = norm(raw)
    if p in _PRODUCTS:
        return _PRODUCTS[p]
    if "tank" in p or "टंकी" in p or "टैंक" in p:
        return A.WATER_TANK
    if any(stem in p for stem in ("mould", "mold", "moundl", "मोल्ड")):
        return A.MOULDING
    # "Vectus ka cooler", "granito wali": a model name among other words.
    words = [w for w in p.split() if w not in {"vectus", "ka", "ki", "ke", "wala", "wali", "wale"}]
    for w in [" ".join(words), *words]:
        if w in _PRODUCTS:
            return _PRODUCTS[w]
    return None


def _exact_state(s: str, by_sound: bool = False) -> str | None:
    """A state name or curated alias. ``by_sound`` also matches folded forms —
    only for transliterated text: on typed text "jamu" (a Jamui typo) would
    sound like Jammu and hand out J&K's numbers."""
    s = re.sub(r"\b(state|rajya|pradesh ka)\b", " ", s).strip() or s
    s = re.sub(r"\s+", " ", s)
    hit = _STATE_NAMES.get(s) or _STATE_NAMES.get(s.replace(" ", ""))
    if not hit and by_sound and len(fold(s)) >= 4:
        hit = _FOLDED_STATES.get(fold(s))
    return hit


def canonical_state(raw: str | None) -> tuple[str | None, bool]:
    """A caller's state -> (normalised state, said exactly?).

    A corrected state ("utar pradesh", or one heard in Hindi script) is fine
    for narrowing a place, but is not exact enough to hand out numbers for the
    whole state on its own — the caller confirms it first.
    """
    s = norm(raw)
    if not s:
        return None, True
    if hit := _exact_state(s):
        return hit, True
    if not s.isascii():
        s = translit(s)
        if hit := _exact_state(s, by_sound=True):
            return hit, False
    hit = process.extractOne(s, A.ALL_STATES, scorer=fuzz.ratio, score_cutoff=85)
    if hit:
        return hit[0], False
    # "south bihar", "up bundelkhand", "bihar south": a state inside a longer
    # regional phrase. Exact words only, and only when they agree.
    words = s.split()
    found = {_STATE_NAMES[" ".join(words[i:i + n])]
             for n in (3, 2, 1) for i in range(len(words) - n + 1)
             if " ".join(words[i:i + n]) in _STATE_NAMES}
    return (found.pop(), True) if len(found) == 1 else (None, True)


def _split_state(term: str, by_sound: bool = False,
                 is_place=lambda _t: False) -> tuple[str, str | None]:
    """ "noida up" -> ("noida", "uttar pradesh"): a state said inside the city.

    Literal suffixes; with ``by_sound``, also sound-alike ones for
    transliterated text ("jammoo kashmeer" -> Jammu and Kashmir).
    """
    # Anywhere in the phrase, not only at the end: the bot's own queries read
    # "Area Manager Jammu and Kashmir samba".
    # Some names are both ("Pondichery" is a KB place and a state alias), so
    # among the possible splits prefer one that leaves a real place behind.
    if term in _STATE_NAMES:
        return "", _STATE_NAMES[term]
    padded = f" {term} "
    splits: list[tuple[str, str]] = []
    for phrase in _STATE_SUFFIXES:
        if f" {phrase} " in padded:
            head = re.sub(r"\s+", " ", padded.replace(f" {phrase} ", " ", 1)).strip()
            if head:
                splits.append((head, _STATE_NAMES[phrase]))
    if by_sound:
        words = term.split()
        for n in range(min(4, len(words) - 1), 0, -1):
            for i in range(len(words) - n + 1):
                hit = _FOLDED_STATES.get(fold(" ".join(words[i:i + n])))
                if hit:
                    splits.append((" ".join(words[:i] + words[i + n:]), hit))
    for head, st in splits:
        if is_place(head):
            return head, st
    return splits[0] if splits else (term, None)


def _allowed_states(state: str | None) -> set[str] | None:
    return A.STATE_REGIONS.get(state, {state}) if state else None


# The old KB wrote moulding rows as "Agra - M" and split territories as
# "Madurai-V"; an LLM that saw those passes them through.
_MARKERS = re.compile(r"(\s*-\s*[mvg]|\s+gpp)\s*$", re.I)


def _drop_hindi_filler(term: str) -> str:
    return " ".join(w for w in term.split() if w not in HINDI_FILLER)


def _sub_phrases(index: Index, term: str, product: str) -> list[Record]:
    """ "noida gaur" -> Noida: the longest run of words that is a place.

    Used only when the whole term matched nothing, and only ever as a
    suggestion — the extra words might have meant something else.
    """
    words = strip_filler(term).split()
    for n in range(len(words) - 1, 0, -1):
        found: list[Record] = []
        for i in range(len(words) - n + 1):
            found += _resolve_term(index, " ".join(words[i:i + n]), product)[0]
        if found:
            return found
    return []


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _resolve_term(index: Index, term: str, product: str) -> tuple[list[Record], str]:
    """Records for one place term, and how they were matched ("" if not)."""
    t = norm(term)
    if (product, t) in index.by_key:
        return list(index.by_key[(product, t)]), "exact"

    stripped = strip_filler(t)
    if stripped and stripped != t and (product, stripped) in index.by_key:
        return list(index.by_key[(product, stripped)]), "exact"

    for candidate in (t, stripped):
        targets = _PLACES.get(candidate)
        if not targets:
            continue
        found: list[Record] = []
        for target in targets:
            key, _, state = target.partition("@")
            for rec in index.by_key.get((product, norm(key)), []):
                if not state or norm(state) in rec.states:
                    found.append(rec)
        if found:
            return found, "alias"
    return [], ""


def _fuzzy(index: Index, terms: list[str], product: str,
           allowed: set[str] | None) -> list[Record]:
    """Sound-alike places for terms that matched nothing — suggestions only."""
    names = index.fuzzy_names.get(product, {})
    if not names:
        return []
    best: dict[str, float] = {}
    # The whole term, then — for a sentence — each word and each pair of
    # words: "katuaa lie chek keejie" -> "katuaa"; "kat huaa" (speech-to-text
    # splitting कठुआ) -> the pair.
    pieces: list[str] = []
    for term in terms:
        words = strip_filler(norm(term)).split()
        pieces.append(" ".join(words))
        if len(words) > 1:
            pieces += words + [" ".join(words[i:i + 2]) for i in range(len(words) - 1)]
    for term in pieces:
        said = term
        f = fold(said)
        # Length as said, not as folded: "Diphuu" folds to 4 letters but is a
        # real attempt; "goa", "sir", "haan" are short as said.
        if len(said.replace(" ", "")) < FUZZY_MIN_LEN or not f:
            continue
        for folded, score, _ in process.extract(
            f, names.keys(), scorer=fuzz.ratio, limit=10, score_cutoff=FUZZY_MIN_SCORE
        ):
            if folded[0] != f[0]:
                continue
            for key in names[folded]:
                best[key] = max(best.get(key, 0), score)
    if not best:
        return []
    top = max(best.values())
    keys = [k for k, s in sorted(best.items(), key=lambda kv: -kv[1])
            if s >= top - FUZZY_BAND]

    out: list[Record] = []
    for key in keys:
        for rec in index.by_key.get((product, key), []):
            if allowed and not rec.states & allowed:
                continue
            out.append(rec)
    return _unique_places(out)[:FUZZY_MAX_SUGGESTIONS]


def _unique_places(records: list[Record]) -> list[Record]:
    # The contact is part of the identity: Madurai-V and Madurai-G clean to the
    # same place but are two reps' territories.
    seen, out = set(), []
    for r in records:
        k = (r.city, r.district, r.state, r.contact)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def _states_of(records: list[Record]) -> list[str]:
    return sorted({r.state for r in records})


def lookup(index: Index, req: Lookup) -> dict:
    product = canonical_product(req.product)
    if product is None:
        return {
            "status": "invalid_product",
            "allowed": [A.WATER_TANK, A.MOULDING],
            "message": "Unrecognised product. Ask the caller whether they need a "
                       "water tank or moulding.",
        }

    state, state_exact = canonical_state(req.state)

    # Callers (and LLMs filling the tool call) put states in the city field:
    # "Noida, UP", "सांबा जम्मू कश्मीर", or just "Kerala". A term that matches a
    # place as a whole is always a place; only one that does not is checked for
    # a state, and only a Hindi-script one that still does not is transliterated.
    terms: list[str] = []
    transliterated = False
    for raw in (req.city, req.district):
        t = norm(_MARKERS.sub("", raw.strip())) if raw else ""
        if not t or _resolve_term(index, t, product)[0]:
            if t:
                terms.append(t)
            continue
        t = _drop_hindi_filler(t)
        for attempt in (1, 2):
            head, said_state = _split_state(
                t, by_sound=transliterated,
                is_place=lambda h: bool(_resolve_term(index, h, product)[0]))
            if said_state:
                t = head
            elif said_state := _exact_state(t, by_sound=transliterated):
                t = ""
            if said_state and not state:
                state, state_exact = said_state, not transliterated
            if attempt == 2 or not t or t.isascii() or _resolve_term(index, t, product)[0]:
                break
            # Speech-to-text wrote it in Devanagari and it is not a curated
            # alias: go by sound. Anything found this way is only suggested.
            t = translit(t)
            transliterated = True
        if t:
            terms.append(t)

    # --- state only: everything in the state, for this product ---
    if not terms:
        if not state:
            return {
                "status": "need_location",
                "message": "Ask the caller for their city or district.",
            }
        in_state = [r for r in index.records
                    if r.product == product and state in r.states]
        if not in_state:
            return _not_found(product, req.state or state.title())
        answer = _answer(in_state, product, match="state")
        if not state_exact and answer["status"] in ("found", "multiple"):
            name = next(r.state for r in in_state)
            return {
                "status": "confirm",
                "suggestions": [{"city": None, "district": None, "state": name}],
                "message": f"Confirm the state with the caller (\"{name}, sahi hai?\") "
                           "and ask for their city or district, then look up again.",
            }
        return answer

    allowed = _allowed_states(state)

    # --- city and/or district ---
    resolved = [(recs, how) for recs, how in
                (_resolve_term(index, t, product) for t in terms) if recs]

    if not resolved:
        partial = [r for t in terms for r in _sub_phrases(index, t, product)]
        suggestions = _unique_places(
            [r for r in partial if not allowed or r.states & allowed]
        )[:FUZZY_MAX_SUGGESTIONS] or _fuzzy(index, terms, product, allowed)
        if suggestions:
            return _confirm(suggestions)
        return _not_found(product, " / ".join(t.title() for t in terms))

    if len(resolved) == 2:
        # City and district both given: prefer the records both agree on.
        both = [r for r in resolved[0][0] if r in resolved[1][0]]
        if not both:
            # They disagree. If one is also a state name ("Ghaziabad" +
            # "Delhi"), the caller meant it as the state, not a second place.
            for keep, other in ((0, 1), (1, 0)):
                as_state = _exact_state(terms[other])
                if as_state and not state:
                    state, allowed = as_state, _allowed_states(as_state)
                    both = resolved[keep][0]
                    break
        candidates = both or resolved[0][0] + resolved[1][0]
    else:
        candidates = resolved[0][0]
    how = "alias" if any(h == "alias" for _, h in resolved) else "exact"

    if allowed:
        in_state = [r for r in candidates if r.states & allowed]
        if not in_state:
            return _need_state(candidates, terms[0], stated=req.state or state.title())
        candidates = in_state
    elif len(_states_of(candidates)) > 1:
        return _need_state(candidates, terms[0])

    if transliterated:
        # Heard, not read: confirm the place before giving out a number.
        return _confirm(_unique_places(candidates)[:FUZZY_MAX_SUGGESTIONS])
    return _answer(candidates, product, match=how)


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


def _place(r: Record) -> dict:
    return {"city": r.city, "district": r.district, "state": r.state}


def _contact(r: Record) -> dict:
    return {
        "salesperson": r.salesperson,
        "contact": r.contact,
        "contact_spoken": spoken_digits(r.contact),
    }


def _answer(records: list[Record], product: str, match: str) -> dict:
    """Group by person: one person covering many places is one answer."""
    groups: OrderedDict[tuple[str, str], list[Record]] = OrderedDict()
    for r in _unique_places(records):
        groups.setdefault((r.salesperson, r.contact), []).append(r)

    if len(groups) > MAX_CONTACTS:
        return {
            "status": "too_many",
            "count": len(groups),
            "message": f"{len(groups)} sales contacts match. Ask the caller for "
                       "their city or district, then look up again.",
        }

    if len(groups) == 1:
        places = next(iter(groups.values()))
        first = places[0]
        out = {"status": "found", "product": product, **_contact(first),
               "match": match,
               "message": f"Share this with the caller: {first.salesperson}, "
                          "number read digit by digit from contact_spoken."}
        if len(places) == 1:
            out.update(_place(first))
        else:
            out.update({
                "city": None, "district": None,
                "state": first.state if len(_states_of(places)) == 1 else None,
                "covers": [p.city for p in places],
            })
        return out

    return {
        "status": "multiple",
        "product": product,
        "match": match,
        "matches": [
            {**_contact(places[0]), "locations": [_place(p) for p in places]}
            for places in groups.values()
        ],
        "message": "More than one sales contact covers this place. Share each "
                   "name with its number.",
    }


def _confirm(suggestions: list[Record]) -> dict:
    first = suggestions[0]
    return {
        "status": "confirm",
        "suggestions": [_place(r) for r in suggestions],
        "message": "No exact match. Confirm the place with the caller (for example: "
                   f"\"Did you mean {first.city}, {first.state}?\"), then look up again "
                   "with the confirmed name.",
    }


def _need_state(records: list[Record], said: str, stated: str | None = None) -> dict:
    # Name the place as the caller said it: "Lakhimpur" matched "North Lakhimpur"
    # too, and echoing the longer name back would confuse them.
    options = _states_of(records)
    name = said.strip().title()
    if stated:
        message = (f"{name} is not listed under {stated}. It is listed under "
                   f"{' or '.join(options)} — ask the caller which.")
    else:
        message = (f"{name} exists in more than one state. Ask the caller: "
                   f"{' or '.join(options)}?")
    return {"status": "need_state", "place": name, "options": options,
            "message": message}


def _not_found(product: str, what: str) -> dict:
    return {
        "status": "not_found",
        # "Moundling" is the KB's spelling; say the real word to the caller.
        "message": f"No Vectus {'moulding' if product == A.MOULDING else 'water tank'} "
                   f"contact is listed for {what}. "
                   "Tell the caller our team will call them back.",
    }
