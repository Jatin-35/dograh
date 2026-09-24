"""Generated misspellings of every KB city: the safety property, measured.

For each single-word city, apply one edit (drop / swap / double a letter,
change a vowel, drop or add an "h") and look it up. The hard rule is that a
misspelling never returns someone else's number. Recall is tracked so a change
that quietly stops suggesting real places also fails.
"""

from __future__ import annotations

import random
from collections import Counter
from pathlib import Path

from app.data import load_index
from app.matching import Lookup, lookup
from app.text import norm

KB = Path(__file__).resolve().parent.parent / "data" / "Vectus_KB_Revised.txt"
VOWELS = "aeiou"


def _mutations(word: str, rng: random.Random) -> set[str]:
    out = set()
    for i in range(1, len(word)):  # first letter kept: callers rarely mishear it
        out.add(word[:i] + word[i + 1:])
        if i < len(word) - 1:
            out.add(word[:i] + word[i + 1] + word[i] + word[i + 2:])
        out.add(word[:i] + word[i] + word[i:])
        if word[i] in VOWELS:
            out.add(word[:i] + rng.choice([v for v in VOWELS if v != word[i]]) + word[i + 1:])
    out.add(word.replace("h", "", 1))
    out.add(word.replace("k", "kh", 1).replace("g", "gh", 1))
    return {m for m in out if m != word and len(m) >= 4}


def test_misspellings_never_return_a_wrong_number():
    ix = load_index(KB)
    rng = random.Random(7)
    real_keys = {k for (_p, k) in ix.by_key}
    stats: Counter = Counter()
    wrong = []

    seen = set()
    for rec in ix.records:
        word = norm(rec.city)
        if " " in word or len(word) < 5 or (word, rec.product) in seen:
            continue
        seen.add((word, rec.product))
        for m in _mutations(word, rng):
            if m in real_keys:  # the typo is itself another real place
                continue
            r = lookup(ix, Lookup(product=rec.product, city=m))
            stats[r["status"]] += 1
            got = {r.get("contact")} | {x["contact"] for x in r.get("matches", [])}
            if r["status"] in ("found", "multiple") and rec.contact not in got:
                wrong.append((m, word, r))
            if r["status"] == "confirm" and norm(rec.city) in {
                norm(s["city"]) for s in r["suggestions"]
            }:
                stats["right_suggested"] += 1

    total = sum(v for k, v in stats.items() if k != "right_suggested")
    print(f"\n{total} misspellings: {dict(stats)}")
    assert not wrong, wrong[:10]
    answered = stats["found"] + stats["multiple"] + stats["right_suggested"]
    assert answered / total >= 0.93, stats
