"""Black-box QA for POST /products: how callers really ask, and what must come back.

Each case is what the bot would send (often the caller's whole phrase as
``product``) and the expected status plus product/model/item. Run directly to
print a report; tests/test_products_qa.py runs the same table under pytest.
"""

from __future__ import annotations

# (request, expected): expected keys are checked only when present.
CASES: list[tuple[dict, dict]] = [
    # ── Toilet seat cover ──
    ({"product": "toilet seat cover"}, {"status": "product", "product": "Toilet seat cover"}),
    ({"product": "mujhe seat cover chahiye"}, {"status": "product", "product": "Toilet seat cover"}),
    ({"product": "commode ka cover milega kya"}, {"product": "Toilet seat cover"}),
    ({"product": "kya Vectus toilet seat cover banata hai"}, {"product": "Toilet seat cover"}),
    ({"product": "western toilet seat"}, {"product": "Toilet seat cover"}),
    ({"product": "soft close seat cover"}, {"status": "found", "model": "Hush"}),
    ({"product": "seat cover jet wala"}, {"status": "found", "model": "Pro with jet"}),
    ({"product": "seat cover without jet"}, {"status": "found", "model": "Pro without jet"}),
    ({"product": "seat cover", "model": "silk"}, {"status": "found", "model": "Silk", "warranty": "24 months"}),
    ({"product": "toylet seat cover"}, {"product": "Toilet seat cover"}),
    ({"product": "sit cover"}, {"product": "Toilet seat cover"}),
    ({"product": "टॉयलेट सीट कवर"}, {"product": "Toilet seat cover"}),
    ({"product": "कमोड कवर"}, {"product": "Toilet seat cover"}),
    # ── Cistern ──
    ({"product": "cistern"}, {"status": "product", "product": "Cistern tank"}),
    ({"product": "flush tank chahiye"}, {"product": "Cistern tank"}),
    ({"product": "toilet ka flush"}, {"product": "Cistern tank"}),
    ({"product": "dual flush cistern"}, {"status": "found", "model": "Chic"}),
    ({"product": "cistern", "model": "pro plus"}, {"status": "found", "model": "Pro+", "details": "8 litre, white"}),
    ({"product": "cistern", "model": "Pro+"}, {"status": "found", "model": "Pro+"}),
    ({"product": "cistern", "model": "pro"}, {"status": "found", "model": "Pro"}),
    ({"product": "centra cistern"}, {"status": "found", "model": "Centra"}),
    ({"product": "side push flush tank swift"}, {"status": "found", "model": "Swift"}),
    ({"product": "sistern tank"}, {"product": "Cistern tank"}),
    ({"product": "systan tank"}, {"product": "Cistern tank"}),
    ({"product": "फ्लश टैंक"}, {"product": "Cistern tank"}),
    ({"product": "सिस्टर्न"}, {"product": "Cistern tank"}),
    # ── Taps ──
    ({"product": "tap"}, {"status": "product", "product": "PTMT taps"}),
    ({"product": "bathroom ki tonti"}, {"product": "PTMT taps"}),
    ({"product": "nal chahiye"}, {"product": "PTMT taps"}),
    ({"product": "plastic tap"}, {"product": "PTMT taps"}),
    ({"product": "टोंटी"}, {"product": "PTMT taps"}),
    ({"product": "angle cock"}, {"status": "found", "item": "angle cock"}),
    ({"product": "angle valve"}, {"status": "found", "item": "angle cock"}),
    ({"product": "angel cock"}, {"status": "found", "item": "angle cock"}),
    ({"product": "bib cock"}, {"status": "found", "item": "bib cock"}),
    ({"product": "bibcock"}, {"status": "found", "item": "bib cock"}),
    ({"product": "bib cok"}, {"status": "found", "item": "bib cock"}),
    ({"product": "pillar cock"}, {"status": "found", "item": "pillar cock"}),
    ({"product": "pillar tap"}, {"status": "found", "item": "pillar cock"}),
    ({"product": "swan neck"}, {"status": "found", "item": "swan neck"}),
    ({"product": "swan nek"}, {"status": "found", "item": "swan neck"}),
    ({"product": "long body bib cock"}, {"status": "found", "item": "long bib cock"}),
    ({"product": "two way angle cock"}, {"status": "found", "item": "two way angle cock"}),
    ({"product": "2 way bib cock"}, {"status": "found", "item": "two way bib cock"}),
    ({"product": "washing machine tap"}, {"status": "found", "item": "washing machine cock"}),
    ({"product": "washing machine cock"}, {"status": "found", "item": "washing machine cock"}),
    ({"product": "sink mixer"}, {"status": "found", "item": "sink mixer"}),
    ({"product": "basin mixer"}, {"status": "found", "item": "basin mixer"}),
    ({"product": "wall mixer"}, {"status": "found", "item": "wall mixer"}),
    ({"product": "center hole mixer"}, {"status": "found", "item": "center hole mixer"}),
    ({"product": "centre hole mixer"}, {"status": "found", "item": "center hole mixer"}),
    ({"product": "nozzle cock"}, {"status": "found", "item": "nozzle cock"}),
    ({"product": "stop cock"}, {"status": "found", "item": "stop cock"}),
    ({"product": "chic dual angle cock"}, {"status": "found", "model": "Chic Dual", "item": "angle cock"}),
    ({"product": "tap", "model": "chic dual", "item": "angle cock"}, {"status": "found", "model": "Chic Dual"}),
    ({"product": "smart series bib cock"}, {"status": "found", "model": "Smart", "item": "bib cock"}),
    ({"product": "smart dual swan neck"}, {"status": "found", "model": "Smart Dual"}),
    ({"product": "casa series pillar cock"}, {"status": "found", "model": "Casa"}),
    ({"product": "casa two way bib cock"}, {"status": "item_not_in_model", "model": "Casa"}),
    ({"product": "tee series sink mixer"}, {"status": "item_not_in_model", "model": "Tee"}),
    ({"product": "eco basin mixer"}, {"status": "item_not_in_model", "model": "Eco"}),
    ({"product": "pro stop cock"}, {"status": "found", "model": "Pro"}),
    ({"product": "rhino tap"}, {"status": "found", "model": "Rhino", "warranty": "24 months"}),
    ({"product": "sony tap"}, {"status": "found", "model": "Sony", "warranty": "12 months"}),
    ({"product": "eco tap"}, {"status": "found", "model": "Eco", "warranty": "12 months"}),
    ({"product": "wave series"}, {"status": "found", "model": "Wave"}),
    ({"product": "renova"}, {"status": "found", "model": "Renova"}),
    ({"product": "sonic tap"}, {"status": "found", "model": "Sonic"}),
    ({"product": "presto"}, {"status": "found", "model": "Presto"}),
    ({"product": "tap", "model": "xyz"}, {"status": "model_not_found"}),
    ({"product": "एंगल कॉक"}, {"item": "angle cock"}),
    # ── Maria, sinks, showers, health faucets ──
    ({"product": "maria"}, {"status": "found", "product": "Maria basin mixer"}),
    ({"product": "single lever basin mixer"}, {"status": "found", "product": "Maria basin mixer"}),
    ({"product": "kitchen sink"}, {"status": "product", "product": "Kitchen sink"}),
    ({"product": "steel sink double bowl"}, {"product": "Kitchen sink"}),
    ({"product": "sink with drain board"}, {"status": "found", "model": "Silk single bowl with drain board"}),
    ({"product": "handmade sink"}, {"status": "found", "product": "Kitchen sink"}),
    ({"product": "kichan sink"}, {"product": "Kitchen sink"}),
    ({"product": "किचन सिंक"}, {"product": "Kitchen sink"}),
    ({"product": "shower"}, {"status": "product", "product": "Shower"}),
    ({"product": "fawara"}, {"product": "Shower"}),
    ({"product": "shawar"}, {"product": "Shower"}),
    ({"product": "hand shower"}, {"product": "Shower"}),
    ({"product": "skymist shower"}, {"status": "found", "model": "Skymist"}),
    ({"product": "शावर"}, {"product": "Shower"}),
    ({"product": "health faucet"}, {"status": "product", "product": "Health faucet"}),
    ({"product": "helth faucet"}, {"product": "Health faucet"}),
    ({"product": "muslim shower"}, {"product": "Health faucet"}),
    ({"product": "health faucet black"}, {"status": "found", "model": "Silk Black"}),
    ({"product": "jet spray"}, {"status": "found", "model": "Jet spray"}),
    ({"product": "jet spre"}, {"status": "found", "model": "Jet spray"}),
    ({"product": "जेट स्प्रे"}, {"model": "Jet spray"}),
    # ── Accessories, spares, filter, manhole ──
    ({"product": "bath accessories"}, {"status": "product", "product": "Bath accessories"}),
    ({"product": "soap dish"}, {"status": "found", "model": "Soap dish"}),
    ({"product": "towel rod"}, {"status": "found", "model": "Towel rod and towel ring"}),
    ({"product": "corner shelf"}, {"status": "found", "model": "Shelf"}),
    ({"product": "mirror cabinet"}, {"status": "found", "model": "Cabinet mirror"}),
    ({"product": "floor trap jali"}, {"status": "found", "model": "Grating"}),
    ({"product": "cockroach grating"}, {"status": "found", "model": "Grating"}),
    ({"product": "teflon tape"}, {"status": "found", "model": "PTFE thread seal tape"}),
    ({"product": "foot valve"}, {"status": "found", "model": "Foot valve"}),
    ({"product": "bottle trap"}, {"status": "found", "model": "Bottle trap"}),
    ({"product": "ball cock for tank"}, {"status": "found", "model": "PTMT ball cock"}),
    ({"product": "connection pipe for basin"}, {"status": "found", "model": "Hose"}),
    ({"product": "braided hose"}, {"status": "found", "model": "Hose"}),
    ({"product": "washing machine inlet pipe"}, {"status": "found", "model": "Washing machine pipe"}),
    ({"product": "washing machine ka pipe"}, {"status": "found", "model": "Washing machine pipe"}),
    ({"product": "manhole cover"}, {"status": "found", "product": "Plastic manhole chamber cover"}),
    ({"product": "chamber cover"}, {"status": "found", "product": "Plastic manhole chamber cover"}),
    ({"product": "cistern spare parts"}, {"status": "product", "product": "Cistern spare parts"}),
    ({"product": "flush ka syphon"}, {"status": "found", "model": "Single flush syphon"}),
    ({"product": "tank filter"}, {"status": "found", "product": "Tank filter"}),
    ({"product": "y filter"}, {"status": "found", "product": "Tank filter"}),
    # ── Household ──
    ({"product": "balti"}, {"status": "product", "product": "Bucket"}),
    ({"product": "plastic ki balti 16 litre"}, {"product": "Bucket"}),
    ({"product": "बाल्टी"}, {"product": "Bucket"}),
    ({"product": "baalti"}, {"product": "Bucket"}),
    ({"product": "bucket", "model": "ganga"}, {"status": "found", "model": "Ganga"}),
    ({"product": "ganga balti"}, {"status": "found", "model": "Ganga"}),
    ({"product": "kaveri"}, {"status": "found", "model": "Kaveri"}),
    ({"product": "milk can"}, {"status": "product", "product": "Milk bucket"}),
    ({"product": "doodh ki balti"}, {"product": "Milk bucket"}),
    ({"product": "maxima"}, {"status": "found", "model": "Maxima"}),
    ({"product": "tub"}, {"status": "product", "product": "Tub"}),
    ({"product": "singham tub"}, {"status": "found", "model": "Singham"}),
    ({"product": "tasla"}, {"product": "Tasla"}),
    ({"product": "ghamela"}, {"product": "Tasla"}),
    ({"product": "mug"}, {"status": "product", "product": "Mug"}),
    ({"product": "soap case"}, {"status": "product", "product": "Soap case"}),
    ({"product": "dust pan"}, {"status": "product", "product": "Dust pan"}),
    ({"product": "lota"}, {"status": "found", "model": "Lota"}),
    ({"product": "gagar"}, {"status": "found", "model": "Gagar"}),
    ({"product": "laundry basket"}, {"status": "found", "model": "Laundry basket"}),
    ({"product": "kapde ki tokri"}, {"status": "found", "model": "Laundry basket"}),
    ({"product": "picnic basket"}, {"status": "found", "model": "Picnic basket"}),
    ({"product": "kitchen tray"}, {"status": "found", "model": "Kitchen tray"}),
    ({"product": "jug"}, {"status": "found", "model": "Jug"}),
    ({"product": "crate"}, {"status": "found", "model": "Crate"}),
    ({"product": "plastic drum"}, {"status": "found", "model": "Drum"}),
    ({"product": "dustbin"}, {"status": "product", "product": "Dustbin"}),
    ({"product": "dast bin"}, {"product": "Dustbin"}),
    ({"product": "kooda daan"}, {"product": "Dustbin"}),
    ({"product": "डस्टबिन"}, {"product": "Dustbin"}),
    ({"product": "pedal wala dustbin"}, {"status": "found", "model": "Pedal bin"}),
    ({"product": "swing bin"}, {"status": "found", "model": "Swing bin"}),
    ({"product": "stool"}, {"status": "product", "product": "Stool"}),
    ({"product": "patla"}, {"product": "Stool"}),
    ({"product": "prithvi stool"}, {"status": "found", "model": "Prithvi"}),
    ({"product": "bathroom set"}, {"status": "product", "product": "Bathroom set"}),
    ({"product": "6 piece bathroom set"}, {"status": "found", "model": "6 piece printed bathroom set"}),
    # ── Garden ──
    ({"product": "gamla"}, {"status": "product", "product": "Planter"}),
    ({"product": "गमला"}, {"product": "Planter"}),
    ({"product": "flower pot"}, {"product": "Planter"}),
    ({"product": "europa planter"}, {"status": "found", "model": "Europa range"}),
    ({"product": "maria gamla"}, {"status": "found", "model": "Europa range"}),
    ({"product": "indus gamla"}, {"status": "found", "model": "Indus range"}),
    ({"product": "hanging basket"}, {"status": "found", "model": "Euro basket"}),
    ({"product": "garden pipe"}, {"status": "product", "product": "Garden pipe"}),
    ({"product": "garden pipe 1 inch"}, {"product": "Garden pipe"}),
    ({"product": "indus garden pipe"}, {"status": "found", "model": "Indus garden pipe"}),
    ({"product": "hose pipe"}, {"product": "Garden pipe"}),
    ({"product": "jhari"}, {"status": "found", "product": "Watering can"}),
    ({"product": "watering can"}, {"status": "found", "product": "Watering can"}),
    # ── Categories / catalogue ──
    ({"product": "bathware"}, {"status": "category", "category": "Bath-ware"}),
    ({"product": "bathroom products"}, {"status": "category", "category": "Bath-ware"}),
    ({"category": "household"}, {"status": "category", "category": "Household"}),
    ({"product": "ghar ka saman"}, {"status": "category", "category": "Household"}),
    ({"product": "garden products"}, {"status": "category"}),
    ({}, {"status": "catalogue"}),
    # ── Water tanks go to the tank tool ──
    ({"product": "water tank"}, {"status": "use_tank_lookup"}),
    ({"product": "1000 litre ki tanki"}, {"status": "use_tank_lookup"}),
    ({"product": "ganga tank"}, {"status": "use_tank_lookup"}),
    ({"product": "पानी की टंकी"}, {"status": "use_tank_lookup"}),
    ({"product": "loft tank"}, {"status": "use_tank_lookup"}),
    # ── Not sold ──
    ({"product": "cpvc pipe"}, {"status": "not_sold"}),
    ({"product": "ppr pipe fitting"}, {"status": "not_sold"}),
    ({"product": "upvc pipe"}, {"status": "not_sold"}),
    ({"product": "plumbing pipes"}, {"status": "not_sold"}),
    ({"product": "gi pipe"}, {"status": "not_sold"}),
    ({"product": "elbow"}, {"status": "not_sold"}),
    # ── Some pipes yes, some no ──
    ({"product": "pipe"}, {"status": "clarify"}),
    ({"product": "pvc pipe"}, {"status": "clarify"}),
    ({"product": "पाइप"}, {"status": "clarify"}),
    # ── Not in the list: never yes or no ──
    ({"product": "geyser"}, {"status": "no_match"}),
    ({"product": "wash basin"}, {"status": "no_match"}),
    ({"product": "ceramic wash basin"}, {"status": "no_match"}),
    ({"product": "commode"}, {"status": "no_match"}),
    ({"product": "western toilet"}, {"status": "no_match"}),
    ({"product": "mixer grinder"}, {"status": "no_match"}),
    ({"product": "water purifier"}, {"status": "no_match"}),
    ({"product": "plastic chair"}, {"status": "no_match"}),
    ({"product": "sofa"}, {"status": "no_match"}),
    # ── A word the list does not cover: confirm only the Vectus part ──
    ({"product": "chrome tap"}, {"status": "partial", "unmatched": "chrome"}),
    ({"product": "sensor tap"}, {"status": "partial", "unmatched": "sensor"}),
    ({"product": "shoe rack"}, {"status": "partial", "unmatched": "shoe"}),
    ({"product": "mug stand"}, {"status": "partial", "unmatched": "stand"}),
    ({"product": "flush valve"}, {"status": "partial", "unmatched": "valve"}),
    ({"product": "concealed cistern"}, {"status": "partial", "unmatched": "concealed"}),
    ({"product": "thermostatic mixer"}, {"status": "partial"}),
    ({"product": "water dispenser"}, {"status": "partial", "unmatched": "water"}),
    ({"product": "brass tap"}, {"status": "found", "product": "Kitchen sink", "warranty": "12 months"}),
    ({"product": "fridge"}, {"status": "no_match"}),
    ({"product": "dabba"}, {"status": "no_match"}),
    ({"product": "television"}, {"status": "no_match"}),
    # ── Said on the tester's real calls (6372534993) ──
    ({"product": "Mujhe household products chahiye, bucket aur mug jaise."}, {"status": "multiple"}),
    ({"product": "bucket aur mug"}, {"status": "multiple"}),
    ({"product": "seat cover and flush tank"}, {"status": "multiple"}),
    ({"product": "household products"}, {"status": "category", "category": "Household"}),
    ({"product": "जी हां, एक बाथरूम की रिक्वायरमेंट थी"}, {"status": "category", "category": "Bath-ware"}),
    ({"product": "Mere ko Vectus ka cooler chahiye"}, {"status": "use_tank_lookup"}),
    ({"product": "vector school"}, {"status": "use_tank_lookup"}),
    ({"product": "Cool aur Puff"}, {"status": "use_tank_lookup"}),
    ({"product": "air cooler"}, {"status": "no_match"}),
    # ── From vectus.in: road safety, T-Base, more dustbins ──
    ({"product": "speed breaker"}, {"status": "found", "model": "Speed breaker"}),
    ({"product": "barricade"}, {"status": "product", "product": "Barricade"}),
    ({"product": "water barrier 100 litre"}, {"status": "found", "model": "Barrier 2 Oval"}),
    ({"product": "barrier 25 litre"}, {"status": "found", "model": "Barrier Pyramid"}),
    ({"product": "traffic cone"}, {"status": "product", "product": "Traffic cone"}),
    ({"product": "convex mirror"}, {"status": "found", "model": "Convex mirror"}),
    ({"product": "mirror"}, {"status": "found", "model": "Cabinet mirror"}),
    ({"product": "cat eye"}, {"status": "found", "model": "Road stud"}),
    ({"product": "wheel stopper"}, {"status": "found", "model": "Wheel stopper"}),
    ({"product": "road safety products"}, {"status": "category", "category": "Road safety"}),
    ({"product": "tank stand"}, {"status": "product", "product": "T-Base tank stand"}),
    ({"product": "t base 1000 litre"}, {"status": "found", "model": "TB-100"}),
    ({"product": "tank stand 550 litre"}, {"status": "found", "model": "TB-50"}),
    ({"product": "community dustbin"}, {"status": "found", "model": "Community bin"}),
    ({"product": "floral swing bin"}, {"status": "found", "model": "Printed garbage bucket"}),
    ({"product": "tub 50 litre"}, {"status": "found", "model": "Gagan"}),
    # ── Shared names ──
    ({"product": "silk"}, {"status": "ambiguous"}),
    ({"product": "smart"}, {"status": "ambiguous"}),
    ({"product": "pro"}, {"status": "ambiguous"}),
    ({"product": "chic"}, {"status": "ambiguous"}),
]


def check(result: dict, expected: dict) -> list[str]:
    errs = []
    for k, v in expected.items():
        if result.get(k) != v:
            errs.append(f"{k}: want {v!r}, got {result.get(k)!r}")
    return errs


def main() -> int:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app import products as P

    cat = P.load_catalog(Path(__file__).resolve().parent.parent / "data" / "products.json")
    bad = 0
    for req, exp in CASES:
        r = P.find(cat, **req)
        errs = check(r, exp)
        if errs:
            bad += 1
            print(f"FAIL {req} -> {r['status']} {r.get('product')}/{r.get('model')}: {'; '.join(errs)}")
    print(f"{len(CASES) - bad}/{len(CASES)} pass")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
