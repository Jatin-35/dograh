"""Vectus bath-ware, household and garden products, from data/products.json.

    find(catalog, product=, model=, item=, category=)  ->  {status, message, ...}

The bot sends what the caller said ("seat cover", "chic dual angle cock",
"balti 16 litre") and reads ``message`` back. Statuses:

    found           one model, or one tap item across the series that have it
    product         a product with its models (the caller named no model)
    model_not_found the product is sold but that model is not in the list
    item_not_in_model  that tap series does not make that item
    partial         one of the above, plus a word the list does not cover
                    ("brass tap"): confirm only the Vectus part
    multiple        two or more products in one ask ("bucket aur mug"), each answered
    category        a category with its products
    catalogue       everything, by category
    not_sold        plumbing supply pipes and fittings (PPR, CPVC, ...)
    ambiguous       a model name several products share ("Casper"): ask which
    use_tank_lookup a water tank question, which /tanks answers
    clarify         a bare "pipe": some are sold, some are not, so ask which
    no_match        not in the list (or "wash basin", "geyser"): never say yes or no

Prices are deliberately not in the data; the bot never quotes one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from rapidfuzz import fuzz

from .tanks import MODEL_ALIASES as TANK_MODEL_ALIASES
from .text import HINDI_FILLER, norm, translit


class ProductCatalogError(Exception):
    pass


# Words that wrap a product name in a question and are never part of one.
_FILLER = set(
    "vectus ka ki ke ko kya hai hain he chahiye chaiye chahie mujhe muje humko hume hamein "
    "batao bataiye bata dijiye do de please pls ji sir madam aap apke aapke koi kaun konsa "
    "kaunsa kon sa wala wali wale the a an of for in me mein about details detail info "
    "information warranty guarantee garanti price rate cost kitna kitne kitni available "
    "milta milti milega milegi hota hoti sell sells selling do you have want need buy "
    "model models series range type types product products item items new and or bhi "
    "se par pe banata banate banati banaate bechte bechta milte deta dete hai kya aapka "
    "sabse acha achha accha best good quality wala chaiye bataye batao hum ham maine "
    "dekha suna puchna poochna tha thi the jo vo woh ye yeh is us what which how much does "
    "can could would will get tell is are am there any some your you please looking i we "
    "aur jaise jaisa jaisi like etc etcetera wagaira wagera waghera vagaira also bhi type "
    "cheez cheeze cheezein saman samaan requirement requirment chahta chahti chahte".split()
) | HINDI_FILLER | {norm(w) for w in "चाहिए क्या मुझे हमें आपके आप बताइए बताओ".split()}

# Words that leave the meaning alone: sizes, colours, where it goes. "balti 16
# litre" is still the bucket; "brass tap" is not a Vectus tap.
_NEUTRAL = set(
    "plastic ptmt litre liter ltr lt l inch inche mm cm m meter metre mtr feet ft kg "
    "piece pc pcs set size bada badi bade chota choti chote big small large medium "
    "white ivory blue yellow red green grey gray black orange pink colour color "
    "bathroom kitchen ghar home house wala tank tanki".split()
)

# Spoken and written spellings of tap items -> the catalogue's wording.
_ITEM_ALIASES = {
    "bibcock": "bib cock", "bib tap": "bib cock", "bib cock tap": "bib cock",
    "angle valve": "angle cock", "angle tap": "angle cock", "angle cock tap": "angle cock",
    "pillar tap": "pillar cock", "swanneck": "swan neck", "swan neck tap": "swan neck",
    "long body bib cock": "long bib cock", "long body bibcock": "long bib cock",
    "bib cock nozzle body": "nozzle cock", "nozzle body": "nozzle cock", "nozzle tap": "nozzle cock",
    "two way": "2 way", "do way": "2 way", "2way": "2 way",
    "centre hole mixer": "center hole mixer",
    "wall mixer l bend": "wall mixer with l bend",
    "wall mixer without telephone": "wall mixer non telephonic",
    "washing machine tap": "washing machine cock", "washing machine nal": "washing machine cock",
    "stop cock": "stop cock", "stopcock": "stop cock",
    # Hindi script, as the speech-to-text writes them
    "एंगल कॉक": "angle cock", "एंगल वाल्व": "angle cock", "बिब कॉक": "bib cock",
    "पिलर कॉक": "pillar cock", "स्वान नेक": "swan neck", "सिंक कॉक": "sink cock",
    "सिंक मिक्सर": "sink mixer", "बेसिन मिक्सर": "basin mixer", "वॉल मिक्सर": "wall mixer",
}


# Said without the exact kind: matched against every item that contains it.
_GENERIC_ITEMS = ("wall mixer", "mixer", "stop cock")

# A bare "pipe": Vectus sells some pipes and not others, so ask which.
_PIPE_WORDS = {"pipe", "paip", "pip", "pipes", "पाइप"}

# Joining words inside a name: "washing machine ka pipe", "pedal wala dustbin".
# Dropped on both sides, so "doodh ki balti" still matches its alias.
_JOIN = {"ka", "ki", "ke", "wala", "wali", "wale", "vala", "vali", "vale", "waala",
         "का", "की", "के", "वाला", "वाली", "वाले"}


def _stem(token: str) -> str:
    # "taps" -> "tap", "cisterns" -> "cistern"; both sides are stemmed alike.
    return token[:-1] if len(token) > 3 and token.endswith("s") and not token.endswith("ss") else token


# The question is stemmed before it is compared, so the word lists are too.
_FILLER |= {_stem(w) for w in _FILLER}
_NEUTRAL |= {_stem(w) for w in _NEUTRAL}


@lru_cache(maxsize=8192)  # pure, and called for every alias of every product
def _key(text: str | None) -> str:
    # "Pro+" is not "Pro": the plus would otherwise vanish with the punctuation.
    s = norm(str(text).replace("+", " plus ") if text else text)
    s = s.replace("-", " ")
    return " ".join(_stem(t) for t in s.split() if t not in _JOIN)


@lru_cache(maxsize=8192)
def _item_key(text: str) -> str:
    s = f" {_key(text)} "
    for variant in sorted(_ITEM_ALIASES, key=len, reverse=True):
        s = s.replace(f" {_key(variant)} ", f" {_key(_ITEM_ALIASES[variant])} ")
    return " ".join(s.split())


def _find_phrase(phrase: str, text: str) -> bool:
    return bool(phrase) and f" {phrase} " in f" {text} "


@dataclass
class Model:
    name: str
    details: str
    aliases: list[str]
    warranty: str | None = None
    items: list[str] = field(default_factory=list)  # tap series only

    def card(self, product: "Product") -> dict:
        out = {"model": self.name, "details": self.details}
        warranty = self.warranty or product.warranty
        if warranty:
            out["warranty"] = warranty
        if self.items:
            out["items"] = self.items
        return out


@dataclass
class Product:
    name: str
    category: str
    aliases: list[str]
    models: list[Model]
    warranty: str | None = None
    features: list[str] = field(default_factory=list)
    kind: str | None = None


@dataclass
class ProductCatalog:
    products: list[Product]
    categories: dict[str, dict]  # name -> {"aliases": [...], "products": [...]}
    not_sold_aliases: list[str]
    not_sold_message: str
    source: str
    # phrase key -> (kind, target); the longest phrase in the question wins
    phrases: list[tuple[str, str, object]] = field(default_factory=list)
    tap_items: list[str] = field(default_factory=list)

    @property
    def taps(self) -> Product | None:
        return next((p for p in self.products if p.kind == "taps"), None)


_WARRANTY = re.compile(r"^\d+ (months|years)$")
_PRICE = re.compile(r"₹|\brs\.?\s*\d|\brupees?\b|\bm\.?r\.?p\b", re.I)


def _validate(products: list["Product"], not_sold: dict) -> None:
    """Refuse a file with a hole in it, naming the product, as /tanks does."""
    if not products:
        raise ProductCatalogError("the file has no products")
    seen: set[str] = set()
    for p in products:
        where = f"{p.category} / {p.name}"
        if not p.name.strip() or p.name in seen:
            raise ProductCatalogError(f"{where}: blank or repeated product name")
        seen.add(p.name)
        if not p.models:
            raise ProductCatalogError(f"{where}: no models")
        names = [m.name for m in p.models]
        if len(set(names)) != len(names):
            raise ProductCatalogError(f"{where}: a model name appears twice")
        for w in [p.warranty, *(m.warranty for m in p.models)]:
            if w is not None and not _WARRANTY.match(w):
                raise ProductCatalogError(f"{where}: warranty {w!r} is not like '24 months'")
        for m in p.models:
            if not m.name.strip() or not m.details.strip():
                raise ProductCatalogError(f"{where}: model {m.name!r} has no name or details")
            if _PRICE.search(m.details):
                raise ProductCatalogError(f"{where} / {m.name}: details carry a price")
        if p.kind == "taps" and not all(m.items and (m.warranty or p.warranty) for m in p.models):
            raise ProductCatalogError(f"{where}: every tap series needs items and a warranty")
    if not not_sold.get("aliases") or not not_sold.get("message"):
        raise ProductCatalogError("not_sold needs aliases and a message")


def load_catalog(path: str | Path) -> ProductCatalog:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        products, categories = [], {}
        for cat in raw["categories"]:
            names = []
            for p in cat["products"]:
                models = [Model(name=m["name"], details=m["details"],
                                aliases=[m["name"], *m.get("aliases", [])],
                                warranty=m.get("warranty"), items=m.get("items", []))
                          for m in p["models"]]
                if not models:
                    raise ProductCatalogError(f"{p['name']} has no models")
                products.append(Product(name=p["name"], category=cat["name"],
                                        aliases=[p["name"], *p.get("aliases", [])],
                                        models=models, warranty=p.get("warranty"),
                                        features=p.get("features", []), kind=p.get("kind")))
                names.append(p["name"])
            categories[cat["name"]] = {"aliases": [cat["name"], *cat.get("aliases", [])],
                                       "products": names}
        not_sold = raw["not_sold"]
        not_listed = raw.get("not_listed", {}).get("aliases", [])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ProductCatalogError(f"cannot read product catalogue {path}: {exc}") from exc
    _validate(products, not_sold)

    catalog = ProductCatalog(products=products, categories=categories,
                             not_sold_aliases=not_sold["aliases"],
                             not_sold_message=not_sold["message"], source=str(path))
    phrases: list[tuple[str, str, object]] = []
    for p in products:
        phrases += [(_key(a), "product", p) for a in p.aliases]
    for name, cat in categories.items():
        phrases += [(_key(a), "category", name) for a in cat["aliases"]]
    phrases += [(_key(a), "not_sold", None) for a in not_sold["aliases"]]
    phrases += [(_key(a), "unlisted", None) for a in not_listed]
    if catalog.taps:
        items = sorted({_item_key(i) for m in catalog.taps.models for i in m.items})
        catalog.tap_items = items
        phrases += [(i, "item", i) for i in items]
        phrases += [(g, "item", g) for g in _GENERIC_ITEMS]
        phrases += [(_key(v), "item", _item_key(c)) for v, c in _ITEM_ALIASES.items()
                    if _item_key(c) in items]
    # A model name said on its own ("Kaveri", "ball cock"). A name several
    # products share ("Silk", "Casper") is kept as a list and asked back.
    by_model: dict[str, list] = {}
    for p in products:
        for m in p.models:
            for a in m.aliases:
                k = _key(a)
                if len(k) >= 3 and (p, m) not in by_model.setdefault(k, []):
                    by_model[k].append((p, m))
    phrases += [(k, "model", tuple(v)) for k, v in by_model.items()]
    catalog.phrases = sorted({(k, kind, id(t)): (k, kind, t) for k, kind, t in phrases if k}.values(),
                             key=lambda x: len(x[0]), reverse=True)
    return catalog


# ---------------------------------------------------------------------------


def _texts(raw: str) -> list[str]:
    """The question as matched: normalised, and romanised when in Devanagari."""
    out = [_item_key(raw)]
    if out[0] and not out[0].isascii():
        out.append(_item_key(translit(raw)))
    return [t for t in out if t]


def _strip_filler(text: str) -> str:
    return " ".join(t for t in text.split() if t not in _FILLER)


def _phrase_hits(catalog: ProductCatalog, text: str) -> list[tuple[str, str, object]]:
    """Every catalogue phrase in the text, longest first, not overlapping."""
    hits, taken = [], f" {text} "
    for key, kind, target in catalog.phrases:
        if f" {key} " in taken:
            hits.append((key, kind, target))
            taken = taken.replace(f" {key} ", " | ", 1)
    return hits


def _fuzzy(catalog: ProductCatalog, text: str, exact_words: set[str]):
    """A misheard name ("sistern", "helth fasset", "jet spre"): n-grams vs aliases.

    Returns (key, kind, target, gram) or None. Two-word names may be a little
    further off than one-word names, which collide too easily. A word that
    already matched exactly must stay in the name: "sonic tap" is not "sony
    tap", "plastic balti" is not "plastic basin".
    """
    tokens = _strip_filler(text).split()
    for n in (3, 2, 1):  # the longest name that fits wins: "angel cock" over "cock"
        best = (0.0, None)
        for i in range(len(tokens) - n + 1):
            gram = " ".join(tokens[i:i + n])
            if len(gram) < 4:
                continue
            tank = _TANK_WORDS & set(gram.split())
            for key, kind, target in catalog.phrases:
                if len(key) < 4 or len(key.split()) != n:
                    continue
                if kind not in ("product", "item", "model") or (kind == "model" and len(target) > 1):
                    continue
                if tank and not tank & set(key.split()):
                    continue  # "water tank" is never "water tap"
                if not (exact_words & set(gram.split())) <= set(key.split()):
                    continue
                if n == 1 and not _same_onset(gram, key):
                    continue  # "fridge" is not "ridge"
                if min(len(gram), len(key)) < 0.8 * max(len(gram), len(key)):
                    continue  # "ss tap" only fits inside "brass tap"
                score = fuzz.ratio(gram, key)
                if score >= (80 if n > 1 else 85) and score > best[0]:
                    best = (score, (key, kind, target, gram))
        if best[1]:
            return best[1]
    return None


_ONSETS = ("cks", "jz", "vw", "fp")  # first sounds speech-to-text swaps


def _same_onset(a: str, b: str) -> bool:
    if a[:1] == b[:1]:
        return True
    return any(a[:1] in g and b[:1] in g for g in _ONSETS)


def _match_model(product: Product, text: str) -> Model | None:
    text = _strip_filler(text)
    if not text:
        return None
    best: tuple[int, Model | None] = (0, None)
    for m in product.models:
        for a in m.aliases:
            k = _key(a)
            if _find_phrase(k, text) and len(k) > best[0]:
                best = (len(k), m)
    if best[1]:
        return best[1]
    # A name that only appears in the details: "Ujjwal" dustbin, "Sudhakar" tasla, "Maria" planter.
    for m in product.models:
        if any(_find_phrase(t, _key(m.details)) for t in text.split() if len(t) >= 4):
            return m
    scored = [(fuzz.ratio(text, _key(a)), m) for m in product.models for a in m.aliases if len(a) >= 4]
    score, m = max(scored, key=lambda x: x[0], default=(0, None))
    return m if score >= 85 else None


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _join(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _warranty_line(product: Product, models: list[Model] | None = None) -> str:
    """" Warranty is 36 months." or, when it differs, which models get what."""
    models = product.models if models is None else models
    groups: dict[str, list[str]] = {}
    for m in models:
        w = m.warranty or product.warranty
        if w:
            groups.setdefault(w, []).append(m.name)
    if not groups:
        return ""
    if len(groups) == 1:
        return f" Warranty is {next(iter(groups))}."
    order = sorted(groups, key=lambda w: -int(w.split()[0]) * (12 if "year" in w else 1))
    return " Warranty: " + "; ".join(f"{w} for {_join(groups[w])}" for w in order) + "."


# "Ganga tank" is the tank brand, not the Ganga bucket.
_TANK_BRANDS = {"ganga", "waterwell", "water well", "vectus"}

_TANK_WORDS = {"tank", "tanki", "tanky", "टैंक", "टंकी", "टंकि"}

_NO_MATCH = ("This item is not in the Vectus product list I have. Do not say it is or is not "
             "available; say the Area Manager will confirm, and offer to share their number.")


_DETAIL_FULL = {"full", "detail", "details", "detailed", "features", "feature", "more", "yes", "true", "1"}


_SPLIT = re.compile(r"\s*(?:,|;|&|\baur\b|\band\b|\bor\b|\bya\b|\bphir\b|और|या)\s*", re.I)
_ANSWERED = ("found", "product", "partial", "model_not_found", "item_not_in_model")


def _several(catalog: ProductCatalog, text: str) -> dict | None:
    """"bucket aur mug", "seat cover and flush tank": one answer per product."""
    # A name with "and" in it ("Racks and baskets", "Jug, crate and drum") is one product.
    said = f" {_key(text)} "
    if any(f" {_key(n)} " in said for p in catalog.products for n in p.aliases if _SPLIT.search(n)):
        return None
    parts = [x for x in _SPLIT.split(text) if x and x.strip()]
    if len(parts) < 2:
        return None
    results, seen = [], set()
    for part in parts:
        r = _find(catalog, product=part)
        key = (r.get("product"), r.get("model"), r.get("item"))
        if r["status"] in _ANSWERED and key not in seen:
            seen.add(key)
            results.append(r)
    if len(results) < 2:
        return None
    return {"status": "multiple", "products": [r.get("product") for r in results],
            "results": results, "message": " ".join(r["message"] for r in results)}


def find(catalog: ProductCatalog, product=None, model=None, item=None, category=None,
         detail=None) -> dict:
    """Answer one question. ``detail="full"`` adds the product's features."""
    result = None
    if product is not None and model in (None, "") and item in (None, ""):
        result = _several(catalog, str(product))
    if result is None:
        result = _find(catalog, product=product, model=model, item=item, category=category)
    prod = next((p for p in catalog.products if p.name == result.get("product")), None)
    if (str(detail or "").strip().lower() in _DETAIL_FULL and prod and prod.features
            and result["status"] in ("found", "product", "partial")):
        result = {**result, "features": prod.features,
                  "message": f"{result['message']} Features: {_join(prod.features)}."}
    return result


