"""Curated lookup tables. The only hand-maintained part of the service.

Keys and targets are written in normalised form (see ``text.norm``); the index
normalises them again on load and **refuses to start** if any target does not
exist in the KB, so a typo here fails loudly instead of silently never matching.

Add to these from the ``status=confirm`` and ``status=not_found`` lines in the
lookup log — they record exactly what callers said that did not match.
"""

# ---------------------------------------------------------------------------
# Products. Anything not listed falls back to a substring rule in matching.py.
# ---------------------------------------------------------------------------

WATER_TANK = "Water tank"
MOULDING = "Moundling"  # the KB's own spelling; returned as-is

PRODUCT_ALIASES: dict[str, str] = {
    "water tank": WATER_TANK,
    "water tanks": WATER_TANK,
    "watertank": WATER_TANK,
    "tank": WATER_TANK,
    "tanks": WATER_TANK,
    "tanki": WATER_TANK,
    "tankee": WATER_TANK,
    "water tanki": WATER_TANK,
    "pani ki tanki": WATER_TANK,
    "पानी की टंकी": WATER_TANK,
    "टंकी": WATER_TANK,
    "टैंक": WATER_TANK,
    "वाटर टैंक": WATER_TANK,
    "मोल्डिंग": MOULDING,
    "मोल्ड": MOULDING,
    "cool tank": WATER_TANK,
    "vectus cool tank": WATER_TANK,
    "storage tank": WATER_TANK,
    "water storage tank": WATER_TANK,
    "overhead tank": WATER_TANK,
    # Vectus tank models: callers name the model, not the category ("Puff ki
    # bata do", "Vectus ka cooler chahiye" — both in real calls).
    "silk": WATER_TANK, "smart": WATER_TANK, "t 90": WATER_TANK, "t90": WATER_TANK,
    "puff": WATER_TANK, "safe": WATER_TANK, "granito": WATER_TANK, "cool": WATER_TANK,
    "cooler": WATER_TANK, "vectus cool": WATER_TANK, "vectus cooler": WATER_TANK,
    "tenx": WATER_TANK, "ten x": WATER_TANK, "10x": WATER_TANK,
    "vectus puff": WATER_TANK, "vectus granito": WATER_TANK, "vectus silk": WATER_TANK,
    "vectus smart": WATER_TANK, "vectus safe": WATER_TANK, "vectus tenx": WATER_TANK,
    "moundling": MOULDING,
    "moulding": MOULDING,
    "mouldings": MOULDING,
    "molding": MOULDING,
    "moldings": MOULDING,
    "mould": MOULDING,
    "mold": MOULDING,
    "moulded": MOULDING,
}

# ---------------------------------------------------------------------------
# States: what a caller may say -> the KB's state, normalised.
# ---------------------------------------------------------------------------

