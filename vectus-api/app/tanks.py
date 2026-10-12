"""Vectus water-tank catalogue: exact lookups by any mix of model, capacity,
colour, layers, guarantee, mould and need.

    POST /tanks  {brand?, state?, model?, capacity?, colour?, layers?,
                  guarantee?, mould?, need?}  ->  {status, ...}

Source of truth: ``data/Tank_details.xlsx``, exactly as Vectus sends it. Every
answer is a filter over all its rows, never a similarity search, so "which
tanks come in 1100 L" lists every one and nothing else. Replace the file and
redeploy to update; startup refuses a sheet it cannot read.

The bot speaks one brand at a time: the brand the customer named, else the
state default (south → Ganga, everywhere else → Vectus), else Vectus.
"""

from __future__ import annotations

import math
import unicodedata
import re
from dataclasses import dataclass, field
from pathlib import Path

from rapidfuzz import fuzz, process

from .text import fold, norm, translit

MAX_RESULTS = 4

BRANDS = ("Vectus", "Ganga", "Waterwell")
_BRAND_RE = re.compile(r"^\s*(vectus|ganga|water\s*well)\s+(.+?)\s*$", re.I)
BRAND_ALIASES = {
    "vectus": "Vectus", "victus": "Vectus", "vektus": "Vectus", "vectas": "Vectus",
    "vector": "Vectus", "vectors": "Vectus", "वेक्टस": "Vectus",
    # how speech-to-text wrote "Vectus" on real Vectus calls
    "victor": "Vectus", "vaktis": "Vectus", "vetex": "Vectus", "dectus": "Vectus",
    "vektas": "Vectus", "वेक्टर": "Vectus", "विक्टर": "Vectus", "वैक्टस": "Vectus",
    "डेक्टस": "Vectus", "वेक्टर्स": "Vectus",
    "ganga": "Ganga", "gunga": "Ganga", "ganga tank": "Ganga", "गंगा": "Ganga",
    "waterwell": "Waterwell", "water well": "Waterwell", "watervel": "Waterwell",
    "वाटरवेल": "Waterwell",
}
ANY_BRAND = {"any", "all", "sab", "sabhi", "koi bhi", "kisi bhi", "every"}

# The prompt's brand rule, kept here so it is applied the same way every time.
SOUTH_STATES = {
    "andhra pradesh", "andhra", "ap", "a p", "telangana", "tl", "ts", "kerala", "kl",
    "karnataka", "ka", "tamil nadu", "tamilnadu", "tn", "t n",
}

# Big southern cities, for when the bot puts the city in the state field.
SOUTH_PLACES = {
    "chennai", "madras", "coimbatore", "madurai", "trichy", "tiruchirappalli", "salem",
    "tirunelveli", "vellore", "erode", "tiruppur", "thanjavur", "hosur",
    "bengaluru", "bangalore", "mysuru", "mysore", "mangaluru", "mangalore", "hubli",
    "hubballi", "belgaum", "belagavi", "davangere", "shimoga", "shivamogga",
    "hyderabad", "secunderabad", "warangal", "karimnagar", "nizamabad",
    "visakhapatnam", "vizag", "vijayawada", "guntur", "nellore", "tirupati", "kurnool",
    "kakinada", "rajahmundry", "anantapur", "kadapa",
    "kochi", "cochin", "ernakulam", "thiruvananthapuram", "trivandrum", "kozhikode",
    "calicut", "thrissur", "kollam", "kannur", "palakkad", "alappuzha", "kottayam",
}

# Rows the bot does not offer for an ordinary home enquiry. They still answer
# when named, when the need asks for them, or when nothing else matches (black,
# 10000 L, 1 or 2 layers).
SPECIAL_FAMILIES = {"chemical tank", "isi tank", "underground", "group"}

# Spoken model names → the sheet's model (brand removed, normalised). Fuzzy
# matching covers ordinary typos on top of these.
MODEL_ALIASES: dict[str, str] = {
    "cool": "cool", "kool": "cool", "kul": "cool", "cooler": "cool", "cool tank": "cool",
    # speech-to-text hears "Vectus Cool" as these (real calls)
    "school": "cool", "vector school": "cool", "vectors school": "cool",
    "vectus school": "cool", "cool school": "cool", "school model": "cool",
    "school tank": "cool",
    "italian": "italian", "itallian": "italian", "italiyan": "italian",
    "tenx": "tenx", "ten x": "tenx", "10x": "tenx", "10 x": "tenx", "ten ex": "tenx",
    "tenex": "tenx", "10 ex": "tenx",
    "t 90": "t 90", "t90": "t 90", "t ninety": "t 90", "tee 90": "t 90", "ti 90": "t 90",
    "t 9 0": "t 90",
    "granito": "granito", "granite": "granito", "grenito": "granito", "granita": "granito",
    "graneto": "granito", "granito tank": "granito",
    "garnier to": "granito", "garnierto": "granito", "garnito": "granito",  # real calls
    "ग्रेनीटो": "granito", "ग्रेनिटो": "granito",
    "puff": "puff", "puf": "puff", "paff": "puff", "puff tank": "puff",
    "silk": "silk", "silky": "silk",
    "smart": "smart", "smaart": "smart",
    "safe": "safe", "seif": "safe",
    "health": "health", "helth": "health",
    "loft": "loft", "lofts": "loft", "loft tank": "loft",
    "pro": "pro",
    "mini": "mini", "minni": "mini",
    "isi": "isi tank", "isi tank": "isi tank", "i s i": "isi tank", "isi mark": "isi tank",
    "underground": "underground", "under ground": "underground", "ug": "underground",
    "ug tank": "underground", "underground tank": "underground",
    "horizontal": "horizontal tank", "horizontal tank": "horizontal tank",
    "horizental": "horizontal tank", "horijontal": "horizontal tank",
    "chemical": "chemical tank", "chemical tank": "chemical tank", "kemikal": "chemical tank",
    "group": "group", "group tank": "group",
    # model names the speech-to-text wrote in Hindi script
    "कूल": "cool", "पफ": "puff", "पफ़": "puff", "सिल्क": "silk", "स्मार्ट": "smart",
    "सेफ": "safe", "सेफ़": "safe", "ग्रेनाइटो": "granito", "ग्रेनाइट": "granito",
    "टेन एक्स": "tenx", "टेनेक्स": "tenx", "टी 90": "t 90", "टी नाइंटी": "t 90",
    "लॉफ्ट": "loft", "मिनी": "mini", "प्रो": "pro", "हेल्थ": "health",
    "इटालियन": "italian", "अंडरग्राउंड": "underground", "केमिकल": "chemical tank",
}