def _find(catalog: ProductCatalog, product=None, model=None, item=None, category=None) -> dict:
    asked = {k: str(v) for k, v in (("product", product), ("model", model), ("item", item),
                                    ("category", category)) if v not in (None, "")}
    whole = " ".join(asked.get(k, "") for k in ("product", "item", "model"))
    texts = _texts(whole)
    cat_texts = _texts(asked.get("category", ""))

    if not texts and not cat_texts:
        return _catalogue(catalog)

    hits: list[tuple[str, str, object]] = []
    matched = texts[-1] if texts else ""
    for t in texts:
        hits = _phrase_hits(catalog, t)
        if hits:
            matched = t
            break
    kinds = {kind for _, kind, _ in hits}
    prod = next((t for _, k, t in hits if k == "product"), None)
    tap_item = next((t for _, k, t in hits if k == "item"), None)
    model_hit = next(((key, t) for key, k, t in hits if k == "model"), None)
    prod_len = max((len(key) for key, k, _ in hits if k in ("product", "item")), default=0)

    if "not_sold" in kinds and not prod and not tap_item:
        return {"status": "not_sold", "message": catalog.not_sold_message}

    # "wash basin", "mixer grinder": longer than the product word inside them.
    unlisted = max((len(key) for key, k, _ in hits if k == "unlisted"), default=0)
    if unlisted and unlisted > prod_len and not (model_hit and len(model_hit[0]) > unlisted):
        return {"status": "no_match", "heard": whole.strip(), "message": _NO_MATCH}

    # A misheard name longer than any exact word ("angel cock" over "cock",
    # "helth faucet" over "faucet", "jet spre" over "jet") replaces it.
    exact = max((len(key) for key, k, _ in hits if k in ("product", "item", "model")), default=0)
    exact_words = {w for key, _, _ in hits for w in key.split()}
    fz = _fuzzy(catalog, texts[-1], exact_words) if texts else None
    if fz and len(fz[0]) > exact:
        key, kind, target, gram = fz
        prod, tap_item, model_hit = None, None, None
        if kind == "product":
            prod = target
        elif kind == "item":
            tap_item = target
        else:
            model_hit = (key, target)
        prod_len = len(key) if kind != "model" else 0
        if matched != texts[-1]:  # the misheard words are in the romanised text
            matched = texts[-1]
            hits = _phrase_hits(catalog, matched)
        hits.append((gram, kind, target))  # the misheard words are consumed

    words = set(" ".join(texts).split())
    tank_model = any(_find_phrase(k, t) for t in texts for k in _TANK_MODELS)
    if not prod and not tap_item and (words & _TANK_WORDS or tank_model) and (
            not model_hit or model_hit[0] in _TANK_BRANDS):
        return {"status": "use_tank_lookup",
                "message": "This is a water tank question: use vectus_tank_lookup instead."}

    if not prod and not tap_item and not model_hit and words & _PIPE_WORDS:
        return {"status": "clarify", "products": ["Garden pipe", "Washing machine pipe", "PVC waste pipe"],
                "message": ("Vectus sells garden pipes, washing machine pipes and PVC waste pipes, "
                            "but not plumbing supply pipes like PPR or CPVC. Ask which pipe they need.")}

    # A model named on its own, or more specifically than the product word
    # ("ball cock" over "cock"): that model's product.
    if model_hit and len(model_hit[0]) > prod_len and not (
            prod and any(p is prod for p, _ in model_hit[1])):
        owners = model_hit[1]
        if len(owners) == 1:
            p, m = owners[0]
            return _flag_leftover(_product(p, model_hit[0], {**asked, "model": m.name}),
                                  catalog, matched, hits)
        if not prod and not tap_item:
            return _ambiguous(model_hit[0], owners)

    # A tap item without a product name ("angle cock") is a tap question.
    if tap_item and (prod is None or prod is catalog.taps):
        return _flag_leftover(_tap_item(catalog, tap_item, _rest(texts, hits), asked),
                              catalog, matched, hits)

    if prod:
        # A group's word that is also one of its models ("drum", "pedal bin",
        # "syphon") names that model; the product's own name ("mug") does not.
        own = {_key(a) for m in prod.models for a in m.aliases} - {_key(prod.name)}
        kept = [h for h in hits if not (h[1] == "product" and h[2] is prod and h[0] in own)]
        rest = _key(asked["model"]) if "model" in asked else _rest(texts, kept)
        return _flag_leftover(_product(prod, rest, asked), catalog, matched, hits)

    cat = next((t for _, k, t in hits if k == "category"), None)
    for t in cat_texts:
        cat = cat or next((tg for _, k, tg in _phrase_hits(catalog, t) if k == "category"), None)
    if cat:
        return _category(catalog, cat)
    return {"status": "no_match", "heard": whole.strip() or asked.get("category"), "message": _NO_MATCH}


