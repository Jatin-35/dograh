"""Text normalisation shared by the index and the lookup.

Everything that is compared — record keys, aliases, caller input — goes through
``norm`` first, so the two sides of every comparison are always in the same form.
"""

from __future__ import annotations

import re
import unicodedata

# Words a caller adds around a place name that are never part of it, in English
# and in romanised Hindi ("Noida ke paas", "Noida mein"), plus pincodes. Only
# applied after the whole term failed to match, so a KB name that contains one
# of these ("Mumbai City") still matches exactly first.
_FILLER = re.compile(
    r"\b(district|dist|distt|zila|zilla|jila|jilla|tehsil|sector\s*\d+|near|area"
    r"|city|shahar|shehar|town|village|ncr|mein|me|ke paas|ke pass|ki taraf|side"
    r"|wala|wale|se|ka|ki|ke|\d{6}"  # "ka" before the "KA" = Karnataka suffix check
    # what an LLM wraps around the place when it passes a whole query
    r"|area manager|sales manager|manager|dealer|distributor|contact|number"
    r"|for|in|at|of|the|from|my|i live in|live in|rehta|rehti|rahta|rahti|hoon|hun"
    r"|mai|main|mera|meri|ghar|ji)\b"
)


def norm(text: str | None) -> str:
    """Lowercase, fold Unicode, turn punctuation into spaces, collapse spaces.

    Letters, combining marks and digits are kept. Marks matter: Devanagari vowel
    signs (the ो in नोएडा) are category M, which ``\\w`` does not match.
    """
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text).lower()
    s = s.replace("&", " and ")
    s = "".join(ch if unicodedata.category(ch)[0] in "LMN" else " " for ch in s)
    return re.sub(r"\s+", " ", s).strip()


def strip_filler(text: str) -> str:
    """Remove words like "district" or "sector 62" from an already-normalised term."""
    return re.sub(r"\s+", " ", _FILLER.sub(" ", text)).strip()


# Applied in order. Pairs first, so "sh" is folded before a lone "h" would be.
_FOLDS = (
    ("aa", "a"), ("ee", "i"), ("oo", "u"),
    ("sh", "s"), ("ph", "f"), ("bh", "b"), ("dh", "d"), ("th", "t"),
    ("kh", "k"), ("gh", "g"), ("ch", "c"), ("ck", "k"),
    ("w", "v"), ("y", "i"), ("z", "j"), ("q", "k"),
)


def fold(text: str) -> str:
    """Collapse spellings of Indian place names that sound alike.

    "Noyda" and "Noida", "Gaziabad" and "Ghaziabad", "Bareli" and "Bareily" fold
    to the same string or very nearly. Only used for fuzzy *suggestions*, never
    to return an answer directly. Non-Latin input returns "" — the folds are
    for romanised spellings.
    """
    s = norm(text)
    if not s or not s.isascii():
        return ""
    for a, b in _FOLDS:
        s = s.replace(a, b)
    s = re.sub(r"(.)\1+", r"\1", s)  # doubled letters: "bareilly" -> "bareily"
    return s.replace(" ", "")


# ---------------------------------------------------------------------------
# Devanagari -> Latin, for place names the speech-to-text wrote in Hindi script.
# Output only feeds fuzzy matching (fold + suggestion), so it aims at "sounds
# like the romanised KB name", not at a standard transliteration scheme.
# ---------------------------------------------------------------------------

_CONS = dict(zip(
    "कखगघङचछजझञटठडढणतथदधनपफबभमयरलवशषसहळ",
    ["k", "kh", "g", "gh", "n", "ch", "chh", "j", "jh", "n", "t", "th", "d", "dh", "n",
     "t", "th", "d", "dh", "n", "p", "ph", "b", "bh", "m", "y", "r", "l", "v", "sh",
     "sh", "s", "h", "l"],
))
_NUKTA_CONS = {"क": "q", "ख": "kh", "ग": "g", "ज": "z", "ड": "r", "ढ": "rh", "फ": "f"}
_VOWELS = dict(zip("अआइईउऊऋएऐओऔऑ",
                   ["a", "aa", "i", "ee", "u", "oo", "ri", "e", "ai", "o", "au", "o"]))
_MATRAS = dict(zip("ािीुूृेैोौॉ", ["aa", "i", "ee", "u", "oo", "ri", "e", "ai", "o", "au", "o"]))
_VIRAMA, _NUKTA, _VISARGA = "्", "़", "ः"
_NASALS = {"ं", "ँ"}  # anusvara, candrabindu

# Words said around a place in Hindi: "मैं सांबा में रहता हूं".
HINDI_FILLER = {norm(w) for w in (
    "में मे मैं रहता रहती रहते हूं हूँ है हैं जी से का की के पास जिला ज़िला शहर तो "
    "इधर उधर यहाँ यहां वाला वाली वाले एरिया मेरा मेरी घर हाँ हां तरफ़ तरफ और"
).split()}


def translit(text: str) -> str:
    """ "सांबा" -> "saambaa", "कानपुर" -> "kaanpur", "कटुआ" -> "katuaa"."""
    words = []
    for word in norm(text).split():
        if word in HINDI_FILLER:
            continue
        syl: list[list] = []  # [consonant, vowel, vowel_is_inherent]
        i = 0
        while i < len(word):
            ch = word[i]
            if ch in _CONS:
                c = _CONS[ch]
                if i + 1 < len(word) and word[i + 1] == _NUKTA:
                    c = _NUKTA_CONS.get(ch, c)
                    i += 1
                nxt = word[i + 1] if i + 1 < len(word) else ""
                if nxt in _MATRAS:
                    syl.append([c, _MATRAS[nxt], False]); i += 2
                elif nxt == _VIRAMA:
                    syl.append([c, "", False]); i += 2
                else:
                    syl.append([c, "a", True]); i += 1
            elif ch in _VOWELS:
                syl.append(["", _VOWELS[ch], False]); i += 1
            elif ch in _NASALS or ch == _VISARGA:
                coda = "h" if ch == _VISARGA else "N"  # N: resolved below
                if syl:
                    syl[-1][1] += coda
                    syl[-1][2] = False
                i += 1
            elif ch == _NUKTA:
                i += 1
            else:
                syl.append([ch, "", False]); i += 1
        # Hindi drops the inherent "a" at the end of a word, and between two
        # sounded syllables (V C a C V -> V C C V), scanning right to left:
        # गोरखपुर -> gorakhpur, पटना -> patnaa.
        if len(syl) > 1 and syl[-1][2]:
            syl[-1][1] = ""
        for j in range(len(syl) - 2, 0, -1):
            if syl[j][2] and syl[j - 1][1] and syl[j + 1][0] and syl[j + 1][1]:
                syl[j][1] = ""
        out = "".join(c + v for c, v, _ in syl)
        # The nasal mark sounds "m" before p/b/m (सांबा -> saamba), else "n".
        out = re.sub(r"N(?=[pbm])", "m", out).replace("N", "n")
        words.append(out)
    return " ".join(words)


def is_latin(text: str) -> bool:
    return text.isascii()


def spoken_digits(number: str) -> str:
    """"9557184174" -> "9 5 5 7 1 8 4 1 7 4", so TTS reads digits, not a quantity."""
    return " ".join(ch for ch in number if ch.isdigit())