# Spoken colour → the sheet's colour. "Lime Yellow" is the sheet's only yellow.
COLOUR_ALIASES: dict[str, str] = {
    "white": "White", "safed": "White", "safaid": "White", "sufed": "White",
    "safed rang": "White", "सफेद": "White", "सफ़ेद": "White",
    "black": "Black", "kala": "Black", "kaala": "Black", "काला": "Black",
    "green": "Green", "hara": "Green", "hari": "Green", "हरा": "Green",
    "blue": "Blue", "neela": "Blue", "nila": "Blue", "neeli": "Blue", "नीला": "Blue",
    "yellow": "Lime Yellow", "lime": "Lime Yellow", "lime yellow": "Lime Yellow",
    "peela": "Lime Yellow", "pila": "Lime Yellow", "peeli": "Lime Yellow",
    "पीला": "Lime Yellow",
    "ruby": "Ruby", "emerald": "Emerald", "pearl": "Pearl",
}

# What the customer wants the tank *for*, → a test on the sheet's own columns.
# Every test reads the Excel (layer names, features, model), nothing invented.
NEEDS: dict[str, tuple[str, ...]] = {
    "cooling": ("cool", "cooling", "thanda", "thanda pani", "garmi", "heat", "hot",
                "temperature", "insulated", "insulation", "dhoop", "sunlight", "sun",
                "summer", "thanda rahe", "garmiyon"),
    # rain, storms, harsh weather (asked on real calls: "barish bahut hoti hai")
    "weather": ("weather", "barish", "baarish", "rain", "rainy", "toofan", "tufan", "storm",
                "mausam", "durable", "durability", "majboot", "long life", "tikau"),
    "underground": ("underground", "under ground", "zameen", "zameen ke neeche",
                    "bhoomigat", "sump"),
    "small_space": ("small space", "small", "kam jagah", "chhoti jagah", "space saving",
                    "parapet", "loft", "less space", "compact"),
    "commercial": ("commercial", "business", "industrial", "factory", "bulk", "large",
                   "big", "project", "society", "hotel"),
    "chemical": ("chemical", "chemicals", "acid", "kemikal"),
    "hygiene": ("hygiene", "bacteria", "germs", "anti bacterial", "anti microbial",
                "microban", "saaf pani", "clean water", "pure water"),
}
_NEED_LOOKUP = {norm(w): need for need, words in NEEDS.items() for w in words}

_WORDS_DROPPED = re.compile(
    r"\b(tank|tanks|tanki|tankee|water|pani|paani|overhead|naya|new|model|wala|wali|wale|"
    r"ka|ki|ke|ko|mein|me|chahiye|"
    r"chaiye|batao|bata|do|dijiye|please|litre|liter|litres|liters|ltr|l|colour|color|"
    r"rang|layer|layers|saal|years|year|yrs|yr|guarantee|warranty)\b"
)


class CatalogError(Exception):
    """The sheet could not be read as a tank catalogue."""


@dataclass(frozen=True)
class Tank:
    brand: str
    model: str          # the sheet's name without the brand, e.g. "Cool", "T-90"
    key: str            # norm(model): "cool", "t 90"
    layers: int
    colours: tuple[str, ...]
    capacities: tuple[int, ...]
    guarantee: int | None
    mould: str          # "Roto" / "Blow"
    layer_names: tuple[str, ...]
    features: tuple[str, ...]

    @property
    def name(self) -> str:
        return f"{self.brand} {self.model}"

    @property
    def special(self) -> bool:
        return self.key in SPECIAL_FAMILIES

    def twin_key(self) -> tuple:
        """Same build under another name (Health = Safe, Cool = Italian, Pro = Smart)."""
        return (self.mould, tuple(norm(n) for n in self.layer_names), self.capacities)


@dataclass
class Catalog:
    tanks: list[Tank]
    source: str
    model_aliases: dict[str, str] = field(default_factory=dict)

    def by_brand(self, brand: str) -> list[Tank]:
        return [t for t in self.tanks if t.brand == brand]


# ---------------------------------------------------------------------------
# Loading the sheet
# ---------------------------------------------------------------------------

_COLUMNS = {
    "name": ("tank name",),
    "layers": ("no of layers", "no. of layers", "layers"),
    "colour": ("color", "colour"),
    "capacities": ("capacities", "capacity"),
    "mould": ("type",),
    "guarantee": ("gurantee yrs", "guarantee yrs", "gurantee", "guarantee"),
    "features": ("features",),
    "layer_names": ("name of layers",),
}


def _capacities(cell) -> tuple[int, ...]:
    # "550, 750, 1100" and "15,000 , 20,000": a comma followed by exactly three
    # digits is a thousands separator, any other comma separates values.
    text = str(cell or "")
    values = [int(m.replace(",", "")) for m in re.findall(r"\d{1,3}(?:,\d{3})+(?!\d)|\d+", text)]
    return tuple(sorted(set(values)))


class _Blank(Exception):
    pass


def _guarantee(cell) -> int | None:
    # A blank cell is a gap in the sheet, not "no guarantee": saying a tank has
    # no guarantee when the sheet simply forgot the value would be a false claim.
    if cell is None or not str(cell).strip():
        raise _Blank
    m = re.search(r"\d+", str(cell))
    return int(m.group()) if m else None  # "No gurantee"


def _colours(cell) -> tuple[str, ...]:
    parts = re.split(r"\s*(?:,|/|;|&|\band\b)\s*", str(cell or ""))
    return tuple(dict.fromkeys(re.sub(r"\s+", " ", p).strip().title() for p in parts if p.strip()))


def _layer_names(cell) -> tuple[str, ...]:
    text = str(cell or "")
    text = re.sub(r"\s*®\s*infused with microban technology\.?", " (Microban)", text, flags=re.I)
    parts = [p.strip(" .") for p in re.split(r"\d+\s*-\s*", text)]
    return tuple(re.sub(r"\s+", " ", p) for p in parts if p and p.lower() not in {"none"})