def _flag_leftover(result: dict, catalog: ProductCatalog, text: str, hits) -> dict:
    """A word the answer does not account for ("brass" tap, "shoe" rack) turns
    a yes into a partial yes: confirm the Vectus part, not the rest."""
    if result["status"] not in ("found", "product", "item_not_in_model"):
        return result
    used = {w for key, _, _ in hits for w in key.split()}
    prod = next((p for p in catalog.products if p.name == result.get("product")), None)
    model = next((m for m in prod.models if m.name == result.get("model")), None) if prod else None
    if model:
        used |= {w for a in model.aliases for w in _key(a).split()} | set(_key(model.details).split())
    if result.get("item"):
        used |= set(_item_key(result["item"]).split())
    left = [w for w in text.split()
            if w not in used and w not in _FILLER and w not in _NEUTRAL and w not in _TANK_WORDS
            and not any(ch.isdigit() for ch in w) and len(w) > 1]
    if not left:
        return result
    said = " ".join(dict.fromkeys(left))
    return {**result, "status": "partial", "unmatched": said,
            "message": (f"Careful: \"{said}\" is not in the Vectus product list, so do not say "
                        f"Vectus has it. What the list does have: {result['message']} Offer "
                        f"this instead, or say the Area Manager will confirm.")}


def _rest(texts: list[str], hits) -> str:
    """The question minus the product words: what is left names the model."""
    text = f" {texts[-1] if texts else ''} "
    for key, kind, _ in hits:
        if kind == "model":
            continue
        text = text.replace(f" {key} ", " ", 1)
    return " ".join(text.split())


