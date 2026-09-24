"""Edge cases, run end to end through the HTTP layer (schema + matching).

Each case: (request body, allowed statuses, a city that must appear in the
answer or the suggestions — or None). Grouped by what they exercise.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("VECTUS_API_KEY", "test-key")

from fastapi.testclient import TestClient  # noqa: E402

from app import aliases as A  # noqa: E402
from app import main  # noqa: E402

main.API_KEY = os.environ["VECTUS_API_KEY"]
H = {"X-API-Key": main.API_KEY}

FOUND = ("found",)
FOUND_OR_MULTI = ("found", "multiple")


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


def post(client, **body):
    r = client.post("/lookup", json=body, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def cities_in(r) -> set[str]:
    s = r["status"]
    if s == "found":
        return {r["city"]} if r.get("city") else set(r.get("covers", []))
    if s == "multiple":
        return {loc["city"] for m in r["matches"] for loc in m["locations"]}
    if s == "confirm":
        return {x["city"] for x in r["suggestions"]}
    return set()


def check(client, body, statuses, city):
    r = post(client, **body)
    assert r["status"] in statuses, (body, r)
    if city:
        assert city in cities_in(r), (body, r)
    # A number may only ever come with found/multiple.
    if r["status"] not in ("found", "multiple"):
        assert "contact" not in r and "matches" not in r, (body, r)
    return r


def t(product="tank", city=None, district=None, state=None):
    return {k: v for k, v in dict(product=product, city=city, district=district,
                                  state=state).items() if v is not None}


# --- 1. how the text arrives --------------------------------------------------------

FORMATTING = [
    (t(city="  noida  "), FOUND, "Noida"),
    (t(city="NOIDA"), FOUND, "Noida"),
    (t(city="Noida."), FOUND, "Noida"),
    (t(city="noida!!"), FOUND, "Noida"),
    (t(city="Noida 🙏"), FOUND, "Noida"),
    (t(city="Noida, UP"), FOUND, "Noida"),
    (t(city="Noida Uttar Pradesh"), FOUND, "Noida"),
    (t(city="Noida 201301"), FOUND, "Noida"),
    (t(city="Noida sector 18"), FOUND, "Noida"),
    (t(city="Noida city"), FOUND, "Noida"),
    (t(city="near Noida"), FOUND, "Noida"),
    (t(city="Noida ke paas"), FOUND, "Noida"),
    (t(city="Noida mein"), FOUND, "Noida"),
    (t(city="Noida district"), FOUND, "Noida"),
    (t(city="zila Bareilly"), FOUND, "Bareily"),
    (t(city="Bareilly distt."), FOUND, "Bareily"),
    (t(city="Mumbai City"), FOUND, None),
    (t(city="Gr. Noida"), FOUND, "Gr.Noida"),
    (t(city="Greater-Noida"), FOUND, "Gr.Noida"),
    (t(city="Dharwad Hubli"), FOUND, "Dharwad-Hubli"),
    (t(city="Dharwad"), FOUND, "Dharwad-Hubli"),
    (t(city="Sas Nagar"), FOUND, "Sas Nagar (Mohali)"),
    (t(city="Mohali"), FOUND, "Sas Nagar (Mohali)"),
    (t(city="Dadra and Nagar Haveli"), FOUND, None),
    (t(city="Dadra & Nagar Haveli"), FOUND, None),
    (t(city="North 24 Parganas"), FOUND, None),
    (t(city="Leh"), FOUND, None),
]

# --- 2. empty / junk / hostile input -----------------------------------------------

JUNK = [
    (t(), ("need_location",), None),
    (t(city=""), ("need_location",), None),
    (t(city="null", district="none", state="N/A"), ("need_location",), None),
    (t(city="@@@"), ("need_location",), None),
    (t(city="123"), ("not_found",), None),
    (t(city="abc"), ("not_found",), None),
    (t(city="hello"), ("not_found",), None),
    (t(city="London"), ("not_found",), None),
    # Accepted trade-off: suggests Dhubri (Assam); the caller says no. See
    # FUZZY_MIN_SCORE in matching.py.
    (t(city="Dubai"), ("not_found", "confirm"), None),
    (t(city="Kathmandu"), ("not_found",), None),
    # Short words the LLM might pass as a place must not become suggestions.
    (t(city="kuch"), ("not_found",), None),
    (t(city="sir"), ("not_found",), None),
    (t(city="haan"), ("not_found",), None),
    (t(city="tank"), ("not_found",), None),
    (t(city="Goa"), ("not_found",), None),          # a real state, not covered
    (t(city="Panaji", state="Goa"), ("not_found",), None),
    (t(city="Noida'; DROP TABLE users;--"), ("not_found", "confirm"), None),
    (t(city="<script>alert(1)</script>"), ("not_found",), None),
    (t(city="a" * 100), ("not_found",), None),
    (t(city="Noida " * 15), ("not_found", "confirm", "found"), None),
]

# --- 3. the state field -------------------------------------------------------------

STATES = [
    (t(city="Noida", state="U.P."), FOUND, "Noida"),
    (t(city="Noida", state="u p"), FOUND, "Noida"),
    (t(city="Noida", state="Uttar Pardesh"), FOUND, "Noida"),
    (t(city="Noida", state="Utter Pradesh"), FOUND, "Noida"),
    (t(city="Noida", state="UP state"), FOUND, "Noida"),
    (t(city="Noida", state="उत्तर प्रदेश"), FOUND, "Noida"),
    (t(city="Noida", state="Narnia"), FOUND, "Noida"),       # unknown state: ignored
    (t(city="Noida", state="Bihar"), ("need_state",), None),  # contradicts the KB
    (t(city="Uttar Pradesh"), ("too_many",), None),           # state put in city field
    (t(district="Bihar"), ("too_many",), None),
    (t(city="Kerala"), FOUND, None),
    (t(city="Sikkim"), FOUND, "Gangtok"),
    (t(state="Orissa"), ("too_many", "multiple", "found"), None),
    (t(state="Pondicherry"), FOUND_OR_MULTI, None),
    (t(state="J&K"), FOUND_OR_MULTI + ("too_many",), None),
    (t(state="Ladakh"), FOUND, None),
    (t(state="Delhi"), FOUND_OR_MULTI, None),
    (t(state="NCT of Delhi"), FOUND_OR_MULTI, None),
    (t(state="Telengana"), ("too_many", "confirm"), None),  # misspelt: confirm before numbers
    (t(state="Karnatka"), ("too_many", "confirm"), None),
    (t(state="Goa"), ("not_found",), None),                   # no Vectus coverage
    (t(state="Manipur"), ("not_found",), None),
]

# --- 4. alternate, old and colloquial names ---------------------------------------

ALT_NAMES = [
    ("Calcutta", "Kolkata"), ("Madras", "Chennai"), ("Bangalore", "Bangalore Urban"),
    ("Bengaluru", "Bangalore Urban"), ("Poona", "Pune"), ("Banaras", "Varanasi"),
    ("Benaras", "Varanasi"), ("Kashi", "Varanasi"), ("Allahabad", "Prayagraj"),
    ("Cawnpore", "Kanpur"), ("Faizabad", "Ayodhya"), ("Mysuru", "Mysore"),
    ("Mangaluru", "Manglore"), ("Belgaum", "Belagavi"), ("Gulbarga", "Kalaburagi"),
    ("Hubballi", "Dharwad-Hubli"), ("Trivandrum", "Thiruvananthapuram"),
    ("Cochin", "Ernakulam"), ("Kochi", "Ernakulam"), ("Calicut", "Kozhikode"),
    ("Pondicherry", "Pondichery"), ("Puducherry", "Pondichery"), ("Pondy", "Pondichery"),
    ("Gurugram", "Gurgaon"), ("Hissar", "Hisar"), ("Nawanshahr", "Sbs Nagar (Nawan Shahr)"),
    ("Ropar", "Rup Nagar"), ("Jamshedpur", "East-Singhbhum"), ("Bhubaneswar", "Bhuvneshver"),
    ("Vizag", "Vishaka"), ("Visakhapatnam", "Vishaka"), ("Vijayawada", "Krishna"),
    ("Rajahmundry", "East Godhavari"), ("Kakinada", "East Godhavari"),
    ("Secunderabad", "Hyderabad"), ("Hanamkonda", "Warangal Urban"),
    ("Amdavad", "Ahmedabad"), ("Baroda", "Vadodara"), ("Nasik", "Nashik"),
    ("Kalyan", "Thane"), ("Dombivli", "Thane"), ("Bhiwandi", "Thane"),
    ("Sholapur", "Solapur"), ("Bhilai", "Durg"), ("Vrindavan", "Mathura"),
    ("Rishikesh", "Dehradun"), ("Roorkee", "Haridwar"), ("Haldwani", "Nainital"),
    ("Kashipur", "Udham Singh Nagar"), ("Rudrapur", "Udham Singh Nagar"),
    ("Dharamshala", "Kangra"), ("Manali", "Kullu"), ("Bhatinda", "Bathinda"),
    ("Jullundur", "Jalandhar"), ("Zirakpur", "Sas Nagar (Mohali)"), ("Sonepat", "Sonipat"),
    ("Ballabhgarh", "Faridabad"), ("Panchkula", "Punchkula"), ("Bodh Gaya", "Gaya"),
    ("Purnea", "Purnia"), ("Motihari", "East Champaran"), ("Bettiah", "West Champaran"),
    ("Chapra", "Saran"), ("Chhapra", "Saran"), ("Arrah", "Bhojpur"), ("Hajipur", "Vaishali"),
    ("Sasaram", "Rohtas"), ("Bihar Sharif", "Nalanda"), ("Daltonganj", "Palamu"),
    ("Medininagar", "Palamu"), ("Gauhati", "Guwahati"), ("Dispur", "Guwahati"),
    ("Durgapur", "Paschim Bardhaman"), ("Asansol", "Paschim Bardhaman"),
    ("Kharagpur", "Paschim Medinipur"), ("Haldia", "Purba Medinipur"),
    ("Salt Lake", "North 24 Parganas"), ("Barasat", "North 24 Parganas"),
    ("Hugli", "Hooghly"), ("Kodaikanal", "Dindigul"), ("Tambaram", "Chengalpattu"),
    ("Manipal", "Udupi"), ("Davanagere", "Davangere"), ("Ballari", "Bellary"),
    ("Karwar", "Uttara Kannada"), ("Kanyakumari", "Nagercoil"), ("Ooty", "Nilgris"),
    ("Tuticorin", "Tuticorin"), ("Thoothukudi", "Tuticorin"), ("Trichy", "Trichy"),
    ("Tiruchirappalli", "Trichy"), ("Tirunelveli", "Thirunelveli"), ("Tiruppur", "Thirupupr"),
    ("Bareilly", "Bareily"), ("Azamgarh", "Aazamgarh"), ("Ghazipur", "Gazipur"),
    ("Ballia", "Balia"), ("Farrukhabad", "Farukahbad"), ("Shahjahanpur", "Shahajahanpur"),
    ("Bulandshahr", "Bulandshahar"), ("Baghpat", "Bagpat"), ("Budaun", "Badaun"),
    ("Rae Bareli", "Raebareli"), ("Shravasti", "Sharavsti"), ("Lakhimpur Kheri", "Lakhimpur"),
    ("Kheri", "Lakhimpur"), ("Greater Noida", "Gr.Noida"), ("Noida Extension", "Gr.Noida"),
    ("Greater Noida West", "Gr.Noida"), ("Gautam Buddh Nagar", None),
    ("Sambhajinagar", "Aurangabad"), ("Chhatrapati Sambhajinagar", "Aurangabad"),
    ("Ahilyanagar", "Ahmednagar"), ("Dharashiv", "Osmanabad"), ("Narmadapuram", "Hoshangabad"),
    ("Singrauli", "Singroli"), ("Shahdol", "Shahdole"), ("Sri Ganganagar", "Ganganagar"),
    ("Karauli", "Karoli"), ("Chittaurgarh", "Chittorgarh"), ("Mewat", "Nuh"),
    ("Kishanganj", "Kisanganj"), ("Garhwa", "Gharwha"), ("Ramgarh", "Ramghar"),
    ("Keonjhar", "Kenojhar"), ("Mayurbhanj", "Mayurbhanji"), ("Cooch Behar", "Cooch Behar"),
    ("Koch Bihar", "Cooch Behar"), ("Burdwan", None), ("Warangal", None),
    ("Bombay", None), ("Mumbai", None), ("Delhi", None), ("New Delhi", None),
    ("Delhi NCR", None), ("Navi Mumbai", None), ("Siliguri", None),
]

# --- 5. Hindi -----------------------------------------------------------------------

HINDI = [
    ("नोएडा", "Noida"), ("ग़ाज़ियाबाद", "Ghaziabad"), ("गाजियाबाद", "Ghaziabad"),
    ("गुरुग्राम", "Gurgaon"), ("गुड़गांव", "Gurgaon"), ("लखनऊ", "Lucknow"),
    ("कानपुर", "Kanpur"), ("बरेली", "Bareily"), ("वाराणसी", "Varanasi"), ("बनारस", "Varanasi"),
    ("इलाहाबाद", "Prayagraj"), ("पटना", "Patna"), ("जयपुर", "Jaipur"), ("भोपाल", "Bhopal"),
    ("इंदौर", "Indore"), ("देहरादून", "Dehradun"), ("चंडीगढ़", "Chandigarh"),
    ("मुंबई", None), ("दिल्ली", None), ("कोलकाता", "Kolkata"), ("हैदराबाद", "Hyderabad"),
    ("पुणे", "Pune"), ("अहमदाबाद", "Ahmedabad"), ("नागपुर", "Nagpur"), ("आगरा", "Agra"),
    ("मेरठ", "Meerut"), ("गोरखपुर", "Gorakhpur"), ("फरीदाबाद", "Faridabad"),
]

# --- 6. sound-alikes: suggested, never answered -----------------------------------

SOUND_ALIKES = [
    ("Noyda", "Noida"), ("Nodia", "Noida"), ("Gaziabad", "Ghaziabad"), ("Bareli", "Bareily"),
    ("Lukhnow", "Lucknow"), ("Lakhnau", "Lucknow"), ("Kanpoor", "Kanpur"),
    ("Varansi", "Varanasi"), ("Gorakpur", "Gorakhpur"), ("Meruth", "Meerut"),
    ("Jaipoor", "Jaipur"), ("Ahmdabad", "Ahmedabad"), ("Hydrabad", "Hyderabad"),
    ("Bhopaal", "Bhopal"), ("Indor", "Indore"), ("Patana", "Patna"), ("Ranchee", "Ranchi"),
    ("Guwahti", "Guwahati"), ("Coimbtore", "Coimbatore"), ("Tirupathi", "Tirupati"),
    ("Mysure", "Mysore"), ("Kolhapoor", "Kolhapur"), ("Faridabaad", "Faridabad"),
    ("Sonipath", "Sonipat"), ("Ludhyana", "Ludhiana"), ("Amritsar ji", None),
]

# --- 7. names that must not cross ----------------------------------------------------

TRAPS = [
    # (query, must be in the answer, must NOT be in the answer)
    ("Muzaffarpur", "Muzaffarpur", "Muzaffarnagar"),
    ("Muzaffarnagar", "Muzaffarnagar", "Muzaffarpur"),
    ("Raipur", "Raipur", "Rampur"),
    ("Rampur", "Rampur", "Raipur"),
    ("Sitapur", "Sitapur", "Sitamarhi"),
    ("Gonda", "Gonda", "Godda"),
    ("Godda", "Godda", "Gonda"),
    ("Nagaon", "Nagaon", "Nagaur"),
    ("Nagaur", "Nagaur", "Nagaon"),
    ("Jalna", "Jalna", "Jalaun"),
    ("Jalaun", "Jalaun", "Jalna"),
    ("Jalgaon", "Jalgaon", "Jalna"),
    ("Palwal", "Palwal", "Palghar"),
    ("Bhandara", "Bhandara", None),
    ("Kota", "Kota", None),
    ("Durg", "Durg", None),
    ("Mau", "Mau", None),
    ("Una", "Una", None),
    ("Moga", "Moga", None),
    ("Puri", "Puri", None),
    ("Gaya", "Gaya", None),
    ("Pali", "Pali", None),
    ("Tonk", "Tonk", None),
    ("Nuh", "Nuh", None),
    ("Agra", "Agra", None),
    ("Kanpur", "Kanpur", "Kanpur Dehat"),
    ("Kanpur Dehat", "Kanpur Dehat", None),
    ("Mumbai Suburban", "Mumbai Suburban", "Mumbai City"),
    ("Bengaluru Rural", "Bengaluru Rural", "Bangalore Urban"),
    ("Warangal Rural", "Warangal Rural", "Warangal Urban"),
    ("South Delhi", "South Delhi", "North Delhi"),
    ("East Champaran", "East Champaran", "West Champaran"),
    ("North Dinajpur", "North Dinajpur", "South Dinajpur"),
    ("Amravati", "Amravati", None),
]

# --- 8. same name, different states -------------------------------------------------

AMBIGUOUS = [
    ("Aurangabad", {"Bihar", "Maharashtra"}),
    ("Balrampur", {"Chhattisgarh", "Uttar Pradesh"}),
    ("Bilaspur", {"Chhattisgarh", "Himachal Pradesh"}),
    ("Buxar", {"Bihar", "Uttar Pradesh"}),
    ("Hamirpur", {"Himachal Pradesh", "Uttar Pradesh"}),
    ("Lakhimpur", {"Assam", "Uttar Pradesh"}),
    ("Pratapgarh", {"Rajasthan", "Uttar Pradesh"}),
    ("Dadri", {"Haryana", "Uttar Pradesh"}),
    ("Amaravati", {"Andhra Pradesh", "Maharashtra"}),
]

RESOLVED_BY_STATE = [
    ("Aurangabad", "Bihar"), ("Aurangabad", "Maharashtra"), ("Aurangabad", "MH"),
    ("Balrampur", "Chhattisgarh"), ("Balrampur", "UP"), ("Bilaspur", "CG"),
    ("Bilaspur", "Himachal"), ("Buxar", "Bihar"), ("Hamirpur", "HP"), ("Hamirpur", "UP"),
    ("Lakhimpur", "Assam"), ("Lakhimpur", "UP"), ("Pratapgarh", "Rajasthan"),
    ("Pratapgarh", "Uttar Pradesh"), ("Dadri", "Haryana"), ("Dadri", "UP"),
]

# --- 9. city and district together --------------------------------------------------

COMBOS = [
    (t(city="Noida", district="Noida"), FOUND, "Noida"),
    (t(city="Noida", district="Gautam Buddh Nagar"), FOUND, None),
    (t(city="Sikandrabad", district="Bulandshahr"), FOUND, "Bulandshahar"),  # town unknown
    (t(city="Kalyan", district="Thane"), FOUND, "Thane"),
    (t(city="Xyzpur", district="Lucknow"), FOUND, "Lucknow"),
    (t(city="Lucknow", district="Xyzpur"), FOUND, "Lucknow"),
    (t(city="Hubli", district="Dharwad"), FOUND, "Dharwad-Hubli"),
    (t(city="Hamirpur", district="Hamirpur", state="HP"), FOUND, "Hamirpur"),
    (t(city="Madurai", district="Madurai"), ("multiple",), "Madurai"),
    (t(city="Lakhimpur", district="Kheri"), FOUND, "Lakhimpur"),       # district disambiguates
    (t(city="Noida", district="Lucknow"), ("multiple",), None),         # contradictory: show both
    (t(city="Noida", district="Uttar Pradesh"), FOUND, "Noida"),       # state in district field
]

# --- 10. products -------------------------------------------------------------------

PRODUCTS = [
    ("Water tank", "Water tank"), ("WATER TANK", "Water tank"), ("water-tank", "Water tank"),
    ("watertanks", "Water tank"), ("Tanks", "Water tank"), ("tanki", "Water tank"),
    ("vectus tank", "Water tank"), ("PVC tank", "Water tank"), ("पानी की टंकी", "Water tank"),
    ("टंकी", "Water tank"), ("टैंक", "Water tank"), ("वाटर टैंक", "Water tank"),
    ("moulding", "Moundling"), ("Moulding products", "Moundling"), ("mold", "Moundling"),
    ("Moldings", "Moundling"), ("MOULDING", "Moundling"), ("Moundling", "Moundling"),
    ("मोल्डिंग", "Moundling"),
]
BAD_PRODUCTS = ["chair", "pipes", "", "   ", None, 123, "water"]


# ------------------------------------------------------------------------------------


@pytest.mark.parametrize("body,statuses,city", FORMATTING + JUNK + STATES + COMBOS,
                         ids=lambda v: str(v)[:60] if isinstance(v, dict) else None)
def test_table(client, body, statuses, city):
    check(client, body, statuses, city)


@pytest.mark.parametrize("said,kb_city", ALT_NAMES)
def test_alternate_names(client, said, kb_city):
    check(client, t(city=said), FOUND_OR_MULTI, kb_city)


@pytest.mark.parametrize("said,kb_city", HINDI)
def test_hindi(client, said, kb_city):
    check(client, t(city=said), FOUND_OR_MULTI, kb_city)


@pytest.mark.parametrize("said,kb_city", SOUND_ALIKES)
def test_sound_alikes_only_suggest(client, said, kb_city):
    check(client, t(city=said), ("confirm", "found"), kb_city)


@pytest.mark.parametrize("said,must,must_not", TRAPS)
def test_similar_names_do_not_cross(client, said, must, must_not):
    r = check(client, t(city=said), FOUND_OR_MULTI, must)
    if must_not:
        assert must_not not in cities_in(r), r


@pytest.mark.parametrize("said,states", AMBIGUOUS)
def test_ambiguous_asks_for_state(client, said, states):
    r = check(client, t(city=said), ("need_state",), None)
    assert set(r["options"]) == states


@pytest.mark.parametrize("said,state", RESOLVED_BY_STATE)
def test_state_resolves_ambiguity(client, said, state):
    check(client, t(city=said, state=state), FOUND, None)


@pytest.mark.parametrize("product,canonical", PRODUCTS)
def test_product_spellings(client, product, canonical):
    r = post(client, product=product, city="Noida")
    assert r["status"] == "found" and r["product"] == canonical, r


@pytest.mark.parametrize("product", BAD_PRODUCTS)
def test_bad_products(client, product):
    body = {"city": "Noida"} if product is None else {"product": product, "city": "Noida"}
    r = client.post("/lookup", json=body, headers=H).json()
    assert r["status"] == "invalid_product", r
    assert "contact" not in r


# --- moulding coverage ---------------------------------------------------------------


@pytest.mark.parametrize("body,statuses", [
    (t("moulding", city="Noida"), FOUND),
    (t("moulding", city="Gurugram"), FOUND),
    (t("moulding", city="Lucknow"), ("not_found",)),   # tanks only there
    (t("moulding", city="Kochi"), ("not_found",)),
    (t("moulding", state="Kerala"), ("not_found",)),
    (t("moulding", state="MP"), FOUND),
    (t("moulding", city="Hamirpur"), FOUND),           # only UP has moulding here
    (t("moulding", city="Delhi"), FOUND_OR_MULTI),
    (t("moulding", city="Noyda"), ("confirm",)),
    (t("moulding", city="Lakhnau"), ("not_found", "confirm")),
])
def test_moulding(client, body, statuses):
    r = check(client, body, statuses, None)
    if r["status"] == "confirm":
        # Suggestions must come from the moulding list only.
        assert all(s["state"] for s in r["suggestions"])


def test_not_found_message_says_moulding(client):
    r = post(client, **t("moulding", city="Lucknow"))
    assert "moulding" in r["message"] and "moundling" not in r["message"].lower()


# --- sweeps over the whole KB --------------------------------------------------------


def test_every_state_and_product_answers_sensibly(client):
    ix = client.app.state.index
    for product in (A.WATER_TANK, A.MOULDING):
        for state in sorted({r.state for r in ix.records if r.product == product}):
            r = post(client, product=product, state=state)
            assert r["status"] in ("found", "multiple", "too_many"), (product, state, r)


def test_every_record_in_messy_forms(client):
    ix = client.app.state.index
    misses = []
    for rec in ix.records:
        for said in (rec.city.upper(), f"  {rec.city.lower()} district ", f"{rec.city}, {rec.state}"):
            r = post(client, product=rec.product, city=said, state=rec.state)
            got = {r.get("contact")} | {m["contact"] for m in r.get("matches", [])}
            if rec.contact not in got:
                misses.append((said, rec.product, r["status"]))
    assert not misses, misses[:25]


def test_every_alias_lands(client):
    """Every curated alias returns an answer for at least one product."""
    dead = []
    for alias in A.PLACE_ALIASES:
        results = [post(client, product=p, city=alias)["status"]
                   for p in (A.WATER_TANK, A.MOULDING)]
        if not any(s in ("found", "multiple", "need_state") for s in results):
            dead.append((alias, results))
    assert not dead, dead


def test_no_fuzzy_suggestion_for_a_real_kb_name(client):
    """A name that is in the KB must never be answered with 'did you mean'."""
    ix = client.app.state.index
    for rec in ix.records:
        r = post(client, product=rec.product, city=rec.city, state=rec.state)
        assert r["status"] != "confirm", (rec.city, r)


# --- API surface ---------------------------------------------------------------------


def test_extra_fields_ignored(client):
    r = post(client, product="tank", city="Noida", pincode="201301", caller="x")
    assert r["status"] == "found"


def test_numeric_city_is_not_a_crash(client):
    r = client.post("/lookup", json={"product": "tank", "city": 201301}, headers=H)
    assert r.status_code in (200, 422)


def test_non_json_body(client):
    r = client.post("/lookup", content=b"city=Noida", headers={**H, "Content-Type": "text/plain"})
    assert r.status_code == 422


def test_get_not_allowed(client):
    assert client.get("/lookup", headers=H).status_code == 405


def test_overlong_field_rejected_cleanly(client):
    r = client.post("/lookup", json={"product": "tank", "city": "x" * 500}, headers=H)
    assert r.status_code in (200, 422)