def _features(cell) -> tuple[str, ...]:
    """Each numbered feature, by its title where it has one.

    Two kinds are left out because the sheet contradicts itself there and the
    Capacities / layers columns are the ones to trust: the size-range item
    ("Available in sizes from 550 to 5000 litres" next to a 500 L capacity) and
    a layer count inside the text (Horizontal Tank: "three-layer configuration"
    on a 10-layer tank).
    """
    text = str(cell or "")
    items = re.split(r"(?:^|(?<=[\s.]))\d+\s*-\s*", text)
    out = []
    for item in items:
        item = re.sub(r"\s+", " ", item).strip(" .")
        if not item:
            continue
        if re.search(r"wide (range|size)|size range|layer configuration", item, re.I):
            continue
        title, _, desc = item.partition(":")
        if ":" in item and len(title) <= 60:
            # Keep what the feature does, in one short clause: callers ask "what
            # does the titanium layer mean?" and the sheet's text answers it.
            first = re.split(r"(?<=[a-z])[.;]\s|,\s(?:ensuring|guaranteeing|making|offering)",
                             desc.strip())[0].strip(" .")
            out.append(_clip(f"{title.strip()}: {first}", 160))
        else:
            out.append(_clip(item, 120))
    return tuple(out)


def _clip(text: str, limit: int) -> str:
    """Shorten at a word boundary, never mid-word."""
    if len(text) <= limit:
        return text.rstrip(" ,")
    return text[:limit].rsplit(" ", 1)[0].rstrip(" ,;")


def _header_map(header: tuple) -> dict[str, int]:
    names = {norm(str(h)): i for i, h in enumerate(header) if h is not None}
    found = {}
    for col, options in _COLUMNS.items():
        for option in options:
            if norm(option) in names:
                found[col] = names[norm(option)]
                break
        else:
            raise CatalogError(f"column {options[0]!r} not found in the sheet header {header}")
    return found


def load_catalog(path: str | Path) -> Catalog:
    import openpyxl  # only needed at startup

    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:  # missing file, not an xlsx
        raise CatalogError(f"cannot open {path}: {exc}") from exc
    # The first sheet whose header has a "Tank Name" column: an empty or notes
    # sheet in front of it must not stop startup.
    rows: list = []
    for ws in wb.worksheets:
        sheet_rows = [r for r in ws.iter_rows(values_only=True)
                      if any(c is not None and str(c).strip() for c in r)]
        if sheet_rows and any(norm(str(h)) == "tank name" for h in sheet_rows[0] if h):
            rows = sheet_rows
            break
        rows = rows or sheet_rows
    wb.close()
    if len(rows) < 2:
        raise CatalogError("the sheet has no tank rows")
    cols = _header_map(rows[0])

    tanks: list[Tank] = []
    for n, row in enumerate(rows[1:], start=2):
        raw_name = str(row[cols["name"]] or "").strip()
        m = _BRAND_RE.match(raw_name)
        if not m:
            raise CatalogError(f"row {n}: tank name {raw_name!r} does not start with a brand")
        brand = BRAND_ALIASES[norm(m.group(1))]
        model = m.group(2).strip()
        model = model[:1].upper() + model[1:]  # "group" -> "Group", "ISI tank" kept
        capacities = _capacities(row[cols["capacities"]])
        try:
            layers = int(row[cols["layers"]])
        except (TypeError, ValueError):
            raise CatalogError(f"row {n} ({raw_name}): layers is not a number") from None
        if not capacities:
            raise CatalogError(f"row {n} ({raw_name}): no capacities")
        colours = _colours(row[cols["colour"]])
        try:
            guarantee = _guarantee(row[cols["guarantee"]])
        except _Blank:
            raise CatalogError(f"row {n} ({raw_name}): guarantee is blank "
                               "(write the years, or 'No guarantee')") from None
        if not colours:
            raise CatalogError(f"row {n} ({raw_name}): no colour")
        mould = str(row[cols["mould"]] or "").strip().title()
        if mould not in {"Roto", "Blow"}:
            raise CatalogError(f"row {n} ({raw_name}): type {mould!r} is not Roto or Blow")
        tanks.append(Tank(
            brand=brand, model=model, key=norm(model), layers=layers, colours=colours,
            capacities=capacities, guarantee=guarantee, mould=mould,
            layer_names=_layer_names(row[cols["layer_names"]]),
            features=_features(row[cols["features"]]),
        ))

    seen = set()
    for t in tanks:
        if (t.brand, t.key) in seen:
            raise CatalogError(f"{t.name} appears twice in the sheet")
        seen.add((t.brand, t.key))
    # Aliases for a model the sheet no longer has are dropped, not fatal: Vectus
    # retiring a model must not stop the service from starting.
    keys = {t.key for t in tanks}
    aliases = {norm(a): t for a, t in MODEL_ALIASES.items() if t in keys}
    aliases.update({k: k for k in keys})
    return Catalog(tanks=tanks, source=Path(path).name, model_aliases=aliases)


# ---------------------------------------------------------------------------
# Reading what the bot sent
# ---------------------------------------------------------------------------

_NUM_WORDS = {
    "ek": 1, "do": 2, "teen": 3, "tin": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5,
    "chhe": 6, "che": 6, "chhah": 6, "saat": 7, "sat": 7, "aath": 8, "ath": 8, "nau": 9,
    "das": 10, "dus": 10, "gyarah": 11, "gyara": 11, "barah": 12, "bara": 12,
    "pandrah": 15, "pandra": 15, "bees": 20, "bis": 20, "pachees": 25, "pachchis": 25,
    "tees": 30, "tis": 30, "dedh": 1.5, "derh": 1.5, "dhai": 2.5, "adhai": 2.5, "dhaai": 2.5,
    # as the Hindi-script transliteration spells them
    "dhaaee": 2.5, "gyaarah": 11, "baarah": 12, "pachchees": 25, "chhah": 6,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15,
    "twenty": 20, "thirty": 30,
}
_MULTIPLIERS = {"sau": 100, "so": 100, "hundred": 100, "hazar": 1000, "hazaar": 1000,
                "hajar": 1000, "hajaar": 1000, "thousand": 1000, "k": 1000}
_HALF = {"saade": 0.5, "sade": 0.5, "saadhe": 0.5, "saarhe": 0.5, "sava": 0.25, "savaa": 0.25,
         "paune": -0.25, "pone": -0.25}


def _spoken_number(words: list[str]) -> float | None:
    """"saade saat sau" -> 750, "dedh hazaar" -> 1500, "gyarah sau" -> 1100."""
    total, current, bump, seen = 0.0, 0.0, 0.0, False
    for w in words:
        if w in _HALF:
            bump = _HALF[w]
        elif w in _NUM_WORDS:
            current = _NUM_WORDS[w] + bump
            bump, seen = 0.0, True
        elif w in _MULTIPLIERS and seen:
            total += (current or 1) * _MULTIPLIERS[w]
            current = 0.0
        elif re.fullmatch(r"\d+(\.\d+)?", w):
            current, seen = float(w), True
    total += current
    return total if seen and total > 0 else None