STATE_ALIASES: dict[str, str] = {
    "up": "uttar pradesh", "u p": "uttar pradesh", "uttarpradesh": "uttar pradesh",
    "उत्तर प्रदेश": "uttar pradesh",
    "mp": "madhya pradesh", "m p": "madhya pradesh", "madhyapradesh": "madhya pradesh",
    "मध्य प्रदेश": "madhya pradesh",
    "hp": "himachal pradesh", "himachal": "himachal pradesh",
    "ap": "andhra pradesh", "andhra": "andhra pradesh",
    "mh": "maharashtra", "maharastra": "maharashtra",
    "tn": "tamil nadu", "tamilnadu": "tamil nadu",
    "jk": "jammu and kashmir", "j and k": "jammu and kashmir",
    "jammu kashmir": "jammu and kashmir", "kashmir": "jammu and kashmir",
    "wb": "west bengal", "bengal": "west bengal",
    "uk": "uttarakhand", "uttaranchal": "uttarakhand",
    "orissa": "odisha",
    "cg": "chhattisgarh", "chattisgarh": "chhattisgarh", "chhatisgarh": "chhattisgarh",
    "chattishgarh": "chhattisgarh",
    "ka": "karnataka", "kl": "kerala",
    "gj": "gujarat", "gujrat": "gujarat",
    "rj": "rajasthan", "rajastan": "rajasthan",
    "pb": "punjab", "panjab": "punjab",
    "hr": "haryana", "br": "bihar", "बिहार": "bihar",
    "jh": "jharkhand", "jharkand": "jharkhand",
    "asam": "assam",
    "ts": "telangana", "tg": "telangana",
    "dl": "delhi", "new delhi": "delhi", "दिल्ली": "delhi",
    "pondicherry": "puducherry", "pondichery": "puducherry",
    "dnh": "dadra and nagar haveli and daman and diu",
    "dadra and nagar haveli": "dadra and nagar haveli and daman and diu",
    "daman": "dadra and nagar haveli and daman and diu",
    "nct of delhi": "delhi", "nct delhi": "delhi", "delhi ncr": "delhi",
    "national capital territory of delhi": "delhi",
    "pondy": "puducherry",
    "ncr": "delhi",
    # Regions callers name instead of the state. (The old KB also used "West
    # UP"/"East UP" as its state field — Vectus's internal zones.)
    "west up": "uttar pradesh", "western up": "uttar pradesh", "paschim up": "uttar pradesh",
    "paschimi up": "uttar pradesh", "east up": "uttar pradesh", "eastern up": "uttar pradesh",
    "purvi up": "uttar pradesh", "purvanchal": "uttar pradesh", "bundelkhand": "uttar pradesh",
    "awadh": "uttar pradesh", "rohilkhand": "uttar pradesh",
    "jammu": "jammu and kashmir",
    "जम्मू कश्मीर": "jammu and kashmir", "जम्मू एंड कश्मीर": "jammu and kashmir",
    "जम्मू और कश्मीर": "jammu and kashmir", "कश्मीर": "jammu and kashmir",
    "यूपी": "uttar pradesh", "एमपी": "madhya pradesh",
    "राजस्थान": "rajasthan", "हरियाणा": "haryana", "पंजाब": "punjab",
    "मध्यप्रदेश": "madhya pradesh", "उत्तराखंड": "uttarakhand", "झारखंड": "jharkhand",
    "महाराष्ट्र": "maharashtra", "गुजरात": "gujarat", "छत्तीसगढ़": "chhattisgarh",
    "हिमाचल प्रदेश": "himachal pradesh", "पश्चिम बंगाल": "west bengal", "असम": "assam",
    "ओडिशा": "odisha", "कर्नाटक": "karnataka", "तमिलनाडु": "tamil nadu", "केरल": "kerala",
    "तेलंगाना": "telangana", "आंध्र प्रदेश": "andhra pradesh",
}

# Every state and UT, so a real state with no Vectus coverage ("Goa") is
# recognised and answered "not covered" instead of "which city?".
ALL_STATES: set[str] = {
    "andhra pradesh", "arunachal pradesh", "assam", "bihar", "chhattisgarh", "goa",
    "gujarat", "haryana", "himachal pradesh", "jharkhand", "karnataka", "kerala",
    "madhya pradesh", "maharashtra", "manipur", "meghalaya", "mizoram", "nagaland",
    "odisha", "punjab", "rajasthan", "sikkim", "tamil nadu", "telangana", "tripura",
    "uttar pradesh", "uttarakhand", "west bengal", "andaman and nicobar islands",
    "chandigarh", "dadra and nagar haveli and daman and diu", "delhi",
    "jammu and kashmir", "ladakh", "lakshadweep", "puducherry",
}

# Records whose KB state is not the only state a caller will give. The KB is
# kept as-is (it is Vectus's data); this only widens what a state filter accepts.
STATE_CORRECTIONS: dict[str, set[str]] = {
    "ghaziabad": {"uttar pradesh"},        # KB says Delhi; it is in UP
    "pondichery": {"puducherry"},          # KB says Tamil Nadu
    "karaikkal": {"puducherry"},           # KB says Tamil Nadu
    "chandigarh": {"punjab", "haryana"},   # a UT; callers name either state
    "leh ladakh": {"jammu and kashmir"},   # UT since 2019; many still say J&K
    "punchkula": {"punjab"},               # tricity with Chandigarh and Mohali
    "dadra and nagar haveli": {"gujarat", "maharashtra"},  # enclave; callers name either
    "pathankot": {"himachal pradesh"},     # border city; the old KB filed it under HP
}