def _product(prod: Product, rest: str, asked: dict) -> dict:
    m = _match_model(prod, rest)
    if m is None and len(prod.models) == 1 and not (_strip_filler(rest) and "model" in asked):
        m = prod.models[0]  # nothing to choose between: give its details
    if m:
        card = m.card(prod)
        msg = f"Vectus {m.name} ({prod.name}): {m.details}."
        if card.get("warranty"):
            msg += f" Warranty is {card['warranty']}."
        if m.items:
            msg += f" This series makes: {_join(m.items)}."
        return {"status": "found", "category": prod.category, "product": prod.name, **card,
                "message": msg}
    names = [x.name for x in prod.models]
    base = {"category": prod.category, "product": prod.name, "models": names}
    if prod.warranty:
        base["warranty"] = prod.warranty
    if _strip_filler(rest) and "model" in asked:
        return {"status": "model_not_found", **base, "heard": asked["model"],
                "message": (f"Vectus does sell {prod.name}, but {asked['model']} is not one of "
                            f"its models. The models are {_join(names)}.{_warranty_line(prod)} "
                            f"Do not guess; ask which one they mean, or say the Area Manager "
                            f"will confirm.")}
    return {"status": "product", **base,
            "message": (f"Yes, Vectus sells {prod.name}. Models: {_join(names)}."
                        f"{_warranty_line(prod)} Ask which model they want, then look it up "
                        f"again for its details.")}