NO_LIMIT = 10**9
MAX_LITRES = 1_000_000  # anything bigger is a mis-hearing, not a tank size


@dataclass(frozen=True)
class Cap:
    """What the customer asked for, in litres.

    One size (low == high), a range ("1000 se 1500"), an open range ("500 tak",
    "2000 se upar"), or a choice of sizes ("1000 ya 1500": ``values``).
    """

    low: int
    high: int
    values: tuple[int, ...] | None = None

    def ok(self, c: int) -> bool:
        return c in self.values if self.values else self.low <= c <= self.high

    @property
    def label(self) -> str:
        if self.values:
            return " or ".join(str(v) for v in self.values) + " litre"
        if self.low == self.high:
            return f"{self.low} litre"
        if self.high >= NO_LIMIT:
            return f"{self.low} litre or more"
        if self.low <= 1:
            return f"up to {self.high} litre"
        return f"{self.low}-{self.high} litre"


_HINDI_JOINERS = {"से": "se", "तक": "tak", "या": "ya", "बीच": "beech", "ऊपर": "upar",
                  "ज़्यादा": "zyada", "ज्यादा": "zyada", "कम": "kam", "लीटर": "litre",
                  "लिटर": "litre"}
_RANGE_WORDS = {"se", "to", "till", "tak", "between", "beech", "and"}
_CHOICE_WORDS = {"or", "ya", "yaa", "either"}
_UP_TO = re.compile(r"\b(tak|upto|up to|under|below|se kam|less than|max|maximum|within|"
                    r"andar|ke andar|tak ka|tak ki)\b")
_AT_LEAST = re.compile(r"\b(se upar|se uper|se zyada|se jyada|se bada|se badi|above|over|"
                       r"more than|at least|atleast|minimum|min|kam se kam|plus)\b|\+")