# A caller who says "Delhi" may live anywhere in the NCR — Noida, Gurgaon,
# Ghaziabad, Faridabad. The old KB filed all of them under Delhi. When a place
# is named, "Delhi" as the state also accepts these; state-only "Delhi" still
# means the Delhi districts.
STATE_REGIONS: dict[str, set[str]] = {
    "delhi": {"delhi", "uttar pradesh", "haryana"},
}

# ---------------------------------------------------------------------------
# Extra keys added to records — for places that must *share* a key so the
# ambiguity is detected, rather than one silently winning.
# ---------------------------------------------------------------------------

EXTRA_PLACE_KEYS: dict[str, list[str]] = {
    # Assam's district is officially "Lakhimpur" (HQ North Lakhimpur). Without
    # this, an Assam caller saying "Lakhimpur" matches UP's record and gets the
    # wrong rep with no warning; with it, "Lakhimpur" asks for the state.
    "north lakhimpur": ["lakhimpur"],
    # Shivamogga and Shimoga are one city with two reps in the KB. Pending a
    # business decision, either spelling returns both so neither rep is hidden.
    "shivmogga": ["shimoga"],
    "shimoga": ["shivmogga"],
}

# ---------------------------------------------------------------------------
# Place aliases: caller's word -> KB keys. A target may be "key" or
# "key@state" to pin one state where the key itself is ambiguous.
# ---------------------------------------------------------------------------

