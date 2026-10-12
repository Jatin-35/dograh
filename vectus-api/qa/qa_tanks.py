"""Black-box scenario suite for POST /tanks.

Every scenario is a real customer question, the request the bot would send,
and the answer written out by hand from Tank_details.xlsx — not computed by
the service's code. Talks to a running instance over HTTP only.

    python qa/qa_tanks.py --base http://127.0.0.1:18080 --key <key> [--report qa/tank_test_report.md]

On the VM (inside Dograh's network):
    docker run --rm --network dograh_app-network -v $PWD/qa:/qa python:3.12-slim \
      sh -c "pip -q install httpx && python /qa/qa_tanks.py --base http://vectus-api:8080 --key $KEY"

Exit code is the number of failed checks.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import statistics
import sys
import time
from dataclasses import dataclass, field

import httpx

# ---------------------------------------------------------------------------
# The sheet, written out by hand (Vectus brand unless the name says otherwise).
# ---------------------------------------------------------------------------
V = "Vectus "
G = "Ganga "
W = "Waterwell "
CAP_SMART = [500, 700, 1000, 1500, 2000, 3000, 5000]          # Smart, Pro, Puff
CAP_SAFE = [550, 750, 1100, 1500, 2000, 3000, 5000]           # Safe, Health, Cool, Italian
CAP_GRANITO = [550, 750, 1100, 1500, 2000]
CAP_SILK = [500, 750, 1000, 1500, 2000]                       # Silk, T-90
CAP_TENX = [500, 750, 1000, 1500, 2000, 3000, 5000]
CAP_LOFT = [100, 150, 200, 300, 500, 1000]
CAP_CHEMICAL = [550, 750, 1100, 1500, 2000, 3000, 5000, 10000]
CAP_ISI = [200, 300, 500, 700, 1000, 1500, 2000, 3000, 5000, 10000]
CAP_UG = [1000, 2000, 5000]
CAP_GROUP = [15000, 20000, 25000, 30000]


@dataclass
class S:
    id: str
    area: str
    says: str
    body: dict
    status: str
    tanks: list[str] | None = None        # exact, in order (shown + "more")
    first: str | None = None              # just the first tank
    contains: tuple[str, ...] = ()        # in data.message
    absent: tuple[str, ...] = ()          # never in data.message
    closest: dict | None = None
    brand: str | None = None
    caps: list[int] | None = None         # capacities of the first tank
    extra: dict = field(default_factory=dict)  # other top-level keys that must match


SCENARIOS: list[S] = [
    # --- A. capacity only ----------------------------------------------------
    S("A01", "capacity", "1100 litre chahiye", {"capacity": "1100"}, "found",
      tanks=[V + "Safe", V + "Cool", V + "Granito"]),
    S("A02", "capacity", "550 litre", {"capacity": 550}, "found",
      tanks=[V + "Safe", V + "Cool", V + "Granito"]),
    S("A03", "capacity", "700 litre", {"capacity": 700}, "found",
      tanks=[V + "Smart", V + "Puff"]),
    S("A04", "capacity", "500 litre", {"capacity": 500}, "found",
      tanks=[V + "Loft", V + "Smart", V + "Puff", V + "Silk", V + "T-90", V + "TenX"]),
    S("A05", "capacity", "750 litre", {"capacity": 750}, "found",
      tanks=[V + "Safe", V + "Silk", V + "Cool", V + "Granito", V + "T-90", V + "TenX"]),
    S("A06", "capacity", "1000 litre", {"capacity": 1000}, "found",
      tanks=[V + "Loft", V + "Mini", V + "Smart", V + "Puff", V + "Silk", V + "T-90",
             V + "Horizontal Tank", V + "TenX"]),
    S("A07", "capacity", "1500 litre", {"capacity": 1500}, "found",
      tanks=[V + "Smart", V + "Puff", V + "Safe", V + "Silk", V + "Cool", V + "Granito",
             V + "T-90", V + "TenX"]),
    S("A08", "capacity", "2000 litre", {"capacity": 2000}, "found",
      tanks=[V + "Smart", V + "Puff", V + "Safe", V + "Silk", V + "Cool", V + "Granito",
             V + "T-90", V + "TenX"]),
    S("A09", "capacity", "3000 litre", {"capacity": 3000}, "found",
      tanks=[V + "Smart", V + "Puff", V + "Safe", V + "Cool", V + "TenX"]),
    S("A10", "capacity", "5000 litre", {"capacity": 5000}, "found",
      tanks=[V + "Smart", V + "Puff", V + "Safe", V + "Cool", V + "TenX"]),
    S("A11", "capacity", "100 litre", {"capacity": 100}, "found", tanks=[V + "Loft"]),
    S("A12", "capacity", "300 litre", {"capacity": 300}, "found", tanks=[V + "Loft"],
      absent=("ISI",)),
    S("A13", "capacity", "10000 litre", {"capacity": 10000}, "found",
      tanks=[V + "ISI tank"], absent=("Chemical",)),
    S("A14", "capacity", "20000 litre", {"capacity": 20000}, "found", tanks=[V + "Group"]),
    S("A15", "capacity", "30000 litre", {"capacity": "30,000"}, "found", tanks=[V + "Group"]),
    # --- B. sizes that are not made --------------------------------------------
    S("B01", "not made", "1200 litre", {"capacity": 1200}, "no_match",
      closest={"lower": 1100, "higher": 1500}, contains=("No Vectus tank comes in 1200",)),
    S("B02", "not made", "400 litre", {"capacity": 400}, "no_match",
      closest={"lower": 300, "higher": 500}),
    S("B03", "not made", "600 litre", {"capacity": 600}, "no_match",
      closest={"lower": 550, "higher": 700}),
    S("B04", "not made", "800 litre", {"capacity": 800}, "no_match",
      closest={"lower": 750, "higher": 1000}),
    S("B05", "not made", "1800 litre", {"capacity": 1800}, "no_match",
      closest={"lower": 1500, "higher": 2000}),
    S("B06", "not made", "2500 litre", {"capacity": 2500}, "no_match",
      closest={"lower": 2000, "higher": 3000}),
    S("B07", "not made", "4000 litre", {"capacity": 4000}, "no_match",
      closest={"lower": 3000, "higher": 5000}),
    S("B08", "not made", "7000 litre", {"capacity": 7000}, "no_match",
      closest={"lower": 5000, "higher": 10000}, contains=("ISI tank",), absent=("Chemical",)),
    S("B09", "not made", "12000 litre", {"capacity": 12000}, "no_match",
      closest={"lower": 10000, "higher": 15000}, contains=("Vectus Group",)),
    S("B10", "not made", "50000 litre", {"capacity": 50000}, "no_match",
      closest={"lower": 30000, "higher": None}),
    S("B11", "not made", "50 litre", {"capacity": 50}, "no_match",
      closest={"lower": None, "higher": 100}, contains=("Vectus Loft",)),
    # --- C. capacity as spoken -------------------------------------------------
    S("C01", "spoken", "gyarah sau litre", {"capacity": "gyarah sau"}, "found",
      tanks=[V + "Safe", V + "Cool", V + "Granito"]),
    S("C02", "spoken", "saade saat sau", {"capacity": "saade saat sau"}, "found",
      first=V + "Safe", contains=("750 litre",)),
    S("C03", "spoken", "dedh hazaar", {"capacity": "dedh hazaar"}, "found",
      first=V + "Smart", contains=("1500 litre",)),
    S("C04", "spoken", "do hazaar litre", {"capacity": "do hazaar"}, "found", first=V + "Smart"),
    S("C05", "spoken", "paanch hazaar", {"capacity": "paanch hazaar"}, "found",
      tanks=[V + "Smart", V + "Puff", V + "Safe", V + "Cool", V + "TenX"]),
    S("C06", "spoken", "1.1k", {"capacity": "1.1k"}, "found",
      tanks=[V + "Safe", V + "Cool", V + "Granito"]),
    S("C07", "spoken", "1100L", {"capacity": "1100L"}, "found", first=V + "Safe"),
    S("C08", "spoken", "1100.0 (LLM float)", {"capacity": 1100.0}, "found", first=V + "Safe"),
    S("C09", "spoken", "1000 ya 1500 (choice)", {"capacity": "1000 ya 1500"}, "found",
      contains=("1000 or 1500 litre",)),
    S("C10", "spoken", "300 tak (up to)", {"capacity": "300 tak"}, "found", tanks=[V + "Loft"]),
    S("C11", "spoken", "5000 se upar", {"capacity": "5000 se upar"}, "found",
      tanks=[V + "Smart", V + "Puff", V + "Safe", V + "Cool", V + "TenX"]),
    S("C12", "spoken", "1000 se 1500 ke beech", {"capacity": "1000 se 1500 ke beech"}, "found",
      contains=("1000-1500 litre",)),
    S("C13", "spoken", "'bada wala' (no number)", {"capacity": "bada wala"}, "catalogue",
      extra={"ignored": {"capacity": "bada wala"}}),
    # --- D. colour only ------------------------------------------------------
    S("D01", "colour", "green colour mein", {"colour": "green"}, "found",
      tanks=[V + "Smart", V + "Safe"]),
    S("D02", "colour", "neela", {"colour": "neela"}, "found", tanks=[V + "Smart", V + "Safe"]),
    S("D03", "colour", "peela tank", {"colour": "peela"}, "found",
      tanks=[V + "T-90", V + "TenX"]),
    S("D04", "colour", "yellow", {"colour": "yellow"}, "found", tanks=[V + "T-90", V + "TenX"]),
    S("D05", "colour", "safed", {"colour": "safed"}, "found",
      tanks=[V + "Loft", V + "Mini", V + "Smart", V + "Puff", V + "Safe", V + "Silk",
             V + "Cool", V + "Horizontal Tank", V + "TenX"]),
    S("D06", "colour", "kala / black", {"colour": "kala"}, "found",
      tanks=[V + "ISI tank", V + "Underground", V + "Group"], absent=("Chemical",)),
    S("D07", "colour", "ruby", {"colour": "ruby"}, "found", tanks=[V + "Granito"]),
    S("D08", "colour", "emerald", {"colour": "Emerald"}, "found", tanks=[V + "Granito"]),
    S("D09", "colour", "pearl", {"colour": "pearl"}, "found", tanks=[V + "Granito"]),
    S("D10", "colour", "red (not in sheet)", {"colour": "red"}, "unknown_colour",
      contains=("Ruby", "Emerald", "Pearl", "White")),
    S("D11", "colour", "green ya blue", {"colour": "green ya blue"}, "found",
      tanks=[V + "Smart", V + "Safe"]),
    S("D12", "colour", "Ganga, green", {"colour": "green", "brand": "Ganga"}, "found",
      tanks=[G + "Health"]),
    S("D13", "colour", "American spelling field", {"color": "green"}, "found",
      tanks=[V + "Smart", V + "Safe"]),
    # --- E. capacity + colour --------------------------------------------------
    S("E01", "cap+colour", "1100 green", {"capacity": 1100, "colour": "green"}, "found",
      tanks=[V + "Safe"]),
    S("E02", "cap+colour", "1000 green", {"capacity": 1000, "colour": "green"}, "found",
      tanks=[V + "Smart"]),
    S("E03", "cap+colour", "1500 blue", {"capacity": 1500, "colour": "blue"}, "found",
      tanks=[V + "Smart", V + "Safe"]),
    S("E04", "cap+colour", "1100 yellow", {"capacity": 1100, "colour": "yellow"}, "no_match",
      closest={"lower": 1000, "higher": 1500}, contains=("T-90", "TenX")),
    S("E05", "cap+colour", "3000 yellow", {"capacity": 3000, "colour": "yellow"}, "found",
      tanks=[V + "TenX"]),
    S("E06", "cap+colour", "3000 ruby", {"capacity": 3000, "colour": "ruby"}, "no_match",
      closest={"lower": 2000, "higher": None}),
    S("E07", "cap+colour", "10000 white", {"capacity": 10000, "colour": "white"}, "no_match",
      closest={"lower": 5000, "higher": None}),
    S("E08", "cap+colour", "200 white", {"capacity": 200, "colour": "white"}, "found",
      tanks=[V + "Loft"]),
    S("E09", "cap+colour", "5000 black", {"capacity": 5000, "colour": "black"}, "found",
      tanks=[V + "ISI tank", V + "Underground"], absent=("Chemical",)),
    # --- F. a named model ------------------------------------------------------
    S("F01", "model", "Vectus Cool batao", {"model": "Vectus Cool"}, "found",
      tanks=[V + "Cool"], caps=CAP_SAFE, contains=("5 layer", "15 year guarantee")),
    S("F02", "model", "Smart", {"model": "Smart"}, "found", tanks=[V + "Smart"], caps=CAP_SMART,
      contains=("White / Green / Blue", "10 year guarantee")),
    S("F03", "model", "Mini", {"model": "Mini"}, "found", tanks=[V + "Mini"],
      contains=("1000 litre only", "15 year guarantee")),
    S("F04", "model", "Loft", {"model": "Loft"}, "found", caps=CAP_LOFT, contains=("Blow",)),
    S("F05", "model", "TenX", {"model": "TenX"}, "found", caps=CAP_TENX,
      contains=("10 layer", "White / Lime Yellow")),
    S("F06", "model", "Granito", {"model": "Granito"}, "found", caps=CAP_GRANITO,
      contains=("Ruby / Emerald / Pearl",)),
    S("F07", "model", "Silk", {"model": "Silk"}, "found", caps=CAP_SILK),
    S("F08", "model", "T-90", {"model": "T-90"}, "found", caps=CAP_SILK, contains=("6 layer",)),
    S("F09", "model", "Chemical tank", {"model": "chemical"}, "found", caps=CAP_CHEMICAL,
      contains=("no guarantee",)),
    S("F10", "model", "ISI tank", {"model": "ISI"}, "found", caps=CAP_ISI),
    S("F11", "model", "Underground", {"model": "underground"}, "found", caps=CAP_UG),
    S("F12", "model", "Vectus group", {"model": "group"}, "found", caps=CAP_GROUP),
    S("F13", "model", "Horizontal", {"model": "horizontal"}, "found",
      contains=("10 layer", "1000 litre only"), absent=("three-layer",)),
    S("F14", "model", "Safe", {"model": "safe"}, "found", caps=CAP_SAFE),
    S("F15", "model", "Puff", {"model": "puff"}, "found", caps=CAP_SMART),
    # --- G. model + capacity ---------------------------------------------------
    S("G01", "model+cap", "Cool 1100?", {"model": "Cool", "capacity": 1100}, "found",
      contains=("Yes, Vectus Cool comes in 1100 litre",)),
    S("G02", "model+cap", "Smart 1100?", {"model": "Smart", "capacity": 1100}, "not_available",
      closest={"lower": 1000, "higher": 1500}, contains=("Vectus Safe", "Vectus Cool")),
    S("G03", "model+cap", "Granito 3000?", {"model": "Granito", "capacity": 3000},
      "not_available", closest={"lower": 2000, "higher": None},
      contains=("550 to 2000",)),
    S("G04", "model+cap", "Cool 1000?", {"model": "Cool", "capacity": 1000}, "not_available",
      closest={"lower": 750, "higher": 1100}),
    S("G05", "model+cap", "Mini 500?", {"model": "Mini", "capacity": 500}, "not_available",
      closest={"lower": None, "higher": 1000}),
    S("G06", "model+cap", "T-90 1100?", {"model": "T-90", "capacity": 1100}, "not_available",
      closest={"lower": 1000, "higher": 1500}),
    S("G07", "model+cap", "TenX 5000?", {"model": "TenX", "capacity": 5000}, "found"),
    S("G08", "model+cap", "Silk 700?", {"model": "Silk", "capacity": 700}, "not_available",
      closest={"lower": 500, "higher": 750}),
    S("G09", "model+cap", "Loft 100?", {"model": "Loft", "capacity": 100}, "found"),
    S("G10", "model+cap", "Loft 2000?", {"model": "Loft", "capacity": 2000}, "not_available",
      closest={"lower": 1000, "higher": None}),
    S("G11", "model+cap", "Underground 3000?", {"model": "underground", "capacity": 3000},
      "not_available", closest={"lower": 2000, "higher": 5000}),
    S("G12", "model+cap", "Chemical 10000?", {"model": "chemical", "capacity": 10000}, "found"),
    S("G13", "model+cap", "Safe 650? (old prompt's wrong value)",
      {"model": "Safe", "capacity": 650}, "not_available",
      closest={"lower": 550, "higher": 750}),
    S("G14", "model+cap", "Cool 1200?", {"model": "Cool", "capacity": 1200}, "not_available",
      closest={"lower": 1100, "higher": 1500}),
    # --- H. model + colour -----------------------------------------------------
    S("H01", "model+colour", "Puff blue?", {"model": "Puff", "colour": "blue"}, "not_available",
      contains=("comes only in White", "Vectus Smart", "Vectus Safe")),
    S("H02", "model+colour", "Safe green?", {"model": "Safe", "colour": "green"}, "found"),
    S("H03", "model+colour", "TenX green?", {"model": "TenX", "colour": "green"},
      "not_available", contains=("White / Lime Yellow",)),
    S("H04", "model+colour", "Granito white?", {"model": "Granito", "colour": "white"},
      "not_available", contains=("Ruby / Emerald / Pearl",)),
    S("H05", "model+colour", "TenX yellow?", {"model": "TenX", "colour": "yellow"}, "found"),
    S("H06", "model+colour+cap", "Safe 1100 green?",
      {"model": "Safe", "capacity": 1100, "colour": "green"}, "found"),
    S("H07", "model+colour+cap", "Smart 1100 green?",
      {"model": "Smart", "capacity": 1100, "colour": "green"}, "not_available",
      contains=("Vectus Safe",)),
    # --- I. layers / guarantee / mould ---------------------------------------
    S("I01", "layers", "5 layer wala", {"layers": 5}, "found", tanks=[V + "Cool"]),
    S("I02", "layers", "4 layer", {"layers": "4 layer"}, "found",
      tanks=[V + "Puff", V + "Safe", V + "Silk"]),
    S("I03", "layers", "6 layer", {"layers": 6}, "found", tanks=[V + "Granito", V + "T-90"]),
    S("I04", "layers", "10 layer", {"layers": 10}, "found",
      tanks=[V + "Horizontal Tank", V + "TenX"]),
    S("I05", "layers", "3 layer", {"layers": "teen"}, "found",
      tanks=[V + "Loft", V + "Mini", V + "Smart"]),
    S("I06", "layers", "1 layer", {"layers": 1}, "found", tanks=[V + "Chemical Tank"]),
    S("I07", "layers", "7 layer (none)", {"layers": 7}, "no_match",
      contains=("1, 2, 3, 4, 5, 6, 10",)),
    S("I08", "layers", "Waterwell 5 layer", {"layers": 5, "brand": "Waterwell"}, "found",
      tanks=[W + "Italian"]),
    S("I09", "guarantee", "15 saal guarantee", {"guarantee": "15 saal"}, "found",
      tanks=[V + "Mini", V + "Safe", V + "Cool", V + "Granito", V + "Horizontal Tank",
             V + "TenX"]),
    S("I10", "guarantee", "10 saal", {"guarantee": 10}, "found",
      tanks=[V + "Loft", V + "Smart", V + "Puff", V + "Silk", V + "T-90"]),
    S("I11", "guarantee", "bina guarantee", {"guarantee": "no guarantee"}, "found",
      tanks=[V + "Underground", V + "Group"], absent=("Chemical",)),
    S("I12", "guarantee", "20 saal (none)", {"guarantee": 20}, "no_match",
      contains=("10 years", "15 years")),
    S("I13", "guarantee+cap", "1100 with 15 saal", {"capacity": 1100, "guarantee": 15}, "found",
      tanks=[V + "Safe", V + "Cool", V + "Granito"]),
    S("I14", "guarantee+cap", "1000 with 15 saal", {"capacity": 1000, "guarantee": 15},
      "found", tanks=[V + "Mini", V + "Horizontal Tank", V + "TenX"]),
    S("I15", "mould", "blow moulded 1000", {"capacity": 1000, "mould": "blow"}, "found",
      tanks=[V + "Loft", V + "Silk", V + "T-90", V + "Horizontal Tank", V + "TenX"]),
    # --- J. needs ------------------------------------------------------------
    S("J01", "need", "garmi mein thanda rahe", {"need": "garmi mein thanda"}, "found",
      tanks=[V + "Puff", V + "Cool", V + "Granito", V + "T-90", V + "Horizontal Tank",
             V + "TenX"]),
    S("J02", "need", "thanda + 1100", {"need": "thanda pani", "capacity": 1100}, "found",
      tanks=[V + "Cool", V + "Granito"]),
    S("J03", "need", "underground 2000", {"need": "underground", "capacity": 2000}, "found",
      tanks=[V + "Underground"]),
    S("J04", "need", "kam jagah (small space)", {"need": "kam jagah"}, "found",
      tanks=[V + "Loft", V + "Mini", V + "Horizontal Tank"]),
    S("J05", "need", "commercial 20000", {"need": "commercial", "capacity": 20000}, "found",
      tanks=[V + "Group"]),
    S("J06", "need", "chemical storage", {"need": "chemical"}, "found",
      tanks=[V + "Chemical Tank"]),
    S("J07", "need", "'sasta' (unknown)", {"need": "sasta", "capacity": 700}, "found",
      tanks=[V + "Smart", V + "Puff"], extra={"ignored": {"need": "sasta"}}),
    # --- K. brands and states ------------------------------------------------
    S("K01", "brand", "Tamil Nadu, 1100", {"state": "Tamil Nadu", "capacity": 1100}, "found",
      tanks=[G + "Health", G + "Cool", G + "Granito"], brand="Ganga"),
    S("K02", "brand", "Kerala, green", {"state": "Kerala", "colour": "green"}, "found",
      tanks=[G + "Health"], brand="Ganga"),
    S("K03", "brand", "Karnataka (KA)", {"state": "KA", "model": "Cool"}, "found",
      tanks=[G + "Cool"], brand="Ganga"),
    S("K04", "brand", "Telangana", {"state": "Telangana", "model": "Puff"}, "found",
      tanks=[G + "Puff"]),
    S("K05", "brand", "Andhra Pradesh", {"state": "AP", "model": "TenX"}, "found",
      tanks=[G + "TenX"]),
    S("K06", "brand", "UP default Vectus", {"state": "UP", "capacity": 1100}, "found",
      brand="Vectus", first=V + "Safe"),
    S("K07", "brand", "TN but asks Vectus", {"state": "TN", "brand": "Vectus", "model": "Cool"},
      "found", tanks=[V + "Cool"]),
    S("K08", "brand", "Waterwell 1100", {"brand": "Waterwell", "capacity": 1100}, "found",
      tanks=[W + "Health", W + "Italian", W + "Granito"]),
    S("K09", "brand", "Sintex (competitor)", {"brand": "Sintex", "capacity": 700}, "found",
      brand="Vectus", extra={"ignored": {"brand": "Sintex"}}),
    S("K10", "brand", "all brands, Cool", {"brand": "all", "model": "Cool"}, "found",
      tanks=[V + "Cool", G + "Cool", W + "Italian"]),
    S("K11", "brand", "'water well' spelling", {"brand": "water well", "model": "Silk"}, "found",
      tanks=[W + "Silk"]),
    # --- L. a model the brand does not sell -----------------------------------
    S("L01", "brand gap", "Vectus Pro", {"model": "Pro"}, "not_in_brand",
      contains=("Vectus Smart",)),
    S("L02", "brand gap", "Vectus Health", {"model": "Health"}, "not_in_brand",
      contains=("Vectus Safe",)),
    S("L03", "brand gap", "Vectus Italian", {"model": "Italian"}, "not_in_brand",
      contains=("Vectus Cool",)),
    S("L04", "brand gap", "Ganga Safe", {"model": "Safe", "brand": "Ganga"}, "not_in_brand",
      contains=("Ganga Health",)),
    S("L05", "brand gap", "Waterwell Cool", {"model": "Waterwell Cool"}, "not_in_brand",
      contains=("Waterwell Italian",)),
    S("L06", "brand gap", "Ganga Mini (TN)", {"model": "Mini", "state": "TN"}, "not_in_brand",
      contains=("Vectus Mini",)),
    S("L07", "brand gap", "Ganga Smart", {"model": "Smart", "brand": "Ganga"}, "not_in_brand",
      contains=("Ganga Pro",)),
    # --- M. model names as heard -----------------------------------------------
    S("M01", "heard", "vector school", {"model": "vector school"}, "found", tanks=[V + "Cool"]),
    S("M02", "heard", "vectors school", {"model": "vectors school"}, "found", tanks=[V + "Cool"]),
    S("M03", "heard", "कूल (Hindi script)", {"model": "कूल"}, "found", tanks=[V + "Cool"]),
    S("M04", "heard", "ten x", {"model": "ten x"}, "found", tanks=[V + "TenX"]),
    S("M05", "heard", "10x", {"model": "10x"}, "found", tanks=[V + "TenX"]),
    S("M06", "heard", "t90", {"model": "t90"}, "found", tanks=[V + "T-90"]),
    S("M07", "heard", "granite", {"model": "granite"}, "found", tanks=[V + "Granito"]),
    S("M08", "heard", "granitto (typo)", {"model": "granitto"}, "found", tanks=[V + "Granito"]),
    S("M09", "heard", "puf", {"model": "puf"}, "found", tanks=[V + "Puff"]),
    S("M10", "heard", "पफ", {"model": "पफ"}, "found", tanks=[V + "Puff"]),
    S("M11", "heard", "Victus Cool", {"model": "Victus Cool"}, "found", tanks=[V + "Cool"]),
    S("M12", "heard", "sintex", {"model": "sintex"}, "unknown_model",
      contains=("Cool", "Puff", "TenX")),
    S("M13", "heard", "supreme", {"model": "supreme"}, "unknown_model"),
    # --- N. the whole phrase in "model" ----------------------------------------
    S("N01", "phrase", "Cool 1100 litre", {"model": "Cool 1100 litre"}, "found",
      contains=("Yes, Vectus Cool comes in 1100 litre",)),
    S("N02", "phrase", "Granito 3000", {"model": "Granito 3000"}, "not_available"),
    S("N03", "phrase", "green safe", {"model": "green safe"}, "found", tanks=[V + "Safe"]),
    S("N04", "phrase", "blue puff", {"model": "blue puff"}, "not_available"),
    S("N05", "phrase", "5 layer wala", {"model": "5 layer wala"}, "found", tanks=[V + "Cool"]),
    S("N06", "phrase", "water tank", {"model": "water tank"}, "catalogue"),
    S("N07", "phrase", "T-90 1000", {"model": "T-90 1000"}, "found",
      contains=("Yes, Vectus T-90 comes in 1000 litre",)),
    # --- O. comparisons ------------------------------------------------------
    S("O01", "compare", "Cool aur Puff mein fark", {"model": "Cool aur Puff"}, "found",
      tanks=[V + "Cool", V + "Puff"], contains=("Compare:", "Temperature-Resistant Foam")),
    S("O02", "compare", "Smart vs Safe", {"model": "Smart vs Safe"}, "found",
      tanks=[V + "Smart", V + "Safe"]),
    S("O03", "compare", "Smart, Safe and Cool", {"model": "Smart, Safe and Cool"}, "found",
      tanks=[V + "Smart", V + "Safe", V + "Cool"]),
    S("O04", "compare", "TN: Cool aur Safe", {"model": "Cool aur Safe", "state": "TN"}, "found",
      tanks=[G + "Cool", G + "Health"]),
    S("O05", "compare", "Granito ya TenX", {"model": "Granito ya TenX"}, "found",
      tanks=[V + "Granito", V + "TenX"]),
    # --- P. empty / junk -------------------------------------------------------
    S("P01", "junk", "(nothing given)", {}, "catalogue",
      contains=("capacity", "5 layer: Cool", "Also, for commercial or special use")),
    S("P02", "junk", "LLM nulls", {"model": "null", "capacity": "", "colour": "None"},
      "catalogue"),
    S("P03", "junk", "only a state", {"state": "UP"}, "catalogue"),
    S("P04", "junk", "unknown fields", {"product": "Water tank", "foo": 1, "model": "Cool"},
      "found", tanks=[V + "Cool"]),
    S("P05", "junk", "very long text", {"model": "cool " * 300}, "found"),
    S("P06", "junk", "relaxation: 10 layer + green", {"layers": 10, "colour": "green"},
      "no_match", contains=("Without the layers", "Without the colour")),
]


# ---------------------------------------------------------------------------


def run_scenario(c: httpx.Client, s: S) -> tuple[bool, str, dict]:
    r = c.post("/tanks", json=s.body)
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}", {}
    d = r.json()
    problems = []
    if d.get("status") != s.status:
        problems.append(f"status {d.get('status')} != {s.status}")
    got = [t["name"] for t in d.get("tanks", [])] + d.get("more", [])
    if s.tanks is not None and got != s.tanks:
        problems.append(f"tanks {got} != {s.tanks}")
    if s.first and (not got or got[0] != s.first):
        problems.append(f"first {got[:1]} != {s.first}")
    if s.caps is not None and (not d.get("tanks") or d["tanks"][0]["capacities"] != s.caps):
        problems.append(f"capacities {d.get('tanks', [{}])[0].get('capacities')} != {s.caps}")
    for text in s.contains:
        if text not in d.get("message", ""):
            problems.append(f"message lacks {text!r}")
    for text in s.absent:
        if text in json.dumps(d):
            problems.append(f"reply has {text!r}")
    if s.closest is not None and d.get("closest_capacities") != s.closest:
        problems.append(f"closest {d.get('closest_capacities')} != {s.closest}")
    if s.brand and d.get("brand") != s.brand:
        problems.append(f"brand {d.get('brand')} != {s.brand}")
    for k, v in s.extra.items():
        if d.get(k) != v:
            problems.append(f"{k} {d.get(k)} != {v}")
    if len(d.get("tanks", [])) > 4:
        problems.append("more than 4 tanks shown")
    return not problems, "; ".join(problems), d


def flows(c: httpx.Client) -> list[tuple[str, str, bool, str]]:
    """Multi-turn calls, the way the bot would chain lookups."""
    out = []

    def q(**b):
        return c.post("/tanks", json=b).json()

    # 1. capacity first, then the customer picks a model from the list
    a = q(capacity=1100)
    b = q(model="Cool", capacity=1100)
    out.append(("FL1", "1100 → options → 'Cool wala' → confirmed",
                a["status"] == "found" and "Vectus Cool" in a["message"]
                and b["message"].startswith("Yes, Vectus Cool comes in 1100"), ""))
    # 2. model first, size not made, customer takes the closest
    a = q(model="Smart", capacity=1100)
    b = q(model="Smart", capacity=a["closest_capacities"]["higher"])
    out.append(("FL2", "Smart 1100 → not made → takes 1500 → confirmed",
                a["status"] == "not_available" and b["status"] == "found"
                and "1500" in b["message"], ""))
    # 3. colour then capacity narrowing
    a = q(colour="green")
    b = q(colour="green", capacity=1100)
    out.append(("FL3", "green → Smart, Safe → '1100 chahiye' → Safe only",
                [t["name"] for t in a["tanks"]] == ["Vectus Smart", "Vectus Safe"]
                and [t["name"] for t in b["tanks"]] == ["Vectus Safe"], ""))
    # 4. south customer, brand stays Ganga through the call
    a = q(state="Tamil Nadu", need="cooling")
    b = q(state="Tamil Nadu", model="Cool", capacity=750)
    out.append(("FL4", "TN cooling → Ganga options → Ganga Cool 750 confirmed",
                all(t["name"].startswith("Ganga") for t in a["tanks"])
                and b["message"].startswith("Yes, Ganga Cool comes in 750"), ""))
    # 5. asks a Vectus name in the south
    a = q(state="Kerala", model="Safe")
    out.append(("FL5", "Kerala asks 'Safe' → told Ganga Health is the same tank",
                a["status"] == "not_in_brand" and "Ganga Health" in a["message"], ""))
    # 6. comparison then pick
    a = q(model="Cool aur Puff")
    b = q(model="Puff", capacity=700)
    out.append(("FL6", "Cool vs Puff → picks Puff 700 → confirmed",
                len(a["tanks"]) == 2 and b["message"].startswith("Yes, Vectus Puff comes in 700"),
                ""))
    return out


def http_checks(base: str, key: str) -> list[tuple[str, str, bool, str]]:
    out = []
    c = httpx.Client(base_url=base, timeout=10)
    r = c.post("/tanks", json={"model": "Cool"})
    out.append(("H1", "no API key → 401", r.status_code == 401, str(r.status_code)))
    r = c.post("/tanks", json={"model": "Cool"}, headers={"X-API-Key": "wrong"})
    out.append(("H2", "wrong API key → 401", r.status_code == 401, str(r.status_code)))
    r = c.post("/tanks/", json={"model": "Cool"}, headers={"X-API-Key": key})
    out.append(("H3", "trailing slash works (no redirect)", r.status_code == 200, ""))
    r = c.post("/tanks", content=b"not json", headers={"X-API-Key": key,
                                                         "Content-Type": "application/json"})
    out.append(("H4", "broken JSON → 422, no crash", r.status_code == 422, str(r.status_code)))
    r = c.get("/health")
    out.append(("H5", "/health reports 39 tanks", r.json().get("tanks") == 39, r.text[:120]))
    r = c.get("/tanks", headers={"X-API-Key": key})
    out.append(("H6", "GET /tanks not allowed", r.status_code == 405, str(r.status_code)))
    return out


def perf(base: str, key: str) -> list[tuple[str, str, bool, str]]:
    bodies = [s.body for s in SCENARIOS]
    times, errors = [], 0

    def one(i):
        c = httpx.Client(base_url=base, timeout=10, headers={"X-API-Key": key})
        local = []
        for j in range(40):
            t0 = time.perf_counter()
            r = c.post("/tanks", json=bodies[(i * 40 + j) % len(bodies)])
            local.append((time.perf_counter() - t0) * 1000)
            if r.status_code != 200:
                local.append(-1)
        return local

    with cf.ThreadPoolExecutor(10) as ex:
        for res in ex.map(one, range(10)):
            errors += sum(1 for x in res if x < 0)
            times += [x for x in res if x >= 0]
    p50 = statistics.median(times)
    p95 = sorted(times)[int(len(times) * 0.95)]
    return [("PF1", f"10 concurrent callers × 40 requests: 0 errors, p95 < 200 ms "
                    f"(p50 {p50:.1f} ms, p95 {p95:.1f} ms)", errors == 0 and p95 < 200, "")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:18080")
    ap.add_argument("--key", required=True)
    ap.add_argument("--report")
    args = ap.parse_args()
    c = httpx.Client(base_url=args.base, timeout=10, headers={"X-API-Key": args.key})

    rows = []
    fails = 0
    for s in SCENARIOS:
        ok, why, d = run_scenario(c, s)
        fails += not ok
        rows.append((s, ok, why, d))
    extra = flows(c) + http_checks(args.base, args.key) + perf(args.base, args.key)
    fails += sum(not ok for _, _, ok, _ in extra)

    total = len(rows) + len(extra)
    print(f"{total - fails}/{total} passed")
    for s, ok, why, _ in rows:
        if not ok:
            print(f"FAIL {s.id} {s.says}: {why}")
    for tid, title, ok, why in extra:
        if not ok:
            print(f"FAIL {tid} {title}: {why}")

    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            f.write("# Tank lookup — scenario test report\n\n")
            f.write(f"Run against `{args.base}` on {time.strftime('%Y-%m-%d %H:%M')}. "
                    f"**{total - fails}/{total} passed.** Expected answers were written by hand "
                    f"from `Tank_details.xlsx` (Vectus brand unless a state or brand is given).\n\n")
            area = None
            for s, ok, why, d in rows:
                if s.area != area:
                    area = s.area
                    f.write(f"\n## {area}\n\n| ID | Customer says | Bot sends | Result | "
                            f"Tanks returned | Pass |\n|---|---|---|---|---|---|\n")
                got = [t["name"] for t in d.get("tanks", [])] + d.get("more", [])
                result = d.get("status", "?")
                if d.get("closest_capacities"):
                    cc = d["closest_capacities"]
                    result += f" (closest {cc['lower']} / {cc['higher']})"
                f.write(f"| {s.id} | {s.says} | `{json.dumps(s.body, ensure_ascii=False)[:70]}` "
                        f"| {result} | {', '.join(got) or '—'} | "
                        f"{'✅' if ok else '❌ ' + why} |\n")
            f.write("\n## Multi-turn flows, HTTP and load\n\n| ID | Check | Pass |\n|---|---|---|\n")
            for tid, title, ok, why in extra:
                f.write(f"| {tid} | {title} | {'✅' if ok else '❌ ' + why} |\n")
            f.write("\n## Sample replies the bot reads\n\n")
            for sid in ("A01", "B01", "G02", "L01", "O01", "E04"):
                s, _, _, d = next(r for r in rows if r[0].id == sid)
                f.write(f"**{sid} — {s.says}**\n\n> {d.get('message', '')}\n\n")
    sys.exit(fails)


if __name__ == "__main__":
    main()