def parse_capacity(value) -> Cap | None:
    """What capacity was asked: "1100", "saade saat sau", "1000 se 1500",
    "1000 ya 1500", "500 tak", "2000 se upar"."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or not 50 <= value <= MAX_LITRES:
            return None  # Infinity, NaN, 1e30: not a tank size
        return Cap(int(value), int(value))
    text = str(value).lower().replace("₹", " ")
    if re.match(r"\s*0+\s*(se|to|till|-)\b", text):   # "0 se 500" = up to 500
        text = re.sub(r"^\s*0+\s*(se|to|till|-)", " ", text) + " tak"
    text = re.sub(r"(\d),(\d{3})(?!\d)", r"\1\2", text)          # 1,000 -> 1000
    text = re.sub(r"(\d)\s*-\s*(\d)", r"\1 to \2", text)          # 1000-1500
    # Hindi script ("1,000 से 1,500", "डेढ़ हज़ार"): the joining words first, since
    # the transliteration drops "से" as filler; then the number words.
    for hindi, latin in _HINDI_JOINERS.items():
        text = re.sub(_word(hindi), f" {latin} ", text)
    text = re.sub(f"[{_DEVANAGARI}]+", lambda m: f" {translit(m.group())} ", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"(\d)\s*[/,]\s*(\d)", r"\1 or \2", text)       # 1000/1500, 1000, 1500
    text = re.sub(r"(\d+(?:\.\d+)?)\s*k\b", lambda m: str(float(m.group(1)) * 1000), text)
    text = re.sub(r"(\d)\s*(l|ltr|litre|liter|litres|liters|lit)\b", r"\1 ", text)
    at_least = bool(_AT_LEAST.search(text))
    up_to = not at_least and bool(_UP_TO.search(text))
    words = re.findall(r"\d+(?:\.\d+)?|[a-z]+", text)
    # Split into the numbers said, remembering whether they were joined by a
    # range word ("se", "to") or a choice word ("ya", "or").
    sides, cur, choice = [], [], False
    for w in words:
        if w in _RANGE_WORDS or w in _CHOICE_WORDS:
            choice = choice or w in _CHOICE_WORDS
            if cur:
                sides.append(cur)
            cur = []
        else:
            cur.append(w)
    if cur:
        sides.append(cur)
    values = [v for v in (_spoken_number(s) for s in sides) if v]
    # A bare multiplier on the last side ("1 se 2 hazaar") applies to the others.
    if len(values) >= 2 and values[-1] >= 50:
        unit = 1000 if values[-1] >= 1000 else 100
        values = [v * unit if v < 50 else v for v in values]
    values = sorted({int(round(v)) for v in values if 50 <= v <= MAX_LITRES})
    if not values:
        return None
    if len(values) == 1:
        v = values[0]
        if at_least:
            return Cap(v, NO_LIMIT)
        if up_to:
            return Cap(1, v)
        return Cap(v, v)
    if choice:
        return Cap(values[0], values[-1], tuple(values))
    return Cap(values[0], values[-1])


def _int_field(value, words_ok: bool = True) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if math.isfinite(value) else None
    text = norm(str(value))
    m = re.search(r"\d+", text)
    if m:
        return int(m.group())
    if words_ok:
        n = _spoken_number(text.split())
        return int(n) if n else None
    return None


def _clean(value) -> str:
    if value is None:
        return ""
    text = norm(str(value))
    return "" if text in {"", "null", "none", "na", "n a", "undefined", "unknown"} else text


def parse_brand(brand, model_text: str, state) -> tuple[str | None, str]:
    """(brand or None for every brand, why)."""
    # A brand inside the model text ("Ganga Pro") is the most specific signal.
    for text, why in ((model_text, "asked"), (_clean(brand), "asked")):
        if not text:
            continue
        if text in ANY_BRAND:
            return None, "all brands"
        for alias, name in BRAND_ALIASES.items():
            if re.search(_word(norm(alias)), text):
                return name, why
    st = _clean(state)
    if st:
        south = (st in SOUTH_STATES
                 or any(w in SOUTH_PLACES for w in st.split())
                 or any(re.search(rf"\b{re.escape(x)}\b", st) for x in SOUTH_STATES if len(x) > 3))
        return ("Ganga" if south else "Vectus"), "state default"
    return "Vectus", "default"


def parse_model(catalog: Catalog, value) -> tuple[str | None, str]:
    """(model key or None, the text that was not understood)."""
    text = _clean(value)
    if not text:
        return None, ""
    for alias in sorted(BRAND_ALIASES, key=len, reverse=True):
        text = re.sub(_word(norm(alias)), " ", text)
    stripped = re.sub(r"\s+", " ", _WORDS_DROPPED.sub(" ", text)).strip()
    for candidate in (text.strip(), stripped):
        if candidate in catalog.model_aliases:
            return catalog.model_aliases[candidate], ""
        if candidate.endswith("s") and candidate[:-1] in catalog.model_aliases:  # "Puffs"
            return catalog.model_aliases[candidate[:-1]], ""
    if not stripped:
        return None, ""
    # "Cool" is the model speech-to-text mangles most: on real calls it came out
    # as "Victor school", "Doctor school", "vertical school", "practice pool",
    # "बैपटिस्ट स्कूल". A last word of school/pool means Cool; "pool" alone does not.
    words = stripped.split()
    if "cool" in catalog.model_aliases.values() and words[-1] in _COOL_ENDINGS and \
            (words[-1] not in {"pool", "पूल"} or len(words) > 1):
        return "cool", ""
    if not stripped.isascii():
        stripped = translit(stripped)
    target = fold(stripped)
    if target:
        folded = {fold(a): k for a, k in catalog.model_aliases.items() if fold(a)}
        if target in folded:
            return folded[target], ""
        # A typo of a model name, not a different word: same first sound, close
        # spelling, and long enough to judge ("proof" is not "puff", "ninety" is
        # not "T-90", "granted" is not "Granito").
        if len(target) >= 4:
            hit = process.extractOne(target, [k for k in folded if k[0] == target[0]],
                                     scorer=fuzz.ratio, score_cutoff=88)
            if hit:
                return folded[hit[0]], ""
    return None, _heard(value)


def _heard(value) -> str:
    """What the caller said, safe to hand back: short, letters and digits only.

    It is returned in a field the LLM reads, so it must never carry an
    instruction ("SYSTEM: ...") or markup through to the model.
    """
    # Letters, marks (Hindi vowel signs are marks, not \w) and digits only.
    text = "".join(ch if unicodedata.category(ch)[0] in "LMN" or ch in " -" else " "
                   for ch in str(value or ""))
    return re.sub(r"\s+", " ", text).strip()[:40]


_DEVANAGARI = "ऀ-ॿ"


def _word(phrase: str) -> str:
    """A regex for ``phrase`` as whole words, Hindi script included (``\\b`` is not
    enough there: a vowel sign counts as a non-word character)."""
    return rf"(?<![\w{_DEVANAGARI}]){re.escape(phrase)}(?![\w{_DEVANAGARI}])"


_COOL_ENDINGS = {"school", "schools", "skool", "scool", "pool", "स्कूल", "पूल", "cool", "kool"}


def parse_colours(value) -> tuple[set[str], str]:
    text = _clean(value)
    if not text:
        return set(), ""
    found: set[str] = set()
    rest = text
    for alias in sorted(COLOUR_ALIASES, key=len, reverse=True):
        a = norm(alias)
        if re.search(rf"(?<!\w){re.escape(a)}(?!\w)", rest):
            found.add(COLOUR_ALIASES[alias])
            rest = re.sub(rf"(?<!\w){re.escape(a)}(?!\w)", " ", rest)
    if not found:
        t = translit(text) if not text.isascii() else text
        for word in t.split():
            hit = process.extractOne(fold(word), {fold(a): c for a, c in COLOUR_ALIASES.items()},
                                     scorer=fuzz.ratio, score_cutoff=85)
            if hit:
                found.add(hit[0])
    return found, ("" if found else _heard(value))


def parse_need(value) -> tuple[str | None, str]:
    text = _clean(value)
    if not text:
        return None, ""
    if text in _NEED_LOOKUP:
        return _NEED_LOOKUP[text], ""
    for phrase in sorted(_NEED_LOOKUP, key=len, reverse=True):
        if re.search(rf"\b{re.escape(phrase)}\b", text):
            return _NEED_LOOKUP[phrase], ""
    return None, _heard(value)


def parse_mould(value) -> str | None:
    text = _clean(value)
    if "roto" in text:
        return "Roto"
    if "blow" in text:
        return "Blow"
    return None


def parse_guarantee(value) -> tuple[int | None, bool]:
    """(years, wants_no_guarantee)."""
    text = _clean(value)
    if not text and not isinstance(value, (int, float)):
        return None, False
    if text in {"no", "no guarantee", "nahi", "without guarantee", "0"}:
        return None, True
    return _int_field(value), False


def _need_ok(t: Tank, need: str) -> bool:
    names = " ".join(t.layer_names).lower()
    feats = " ".join(t.features).lower()
    if need == "cooling":
        return any(w in names for w in ("foam", "heat-reflective", "weather-resistant"))
    if need == "underground":
        return t.key == "underground"
    if need == "small_space":
        return bool(re.search(r"small spaces|space-saving|parapet", feats))
    if need == "chemical":
        return t.key == "chemical tank"
    if need == "hygiene":
        return "anti-bacterial" in names or "anti-microbial" in names
    if need == "weather":  # rain/storm/durability: the sheet's strength and weather layers
        return any(w in names for w in ("weather-resistant", "strength", "stress-crack"))
    return True  # commercial: no filter, it only opens up the large tanks


# ---------------------------------------------------------------------------
# Answering
# ---------------------------------------------------------------------------


def _guarantee_text(t: Tank) -> str:
    return f"{t.guarantee} year guarantee" if t.guarantee else "no guarantee"


def _cap_text(caps) -> str:
    caps = list(caps)
    if len(caps) == 1:
        return f"{caps[0]} litre only"
    return f"{caps[0]} to {caps[-1]} litres ({', '.join(str(c) for c in caps)})"


def describe(t: Tank) -> str:
    return (f"{t.name}: {t.layers} layer, {' / '.join(t.colours)}, {_cap_text(t.capacities)}, "
            f"{_guarantee_text(t)}, {t.mould} moulded")


def _card(t: Tank, detail: bool) -> dict:
    card = {"name": t.name, "layers": t.layers, "colours": list(t.colours),
            "capacities": list(t.capacities), "guarantee_years": t.guarantee,
            "mould": t.mould}
    if detail:
        card["layer_names"] = list(t.layer_names)
        card["features"] = list(t.features)
    return card


def _closest(caps: list[int], cap: Cap) -> dict:
    below = [c for c in caps if c < cap.low]
    above = [c for c in caps if c > cap.high]
    return {"lower": max(below) if below else None, "higher": min(above) if above else None}


def _cap_ok(t: Tank, cap: Cap | None) -> bool:
    return cap is None or any(cap.ok(c) for c in t.capacities)


def _cap_label(cap: Cap) -> str:
    return cap.label


@dataclass
class Filters:
    cap: Cap | None = None
    colours: set[str] = field(default_factory=set)
    layers: int | None = None
    guarantee: int | None = None
    no_guarantee: bool = False
    mould: str | None = None
    need: str | None = None

    def tests(self) -> dict:
        """Each filter given, as name -> test(tank)."""
        out = {}
        if self.cap:
            out["capacity"] = lambda t: _cap_ok(t, self.cap)
        if self.colours:
            out["colour"] = lambda t: bool(self.colours & set(t.colours))
        if self.layers is not None:
            out["layers"] = lambda t: t.layers == self.layers
        if self.guarantee is not None:
            out["guarantee"] = lambda t: t.guarantee == self.guarantee
        if self.no_guarantee:
            out["guarantee"] = lambda t: t.guarantee is None
        if self.mould:
            out["mould"] = lambda t: t.mould == self.mould
        if self.need and self.need != "commercial":
            out["need"] = lambda t: _need_ok(t, self.need)
        return out

    def label(self) -> str:
        bits = []
        if self.cap:
            bits.append(_cap_label(self.cap))
        if self.colours:
            bits.append("/".join(sorted(self.colours)))
        if self.layers is not None:
            bits.append(f"{self.layers} layer")
        if self.guarantee is not None:
            bits.append(f"{self.guarantee} year guarantee")
        if self.no_guarantee:
            bits.append("no guarantee")
        if self.mould:
            bits.append(f"{self.mould} moulded")
        if self.need:
            bits.append(f"{self.need.replace('_', ' ')} need")
        return ", ".join(bits)


def _sorted(tanks: list[Tank], brand: str | None) -> list[Tank]:
    order = {b: i for i, b in enumerate(BRANDS)}
    return sorted(tanks, key=lambda t: (t.special, t.layers, order[t.brand], t.model))


def _pool(catalog: Catalog, brand: str | None) -> list[Tank]:
    return catalog.tanks if brand is None else catalog.by_brand(brand)


def _listing(tanks: list[Tank], label: str, ignored: dict, brand_info: dict) -> dict:
    shown = tanks[:MAX_RESULTS]
    detail = len(shown) == 1
    lines = "; ".join(describe(t) for t in shown)
    more = len(tanks) - len(shown)
    msg = f"Tanks ({label}): {lines}." if label else f"Tanks: {lines}."
    if detail:
        t = shown[0]
        msg = (f"{describe(t)}. Layers: {', '.join(t.layer_names) or 'not listed'}. "
               f"Features: {'; '.join(t.features) or 'not listed'}. "
               "Answer only the customer's question from this.")
    elif more:
        msg += (f" Also: {', '.join(t.name for t in tanks[len(shown):])}. Say these names and "
                "ask which capacity, colour or layers the customer prefers.")
    else:
        msg += " Say these names briefly and ask which one the customer prefers."
    out = {"status": "found", **brand_info, "count": len(tanks),
           "tanks": [_card(t, detail) for t in shown], "message": msg}
    if more:
        out["more"] = [t.name for t in tanks[len(shown):]]
    if ignored:
        out["ignored"] = ignored
    return out


_MODEL_SPLIT = re.compile(r"\b(?:aur|and|or|ya|vs|versus|compare|comparison|with|ke saath)\b|[,/&+]")


def _unpack_model_text(model, capacity, colour, layers):
    """An LLM often puts the whole phrase in ``model``: "Cool 1100 litre",
    "green Safe", "5 layer wala". Move each part to its own field (only when
    that field is empty) and return the model names left, split on "aur", "vs"."""
    # norm() turns punctuation into spaces, so mark list separators first:
    # "Smart, Safe and Cool" must still split into three models.
    # ("1,100" keeps its comma: that one is a thousands separator).
    text = _clean(re.sub(r"(?<!\d),|,(?!\d{3})|[/&+]", " aur ", str(model))
                  if model is not None else None)
    if not text:
        return [], capacity, colour, layers
    m = re.search(r"\b(\d+)\s*layers?\b", text)
    if m:
        if layers in (None, ""):
            layers = int(m.group(1))
        text = text.replace(m.group(0), " ")
    m = re.search(r"\b(\d{1,2} \d{3}|\d{3,5})\s*(?:l|ltr|litre|liter|litres|liters)?\b", text)
    if m:  # 3+ digits only: "T-90" and "10x" are model names, not capacities
        if capacity in (None, ""):
            capacity = m.group(1).replace(" ", "")
        text = text.replace(m.group(0), " ")
    found, _ = parse_colours(text)
    if found:
        if colour in (None, ""):
            colour = " ".join(sorted(found))
        for alias in sorted(COLOUR_ALIASES, key=len, reverse=True):
            text = re.sub(rf"(?<!\w){re.escape(norm(alias))}(?!\w)", " ", text)
    text = re.sub(r"\b(\w+)(?:\s+\1\b)+", r"\1", text)  # "cool cool cool": a stutter
    parts = [p.strip() for p in _MODEL_SPLIT.split(text)]
    return [p for p in parts if p and _WORDS_DROPPED.sub("", p).strip()], capacity, colour, layers


def _compare(catalog, keys, brand_name, brand_info, ignored, f: Filters | None = None) -> dict:
    tanks = []
    for key in keys:
        pool = _pool(catalog, brand_name)
        tank = next((t for t in pool if t.key == key), None)
        if tank is None:  # not sold under this brand: its twin, else any brand
            others = [t for t in catalog.tanks if t.key == key]
            tank = next((t for t in pool if any(t.twin_key() == o.twin_key() for o in others)),
                        others[0])
        if tank not in tanks:
            tanks.append(tank)
    tanks = tanks[:MAX_RESULTS]

    def fits(t: Tank) -> str:
        # "Granito ya Cool, 1000 litre mein?" (a real call): say per tank whether
        # it comes in that size, so a comparison never implies one that does not.
        if not f or not f.cap:
            return ""
        if _cap_ok(t, f.cap):
            return f", comes in {f.cap.label}"
        near = _closest(list(t.capacities), f.cap)
        alt = " or ".join(str(c) for c in (near["lower"], near["higher"]) if c)
        return f", does NOT come in {f.cap.label} (closest {alt} litre)"

    out = {"status": "found", **brand_info, "count": len(tanks),
           "tanks": [_card(t, True) for t in tanks],
           "message": "Compare: " + "; ".join(
               f"{describe(t)}{fits(t)}, layers: {', '.join(t.layer_names) or 'not listed'}"
               for t in tanks) + ". Answer the customer's comparison briefly from these."}
    if ignored:
        out["ignored"] = ignored
    return out


def find(catalog: Catalog, *, brand=None, state=None, model=None, capacity=None,
         colour=None, layers=None, guarantee=None, mould=None, need=None) -> dict:
    model_parts, capacity, colour, layers = _unpack_model_text(model, capacity, colour, layers)
    parsed = [parse_model(catalog, p) for p in model_parts]
    known = list(dict.fromkeys(k for k, _ in parsed if k))
    model_bad = next((bad for k, bad in parsed if bad), "")
    model_key = known[0] if known else None
    brand_name, brand_why = parse_brand(brand, _clean(model), state)
    brand_info = {"brand": brand_name or "all", "brand_reason": brand_why}

    ignored: dict[str, str] = {}
    f = Filters()
    if capacity not in (None, ""):
        f.cap = parse_capacity(capacity)
        if f.cap is None and _clean(capacity):
            ignored["capacity"] = str(capacity)
    f.colours, colour_bad = parse_colours(colour)
    f.layers = _int_field(layers) if _clean(layers) or isinstance(layers, (int, float)) else None
    f.guarantee, f.no_guarantee = parse_guarantee(guarantee)
    f.mould = parse_mould(mould)
    f.need, need_bad = parse_need(need)
    if need_bad:
        ignored["need"] = need_bad
    if _clean(brand) and brand_why != "asked" and _clean(brand) not in ANY_BRAND:
        ignored["brand"] = str(brand)  # e.g. a competitor's name
    if len(known) > 1:
        return _compare(catalog, known, brand_name, brand_info, ignored, f)
    if model_bad and known:
        model_bad = ""  # "Cool aur xyz": answer for the model we know
    if f.need in {"underground", "chemical"} and not model_key:
        model_key = "underground" if f.need == "underground" else "chemical tank"

    if model_bad:
        names = sorted({t.model for t in _pool(catalog, brand_name)})
        return {"status": "unknown_model", **brand_info, "heard": model_bad, "models": names,
                "message": "That model name is not in the catalogue. Confirm the model name "
                           f"with the customer once. Models: {', '.join(names)}."}
    if colour_bad:
        colours = sorted({c for t in _pool(catalog, brand_name) for c in t.colours})
        return {"status": "unknown_colour", **brand_info, "heard": colour_bad,
                "colours": colours,
                "message": "No tank comes in that colour. "
                           f"Available colours: {', '.join(colours)}."}

    tests = f.tests()

    # --- a named model ----------------------------------------------------
    if model_key:
        if brand_name is None:  # all brands: the model in each, plus the same tank renamed
            named = [t for t in catalog.tanks if t.key == model_key]
            twins = [t for t in catalog.tanks if t not in named
                     and any(t.twin_key() == n.twin_key() for n in named)]
            hits = [t for t in named + twins if all(test(t) for test in tests.values())]
            if hits:
                ordered = _sorted([t for t in hits if t in named], None) + \
                    _sorted([t for t in hits if t in twins], None)
                return _listing(ordered, f.label(), ignored, brand_info)
            return _model_not_available(catalog, named[0], [n for n, test in tests.items()
                                                            if not test(named[0])],
                                        f, tests, None, brand_info, ignored)
        tank = next((t for t in _pool(catalog, brand_name) if t.key == model_key), None)
        if tank is None:
            return _not_in_brand(catalog, model_key, brand_name, f, tests, brand_info)
        failed = [name for name, test in tests.items() if not test(tank)]
        if not failed:
            out = _listing([tank], f.label(), ignored, brand_info)
            if f.cap:
                out["message"] = (f"Yes, {tank.name} comes in {_cap_label(f.cap)}. "
                                  + out["message"])  # keeps the layers and features
            return out
        return _model_not_available(catalog, tank, failed, f, tests, brand_name, brand_info,
                                    ignored)

    # --- open search ------------------------------------------------------
    pool = _pool(catalog, brand_name)
    if not tests and f.need != "commercial":
        regular = _sorted([t for t in pool if not t.special], brand_name)
        special = _sorted([t for t in pool if t.special], brand_name)
        names = [t.name for t in regular]
        # Grouped by layers: "which tanks do you have, in how many layers?" was a
        # real question, and this answers it from the sheet.
        by_layers: dict[int, list[str]] = {}
        for t in regular:
            by_layers.setdefault(t.layers, []).append(t.model)
        grouped = "; ".join(f"{n} layer: {', '.join(m)}" for n, m in sorted(by_layers.items()))
        other = ", ".join(f"{t.model} ({t.layers} layer)" for t in special)
        return {"status": "catalogue", **brand_info, "models": names,
                "other_models": [t.name for t in special],
                **({"ignored": ignored} if ignored else {}),
                "message": f"{brand_name or 'All brands'} home tanks by layers — {grouped}. "
                           + (f"Also, for commercial or special use: {other}. " if other else "")
                           + "If the customer has not said a capacity, ask it, then look up "
                             "again."}

    def matching(candidates):
        return [t for t in candidates if all(test(t) for test in tests.values())]

    regular = matching([t for t in pool if not t.special])
    special = matching([t for t in pool if t.special])
    if f.need == "commercial":
        hits = _sorted(regular + special, brand_name)
    else:
        hits = _sorted(regular or special, brand_name)
    if hits:
        return _listing(hits, f.label(), ignored, brand_info)
    return _no_match(pool, f, tests, brand_info, ignored)


def _not_in_brand(catalog, model_key, brand_name, f, tests, brand_info) -> dict:
    others = [t for t in catalog.tanks if t.key == model_key]
    model_name = others[0].model
    twins = [t for t in catalog.by_brand(brand_name)
             if any(t.twin_key() == o.twin_key() for o in others)]
    if twins:
        twin = twins[0]
        return {"status": "not_in_brand", **brand_info, "asked": f"{brand_name} {model_name}",
                "same_tank": _card(twin, True),
                "message": f"{brand_name} does not sell {model_name}; the same tank in "
                           f"{brand_name} is {twin.name}. {describe(twin)}. "
                           f"Layers: {', '.join(twin.layer_names) or 'not listed'}. "
                           f"Features: {'; '.join(twin.features) or 'not listed'}."}
    ordered = _sorted(others, None)
    return {"status": "not_in_brand", **brand_info, "asked": f"{brand_name} {model_name}",
            "other_brands": [_card(o, False) for o in ordered],
            "message": f"{brand_name} does not sell {model_name}. It is available as "
                       f"{'; '.join(describe(o) for o in ordered)}. "
                       "Ask the customer if that brand is fine."}


def _model_not_available(catalog, tank, failed, f, tests, brand_name, brand_info,
                         ignored) -> dict:
    parts = []
    out: dict = {"status": "not_available", **brand_info, "asked": f"{tank.name}, {f.label()}",
                 "tank": _card(tank, False)}
    if "capacity" in failed:
        near = _closest(list(tank.capacities), f.cap)
        out["closest_capacities"] = near
        near_text = " or ".join(str(c) for c in (near["lower"], near["higher"]) if c)
        parts.append(f"{tank.name} does not come in {_cap_label(f.cap)}; it comes in "
                     f"{_cap_text(tank.capacities)}. Closest: {near_text} litre")
    if "colour" in failed:
        parts.append(f"{tank.name} comes only in {' / '.join(tank.colours)}")
    for name in failed:
        if name == "layers":
            parts.append(f"{tank.name} is {tank.layers} layer")
        elif name == "guarantee":
            parts.append(f"{tank.name} has {_guarantee_text(tank)}")
        elif name == "mould":
            parts.append(f"{tank.name} is {tank.mould} moulded")
        elif name == "need":
            parts.append(f"{tank.name} is not listed for {f.need.replace('_', ' ')}")
    parts.append(f"For reference, {describe(tank)}")  # the asked tank in full
    # Other tanks that do have everything that was asked.
    alts = [t for t in _pool(catalog, brand_name)
            if t is not tank and all(test(t) for test in tests.values())]
    alts = [t for t in alts if not t.special] or alts
    alts = sorted(alts, key=lambda t: (abs(t.layers - tank.layers), t.layers, t.model))[:3]
    if alts:
        out["alternatives"] = [_card(t, False) for t in alts]
        parts.append(f"Tanks with {f.label()}: {'; '.join(describe(t) for t in alts)}")
    out["message"] = ". ".join(parts) + ". Tell the customer briefly and ask what they prefer."
    if ignored:
        out["ignored"] = ignored
    return out


def _no_match(pool, f, tests, brand_info, ignored) -> dict:
    """Nothing has everything asked: say which part fails and what is closest."""
    out: dict = {"status": "no_match", **brand_info, "asked": f.label()}
    parts = [f"No {brand_info['brand'] if brand_info['brand'] != 'all' else ''} tank has "
             f"all of: {f.label()}".replace("  ", " ")]
    others = {k: v for k, v in tests.items() if k != "capacity"}
    if "capacity" in tests:
        everything = [t for t in pool if all(test(t) for test in others.values())]
        regular = [t for t in everything if not t.special]
        candidates = regular or everything
        caps = sorted({c for t in candidates for c in t.capacities})
        near = _closest(caps, f.cap) if caps else {"lower": None, "higher": None}
        if regular and near["higher"] is None:
            # Bigger than any home tank (7000 L): the large tanks are the only way up.
            candidates = everything
            caps = sorted({c for t in candidates for c in t.capacities})
        if caps:
            near = _closest(caps, f.cap)
            out["closest_capacities"] = near
            options = []
            for c in (near["lower"], near["higher"]):
                if c:
                    names = [t.name for t in _sorted(candidates, None) if c in t.capacities]
                    options.append({"capacity": c, "tanks": names[:MAX_RESULTS]})
                    parts.append(f"At {c} litre: {', '.join(names[:MAX_RESULTS])}")
            out["options"] = options
            brand_word = brand_info["brand"] if brand_info["brand"] != "all" else "any"
            parts[0] = f"No {brand_word} tank comes in {_cap_label(f.cap)}"
            if others:
                parts[0] += f" with {Filters(**{**f.__dict__, 'cap': None}).label()}"
            parts[0] += ". Closest options"
    if "capacity" not in tests or not out.get("options"):
        # Drop one filter at a time and report what each relaxation gives.
        relaxed = []
        for name in tests:
            rest = [t for k, t in tests.items() if k != name]
            hits = [t for t in pool if all(test(t) for test in rest)]
            hits = _sorted([t for t in hits if not t.special] or hits, None)
            if hits:
                relaxed.append({"without": name, "tanks": [t.name for t in hits[:3]]})
                parts.append(f"Without the {name}: {', '.join(t.name for t in hits[:3])}")
        out["relaxed"] = relaxed
        values = _available_values(pool, tests)
        if values:
            out["available"] = values
            parts.append("Available " + "; ".join(f"{k}: {v}" for k, v in values.items()))
    out["message"] = ". ".join(parts) + ". Tell the customer briefly and ask what they prefer."
    if ignored:
        out["ignored"] = ignored
    return out


def _available_values(pool, tests) -> dict:
    out = {}
    if "layers" in tests:
        out["layers"] = ", ".join(str(n) for n in sorted({t.layers for t in pool}))
    if "guarantee" in tests:
        out["guarantee"] = ", ".join(
            sorted({f"{t.guarantee} years" if t.guarantee else "no guarantee" for t in pool}))
    return out


# ---------------------------------------------------------------------------
# Compact replies (POST /tanks?compact=1, what the bot's tool uses)
# ---------------------------------------------------------------------------

# Everything the bot needs is in "message"; these repeat it as structured data
# (or are internal) and only cost tokens on every later turn of the call.
_COMPACT_KEEP = ("status", "brand", "message", "closest_capacities", "ignored", "heard")


def compact(result: dict) -> dict:
    """The same answer without the duplicate data cards.

    Nothing the bot can say is lost: every fact on a card is also in the
    message, except the features in a comparison, which are kept per tank.
    """
    # Detailed answers (one tank, a comparison, the same tank under another name)
    # stay whole. Measured with the agent's LLM on "which layers does Granito
    # have?": the full reply got the layer names 8/8; with the card removed 1/8,
    # with the list kept but the message text trimmed 0/4. Only internal fields go.
    cards = list(result.get("tanks", [])) + [result[k] for k in ("same_tank",) if k in result]
    if any(c.get("layer_names") is not None for c in cards):
        return {k: v for k, v in result.items() if k not in _INTERNAL}
    return {k: result[k] for k in _COMPACT_KEEP if k in result}


_INTERNAL = ("brand_reason", "asked", "count")