def _tap_item(catalog: ProductCatalog, item: str, rest: str, asked: dict) -> dict:
    taps = catalog.taps
    has = [m for m in taps.models if item in {_item_key(i) for i in m.items}]
    if not has:  # "wall mixer" covers both kinds
        has = [m for m in taps.models if any(_find_phrase(item, _item_key(i)) for i in m.items)]
    m = _match_model(taps, _key(asked["model"]) if "model" in asked else rest)
    if m is None and len(has) == 1:
        m = has[0]  # only one series makes it ("sony tap")
    spoken = item.replace("2 way", "two way")
    a = "an" if spoken[0] in "aeiou" else "a"
    if m and m in has:
        card = m.card(taps)
        return {"status": "found", "category": taps.category, "product": taps.name, "item": spoken,
                **card,
                "message": (f"Yes, the Vectus {m.name} series has {a} {spoken}. {_cap(m.details)}. "
                            f"Warranty is {card['warranty']}. The {m.name} series makes: "
                            f"{_join(m.items)}.")}
    series = [{"model": x.name, "warranty": x.warranty or taps.warranty} for x in has]
    if m:
        return {"status": "item_not_in_model", "category": taps.category, "product": taps.name,
                "item": spoken, "model": m.name, "series_with_item": series,
                "message": (f"The {m.name} series does not have {a} {spoken}. It comes in "
                            f"{_join([x.name for x in has])}.{_warranty_line(taps, has)}")}
    war = _warranty_line(taps, has)
    return {"status": "found", "category": taps.category, "product": taps.name, "item": spoken,
            "series_with_item": series,
            "message": (f"Yes, Vectus makes a PTMT {spoken}, in these series: "
                        f"{_join([x.name for x in has])}.{war}")}