PLACE_ALIASES: dict[str, list[str]] = {
    # --- renamed / official vs common names ---
    "gurugram": ["gurgaon"],
    "prayag": ["prayagraj"], "allahabad": ["prayagraj"],
    "faizabad": ["ayodhya"],
    "bengaluru": ["bangalore urban"], "bangalore": ["bangalore urban"],
    "bengaluru urban": ["bangalore urban"], "bangalore rural": ["bengaluru rural"],
    "mysuru": ["mysore"],
    "mangaluru": ["manglore"], "mangalore": ["manglore"],
    "gulbarga": ["kalaburagi"],
    "belgaum": ["belagavi"],
    "bijapur": ["vijayapura"],
    "hubballi": ["hubli"], "hubli dharwad": ["dharwad hubli"],
    "hosapete": ["vijayanagar"],
    "tumakuru": ["tumkur"],
    "chitradurga": ["chithradurga"],
    "chikkamagaluru": ["chikamangalore"], "chikmagalur": ["chikamangalore"],
    "chikkaballapur": ["chikballapur"], "chikkaballapura": ["chikballapur"],
    "shivamogga": ["shivmogga"],
    "kodagu": ["madikeri"], "coorg": ["madikeri"],
    "dakshina kannada": ["dakshin kannada"],
    "chamarajanagar": ["chamrajnagar"],
    "tiruchirappalli": ["trichy"], "tiruchi": ["trichy"],
    "thoothukudi": ["tuticorin"],
    "puducherry": ["pondichery"], "pondicherry": ["pondichery"],
    "karaikal": ["karaikkal"],
    "tirunelveli": ["thirunelveli"],
    "tiruvallur": ["thiruvallur"],
    "tiruvannamalai": ["thiruvannamalai"],
    "tiruvarur": ["thiruvarur"],
    "tiruppur": ["thirupupr"], "tirupur": ["thirupupr"],
    "tirupattur": ["thirpathur"], "tirupathur": ["thirpathur"],
    "pudukkottai": ["puthukottai"], "pudukottai": ["puthukottai"],
    "kanyakumari": ["nagercoil"],
    "nilgiris": ["nilgris"], "ooty": ["nilgris"],
    "dharmapuri": ["darmapuri"],
    "ramanathapuram": ["ramanad"], "ramnad": ["ramanad"],
    "sivaganga": ["sivagangai"],
    "kancheepuram": ["kanchipuram"],
    "viluppuram": ["villupuram"],
    "tanjore": ["thanjavur"],
    "madras": ["chennai"],
    "kovai": ["coimbatore"],
    "cochin": ["ernakulam"], "kochi": ["ernakulam"],
    "trivandrum": ["thiruvananthapuram"],
    "calicut": ["kozhikode"],
    "alleppey": ["alappuzha"],
    "quilon": ["kollam"],
    "trichur": ["thrissur"],
    "palghat": ["palakkad"],
    "cannanore": ["kannur"],
    "bombay": ["mumbai city", "mumbai suburban"],
    "mumbai": ["mumbai city", "mumbai suburban"],
    "chhatrapati sambhajinagar": ["aurangabad@maharashtra"],
    "sambhajinagar": ["aurangabad@maharashtra"],
    "dharashiv": ["osmanabad"],
    "ahilyanagar": ["ahmednagar"],
    "poona": ["pune"],
    "nasik": ["nashik"],
    "gondiya": ["gondia"],
    "bid": ["beed"],
    "visakhapatnam": ["vishaka"], "vishakhapatnam": ["vishaka"],
    "vizag": ["vishaka"], "vishakapatnam": ["vishaka"],
    "vizianagaram": ["vijayanagaram"],
    "anantapur": ["ananthapur"], "anantapuram": ["ananthapur"],
    "cuddapah": ["kadapa"], "ysr kadapa": ["kadapa"],
    "east godavari": ["east godhavari"],
    "west godavari": ["west godhavari"],
    "secunderabad": ["hyderabad"],
    "rangareddy": ["ranga reddy"],
    "kothagudem": ["bhadradri kothagudem"],
    "warangal": ["warangal urban", "warangal rural"],
    "hanamkonda": ["warangal urban"],
    "bhubaneswar": ["bhuvneshver"], "bhubaneshwar": ["bhuvneshver"],
    "bhubneshwar": ["bhuvneshver"],
    "keonjhar": ["kenojhar"], "kendujhar": ["kenojhar"],
    "mayurbhanj": ["mayurbhanji"],
    "baleshwar": ["balasore"],
    "bolangir": ["balangir"],
    "kendrapara": ["kendrapada"],
    "khordha": ["khurda"], "khorda": ["khurda"],
    "subarnapur": ["sonepur"],
    "garhwa": ["gharwha"],
    "ramgarh": ["ramghar"],
    "purbi singhbhum": ["east singhbhum"], "jamshedpur": ["east singhbhum"],
    "pashchimi singhbhum": ["west singhbhum"], "chaibasa": ["west singhbhum"],
    "seraikela": ["saraikela kharsawan"],
    "sahibganj": ["sahebganj"],
    "kishanganj": ["kisanganj"],
    "sheikhpura": ["shiekhpura"],
    "bhabua": ["kaimur"],
    "shravasti": ["sharavsti"],
    "siddharthnagar": ["sidharth nagar"], "siddharth nagar": ["sidharth nagar"],
    "ambedkar nagar": ["ambedkar"],
    "sant kabir nagar": ["santkabir nagar"],
    "ghazipur": ["gazipur"],
    "ballia": ["balia"],
    "azamgarh": ["aazamgarh"],
    "farrukhabad": ["farukahbad"],
    "shahjahanpur": ["shahajahanpur"],
    "lakhimpur kheri": ["lakhimpur@uttar pradesh"],
    "kheri": ["lakhimpur@uttar pradesh"],
    "bulandshahr": ["bulandshahar"],
    "baghpat": ["bagpat"],
    "budaun": ["badaun"],
    "rae bareli": ["raebareli"], "raibareli": ["raebareli"],
    "bareilly": ["bareily"],
    "sant ravidas nagar": ["bhadohi"],
    "greater noida": ["gr noida"],
    "rajgarh": ["rajgahr"],
    "shajapur": ["sajapur"],
    "mauganj": ["maugajn"],
    "anuppur": ["annuppur"],
    "shahdol": ["shahdole"],
    "dindori": ["dindore"],
    "sheopur": ["seopur"],
    "raisen": ["rasen"],
    "agar malwa": ["agarmalwa"], "agar": ["agarmalwa"],
    "narmadapuram": ["hoshangabad"],
    "singrauli": ["singroli"],
    "karauli": ["karoli"],
    "sri ganganagar": ["ganganagar"],
    "chittaurgarh": ["chittorgarh"],
    "shaheed bhagat singh nagar": ["sbs nagar"], "nawanshahr": ["nawan shahr"],
    "rupnagar": ["rup nagar"], "ropar": ["rup nagar"],
    "sri muktsar sahib": ["muktsar"],
    "ferozepur": ["firozpur"], "firozepur": ["firozpur"],
    "panchkula": ["punchkula"],
    "yamuna nagar": ["yamunanagar"],
    "mewat": ["nuh"],
    # Two Dadris: Charkhi Dadri (Haryana) and Dadri in Gautam Buddh Nagar (UP,
    # covered by the Greater Noida rep). Both, so the caller is asked the state.
    "dadri": ["charkhi dadri", "gr noida"],
    # Amaravati is AP's capital (Guntur district) and a common spelling of
    # Amravati, Maharashtra.
    "amaravati": ["guntur", "amravati@maharashtra"],
    "rudrapur": ["udham singh nagar"],
    "pauri": ["pauri garhwal"],
    "tehri": ["tehri garhwal"],
    "leh": ["leh ladakh"], "ladakh": ["leh ladakh"],
    "koch bihar": ["cooch behar"],
    "maldah": ["malda"],
    "east midnapore": ["purba medinipur"], "purba midnapore": ["purba medinipur"],
    "west midnapore": ["paschim medinipur"], "paschim midnapore": ["paschim medinipur"],
    "burdwan": ["purba bardhaman", "paschim bardhaman"],
    "bardhaman": ["purba bardhaman", "paschim bardhaman"],
    "asansol": ["paschim bardhaman"],
    "dakshin dinajpur": ["south dinajpur"], "uttar dinajpur": ["north dinajpur"],
    "delhi": ["east delhi", "north delhi", "west delhi", "central delhi", "south delhi"],
    "new delhi": ["east delhi", "north delhi", "west delhi", "central delhi", "south delhi"],
    # --- old and colloquial names ---
    "calcutta": ["kolkata"],
    "banaras": ["varanasi"], "benaras": ["varanasi"], "banaras city": ["varanasi"],
    "kashi": ["varanasi"],
    "cawnpore": ["kanpur"],
    "pondy": ["pondichery"],
    "hissar": ["hisar"],
    "bezwada": ["krishna"],
    "amdavad": ["ahmedabad"], "baroda": ["vadodara"],
    "sholapur": ["solapur"],
    "bhatinda": ["bathinda"],
    "jullundur": ["jalandhar"],
    "sonepat": ["sonipat"],
    "purnea": ["purnia"],
    "gauhati": ["guwahati"],
    "hugli": ["hooghly"],
    "davanagere": ["davangere"],
    "ballari": ["bellary"],
    "lakhnau": ["lucknow"], "lakhnow": ["lucknow"],
    # --- well-known towns -> the KB district that covers them. Only towns that
    # lie wholly in one district; a town that straddles lists every district. ---
    "vijayawada": ["krishna"],
    "rajahmundry": ["east godhavari"], "rajamahendravaram": ["east godhavari"],
    "kakinada": ["east godhavari"],
    "kalyan": ["thane"], "dombivli": ["thane"], "bhiwandi": ["thane"],
    "ulhasnagar": ["thane"], "ambernath": ["thane"], "mira road": ["thane"],
    "mira bhayandar": ["thane"],
    "vasai": ["palghar"], "virar": ["palghar"],
    "panvel": ["raigad"],
    "navi mumbai": ["thane", "raigad"],
    "bhilai": ["durg"],
    "vrindavan": ["mathura"],
    "rishikesh": ["dehradun"],
    "roorkee": ["haridwar"],
    "haldwani": ["nainital"], "kathgodam": ["nainital"],
    "kashipur": ["udham singh nagar"],
    "dharamshala": ["kangra"], "dharamsala": ["kangra"],
    "manali": ["kullu"],
    "zirakpur": ["sas nagar"], "kharar": ["sas nagar"],
    "ballabhgarh": ["faridabad"],
    "bodh gaya": ["gaya"],
    "motihari": ["east champaran"], "bettiah": ["west champaran"],
    "chapra": ["saran"], "chhapra": ["saran"],
    "arrah": ["bhojpur"],
    "hajipur": ["vaishali"],
    "sasaram": ["rohtas"],
    "bihar sharif": ["nalanda"], "biharsharif": ["nalanda"],
    "daltonganj": ["palamu"], "medininagar": ["palamu"],
    "dispur": ["guwahati"],
    "durgapur": ["paschim bardhaman"],
    "kharagpur": ["paschim medinipur"],
    "haldia": ["purba medinipur"], "digha": ["purba medinipur"],
    "salt lake": ["north 24 parganas"], "bidhannagar": ["north 24 parganas"],
    "barasat": ["north 24 parganas"],
    "siliguri": ["darjeeling", "jalpaiguri"],
    "kodaikanal": ["dindigul"],
    "tambaram": ["chengalpattu"],
    "manipal": ["udupi"],
    "karwar": ["uttara kannada"],
    "noida extension": ["gr noida"], "greater noida west": ["gr noida"],
    "gautam buddh nagar": ["noida", "gr noida"], "gautam budh nagar": ["noida", "gr noida"],
    "gb nagar": ["noida", "gr noida"],
    # --- Devanagari: the most common ones; extend from the log ---
    "नोएडा": ["noida"],
    "ग्रेटर नोएडा": ["gr noida"],
    "गाजियाबाद": ["ghaziabad"], "ग़ाज़ियाबाद": ["ghaziabad"],
    "गुड़गांव": ["gurgaon"], "गुरुग्राम": ["gurgaon"],
    "फरीदाबाद": ["faridabad"],
    "दिल्ली": ["east delhi", "north delhi", "west delhi", "central delhi", "south delhi"],
    "लखनऊ": ["lucknow"],
    "कानपुर": ["kanpur"],
    "आगरा": ["agra"],
    "मेरठ": ["meerut"],
    "वाराणसी": ["varanasi"], "बनारस": ["varanasi"],
    "प्रयागराज": ["prayagraj"], "इलाहाबाद": ["prayagraj"],
    "गोरखपुर": ["gorakhpur"],
    "बरेली": ["bareily"],
    "पटना": ["patna"],
    "जयपुर": ["jaipur"],
    "भोपाल": ["bhopal"],
    "इंदौर": ["indore"],
    "देहरादून": ["dehradun"],
    "चंडीगढ़": ["chandigarh"],
    "मुंबई": ["mumbai city", "mumbai suburban"], "कोलकाता": ["kolkata"],
    "चेन्नई": ["chennai"], "बेंगलुरु": ["bangalore urban"], "बैंगलोर": ["bangalore urban"],
    "हैदराबाद": ["hyderabad"], "पुणे": ["pune"], "अहमदाबाद": ["ahmedabad"],
    "सूरत": ["surat"], "नागपुर": ["nagpur"], "ग्वालियर": ["gwalior"], "झांसी": ["jhansi"],
    "मथुरा": ["mathura"], "अलीगढ़": ["aligarh"], "मुरादाबाद": ["moradabad"],
    "सहारनपुर": ["saharanpur"], "हरिद्वार": ["haridwar"], "लुधियाना": ["ludhiana"],
    "अमृतसर": ["amritsar"], "रांची": ["ranchi"], "रायपुर": ["raipur"],
    "जबलपुर": ["jabalpur"], "उज्जैन": ["ujjain"], "जोधपुर": ["jodhpur"],
    "उदयपुर": ["udaipur"], "कोटा": ["kota"], "अजमेर": ["ajmer"], "गया": ["gaya"],
    "मुजफ्फरपुर": ["muzaffarpur"], "मुज़फ़्फ़रपुर": ["muzaffarpur"],
    "मुजफ्फरनगर": ["muzaffarnagar"], "मुज़फ़्फ़रनगर": ["muzaffarnagar"],
    "भागलपुर": ["bhagalpur"], "अयोध्या": ["ayodhya"], "गोंडा": ["gonda"],
    "सीतापुर": ["sitapur"], "हापुड़": ["hapur"], "बुलंदशहर": ["bulandshahar"],
    "सोनीपत": ["sonipat"], "पानीपत": ["panipat"], "करनाल": ["karnal"],
    "रोहतक": ["rohtak"], "हिसार": ["hisar"], "अंबाला": ["ambala"],
    "ग्रेटर नॉएडा": ["gr noida"], "नॉएडा": ["noida"], "नोयडा": ["noida"],
}