def _ambiguous(heard: str, owners) -> dict:
    options = [{"product": p.name, "model": m.name} for p, m in owners]
    prods = list(dict.fromkeys(p.name for p, _ in owners))
    return {"status": "ambiguous", "heard": heard, "options": options,
            "message": (f"Vectus uses the name {heard.title()} for more than one product: "
                        f"{_join(prods)}. Ask which product they mean.")}


def _category(catalog: ProductCatalog, name: str) -> dict:
    prods = catalog.categories[name]["products"]
    return {"status": "category", "category": name, "products": prods,
            "message": f"Vectus {name} products: {_join(prods)}. Ask which one they need."}


def _catalogue(catalog: ProductCatalog) -> dict:
    cats = {n: c["products"] for n, c in catalog.categories.items()}
    said = "; ".join(f"{n}: {_join(p)}" for n, p in cats.items())
    return {"status": "catalogue", "categories": cats,
            "message": f"Besides water tanks, Vectus sells {said}. Ask what they are looking for."}


# The tank lookup's spoken model names, minus the ones a bath-ware product also
# uses (Silk, Smart, Pro), which stay here and are asked back.
def _tank_models() -> set[str]:
    return {_key(k) for k in TANK_MODEL_ALIASES} - {"pro", "silk", "smart"}


_TANK_MODELS = _tank_models()


# ---------------------------------------------------------------------------
# Compact replies (POST /products?compact=1, what the bot's tool uses)
# ---------------------------------------------------------------------------

# Everything the bot can say is in "message"; the lists and cards repeat it and
# would cost tokens on every later turn of the call. Names stay, so the bot can
# pass them back on the next lookup.
_COMPACT_KEEP = ("status", "message", "product", "model", "item", "category", "heard", "unmatched")


def compact(result: dict) -> dict:
    """The same answer without the data that only repeats the message."""
    return {k: result[k] for k in _COMPACT_KEEP if k in result}
