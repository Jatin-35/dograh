"""Tank questions from real Vectus calls, replayed through the lookup.

Every ``said`` is what speech-to-text wrote for a real customer on a Vectus
call (prod agent 10 and earlier test calls; names and phone numbers removed).
``send`` is the request the bot should make; ``raw`` cases send the customer's
words as they were, the worst case of a bot that passes text through.
``bot_said`` records what the live bot answered from its prompt table where
that was wrong — each of those cases proves the lookup would have been right.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import tanks as T

SHEET = Path(__file__).resolve().parent.parent / "data" / "Tank_details.xlsx"
COOL = [550, 750, 1100, 1500, 2000, 3000, 5000]
GRANITO = [550, 750, 1100, 1500, 2000]


@pytest.fixture(scope="module")
def cat():
    return T.load_catalog(SHEET)


def names(r):
    return [t["name"] for t in r.get("tanks", [])] + r.get("more", [])


# (call, said, send, check, bot_said)
CASES = [
    # --- "1000 L Vectus Cool": the live bot confirmed this impossible size again and again
    ("10-812", "मेरे को मैम आप डेक्टस कूल मॉडल चाहिए 1000 मतलब 1000 लीटर तक का।",
     {"model": "डेक्टस कूल", "capacity": "1000"},
     {"status": "not_available", "closest": {"lower": 750, "higher": 1100}}, None),
    ("10-800", "यह मैंने बताया था मुझे 1000 लीटर का वेक्टर स्कूल चाहिए।",
     {"model": "वेक्टर स्कूल", "capacity": 1000},
     {"status": "not_available", "closest": {"lower": 750, "higher": 1100}}, None),
    ("10-716", "A 1000 L ka Vectus cool model.",
     {"model": "A 1000 L ka Vectus cool model."},  # raw: the whole phrase as the model
     {"status": "not_available", "closest": {"lower": 750, "higher": 1100}}, None),
    ("10-583", "मुझे एक टंकी की रिक्वायरमेंट है 1000 L वेक्टर स्कूल पर्सनल पर्पस के लिए वन यूनिट।",
     {"model": "वेक्टर स्कूल", "capacity": "1000 L"},
     {"status": "not_available"}, "Bas confirm kar leti hoon, aapko 1000 litre Vectus Cool"),
    ("10-586", "मेरे को एक टंकी चाहिए फॉर मतलब स्कूल 1000 लीटर कैपेसिटी",
     {"model": "स्कूल", "capacity": "1000 लीटर"},
     {"status": "not_available"}, "aapko Vectus Cool model 1000 litre ... Sahi hai?"),
    ("10-477", "So, mere ko 1,000 L per day ka sewage pool chahiye tha.",
     {"model": "sewage pool", "capacity": "1,000 L"},
     {"status": "not_available"}, "Aapko 1000 litre Vectus Cool tanki ... Sahi hai?"),
    ("10-536", "जी, 1000 लीटर की एक यूनिट वेक्टर स्कूल चाहिए।",
     {"model": "वेक्टर स्कूल", "capacity": 1000},
     {"status": "not_available"}, "aapko 1000 litre Vectus Cool tanki, ek unit chahiye"),
    ("10-610", "एक 1000 लीटर कैपेसिटी का वाटर टैंक चाहिए ... वेक्टस कूल मॉडल रहेगा तो बेटर रहेगा।",
     {"model": "वेक्टस कूल", "capacity": 1000},
     {"status": "not_available", "closest": {"lower": 750, "higher": 1100}}, None),
    ("10-484", "मेरे को नहीं लगता कि व्यक्त उसमें पूल में 1000 1000 लीटर का ऑप्शन आता है।",
     {"model": "व्यक्त पूल", "capacity": 1000},
     {"status": "not_available"}, "Vectus Cool: 500, 750, 1000, 1500, 2000, 3000 aur 5000 litre"),

    # --- capacities the live bot stated wrongly
    ("10-633", "Doctor school mein kitne kitne litre ka tank hai aapka?",
     {"model": "Doctor school"},
     {"status": "found", "first": "Vectus Cool", "caps": COOL}, "650, 750, 1100, 1500, 2000, 3000 aur 5000"),
    ("10-488", "आप मेरे को वेबपेज स्कूल के बारे में थोड़ा बता सकते हैं क्या?",
     {"model": "वेबपेज स्कूल"},
     {"status": "found", "first": "Vectus Cool", "caps": COOL}, "650 750 1100 1500 2000 aur 5000 (no 3000)"),
    ("10-485", "एक्चुअली मुझे थोड़ा सा बैपटिस्ट स्कूल के बारे में जानना था।",
     {"model": "बैपटिस्ट स्कूल"},
     {"status": "found", "first": "Vectus Cool", "caps": COOL}, "650, 750, 1100, ..."),
    ("10-484", "क्या-क्या ऑप्शन अवेलेबल है वेक्टर स्कूल में?",
     {"model": "वेक्टर स्कूल"},
     {"status": "found", "first": "Vectus Cool", "caps": COOL}, "500, 750, 1000, 1500, 2000, 3000 aur 5000"),
    ("10-485", "हां, यह ग्रेनीटो के बारे में",
     {"model": "ग्रेनीटो"},
     {"status": "found", "first": "Vectus Granito", "caps": GRANITO}, "650, 750, 1100, 1500, aur 2000"),
    ("10-485", "T90 के बारे में बता सकते हैं थोड़ा?",
     {"model": "T90"},
     {"status": "found", "first": "Vectus T-90", "caps": [500, 750, 1000, 1500, 2000]}, None),
    ("10-478", "Nahi, mein yeh bol raha tha 700 lead par kabhi tank rehta hai kya aapke paas?",
     {"capacity": "700 lead", "brand": "Ganga"},
     {"status": "found", "names": ["Ganga Pro", "Ganga Puff"]}, "Ganga Silk aur T-90 mein 700 litre"),
    ("local1-17", "school model five thousand liter capacity.",
     {"model": "school model", "capacity": "five thousand liter"},
     {"status": "found", "first": "Vectus Cool"},
     "5000 litre mein Vectus Cool, Waterwell Italian, aur Granito, Safe"),
    ("local1-17", "(bot listed 5000 L tanks)",
     {"capacity": 5000},
     {"status": "found", "absent": "Granito"}, "Granito at 5000 litre"),

    # --- layers: the live bot could not answer these
    ("10-647", "मुझे प्रोजेक्ट के लिए black color ka tank chahiye tha jo two layer mein hai.",
     {"colour": "black", "layers": "two layer"},
     {"status": "found", "names": ["Vectus ISI tank", "Vectus Underground"]}, "only ISI Tank"),
    ("10-647", "Granito के बारे में बता दीजिए कितने लेयर का टैंक है यह? कौन-कौन से लेयर्स अवेलेबल है उसमें?",
     {"model": "Granito"},
     {"status": "found", "layers": 6, "layer_named": "Granite-Finish Layer"},
     "Mere paas Granito tank ki specific layers ke contents ki jaankari nahi hai"),
    ("10-647", "टाइटेनियम लेयर्स मींस क्या है?",
     {"model": "Granito"},
     {"status": "found", "feature_has": "titanium"},
     "Mere paas layers ki specific technical jaankari nahi hai"),
    ("10-647", "कौन-कौन सा टैंक है आपके पास कितने-कितने लेयर्स में अवेलेबल है?",
     {},
     {"status": "catalogue", "message_has": ["5 layer: Cool", "10 layer: Horizontal Tank, TenX"]},
     None),
    ("10-647", "कौन-कौन सा मॉडल है आपके उसमें लेयर मींस गंगा में?",
     {"brand": "Ganga"},
     {"status": "catalogue", "message_has": ["3 layer: Loft, Pro", "4 layer: Health, Puff, Silk"],
      "absent": "Smart"}, None),
    ("local1-7", "यह फाइव लेयर बोला आपने, मेरे को समझा नहीं। मतलब यह क्या लेयर्स क्या रहती है?",
     {"model": "Cool"},
     {"status": "found", "layers": 5, "layer_named": "Temperature-Resistant Foam Layer"}, None),

    # --- comparisons asked on real calls
    ("10-614", "Granito aur cool mein kya difference hai?",
     {"model": "Granito aur cool"},
     {"status": "found", "names": ["Vectus Granito", "Vectus Cool"]}, None),
    ("10-614", "Kaun sa select karun? Kya feature hai Cool and Garnier to ke?",
     {"model": "Cool and Garnier to"},
     {"status": "found", "names": ["Vectus Cool", "Vectus Granito"]}, None),
    ("10-614", "(after the comparison) 1000 लीटर में चाहिए मुझे।",
     {"model": "Cool and Granito", "capacity": "1000 लीटर"},
     {"status": "found", "message_has": ["Vectus Cool", "does NOT come in 1000 litre"]}, None),
    ("10-633", "विक्टर स्कूल ... विक्टर 10X तो उसमें दोनों में डिफरेंस क्या है?",
     {"model": "विक्टर स्कूल aur विक्टर 10X"},
     {"status": "found", "names": ["Vectus Cool", "Vectus TenX"]}, None),
    ("10-633", "Next mein bhi heat reflective layer hoga ... thanda rahega kya?",
     {"model": "TenX", "need": "thanda"},
     {"status": "found", "feature_has": "temperature"},
     "pani ko thanda rakhne ka vishesh feature Vectus Cool model mein hota hai"),

    # --- sizes not made
    ("10-633", "Mujhe Vectus Vectus Puffs ka tank chahiye tha 7000 L mein. Available hai?",
     {"model": "Vectus Vectus Puffs", "capacity": "7000 L"},
     {"status": "not_available", "closest": {"lower": 5000, "higher": None}}, None),
    ("local1-6", "Mere ko actually 3200 ki capacity required hai. (Puff)",
     {"model": "puff", "capacity": 3200},
     {"status": "not_available", "closest": {"lower": 3000, "higher": 5000}}, None),
    ("local1-7", "मेरे को तो 3200 का कैपेसिटी चाहिए था। आपके पास रहेगा क्या अवेलेबल?",
     {"capacity": "3200"},
     {"status": "no_match", "closest": {"lower": 3000, "higher": 5000}}, None),

    # --- capacities as heard
    ("10-826", "2000 m", {"capacity": "2000 m"}, {"status": "found", "count": 8}, None),
    ("10-825", "2000 L", {"capacity": "2000 L"}, {"status": "found", "count": 8}, None),
    ("10-646", "700 L की टैंक चाहिए।", {"capacity": "700 L की टैंक चाहिए।"},
     {"status": "found", "names": ["Vectus Smart", "Vectus Puff"]}, None),
    ("10-717", "1,000 से 1,500", {"capacity": "1,000 से 1,500"},
     {"status": "found", "message_has": ["1000-1500 litre"]}, None),
    ("10-598", "3000 L capacity mein", {"capacity": "3000 L capacity mein"},
     {"status": "found", "count": 5}, None),
    ("10-580", "एक टंकी की रिक्वायरमेंट है ₹1000", {"capacity": "₹1000"},
     {"status": "found", "count": 8}, None),
    ("10-602", "5000 लीटर 1000 लीटर कैपेसिटी की एक यूनिट", {"capacity": "5000 लीटर 1000 लीटर"},
     {"status": "found", "count": 8}, None),  # the last size said wins
    ("local1-6", "जी, 5,000 कर लीजिए फिर आप। सॉरी, 3,000 3,000",
     {"capacity": "5,000 कर लीजिए फिर आप। सॉरी, 3,000 3,000"},
     {"status": "found", "count": 5}, None),  # corrected to 3000
    ("local1-16", "hundred liter better. Well, five thousand liter capacity",
     {"capacity": "hundred liter better. Well, five thousand liter capacity"},
     {"status": "found", "count": 5}, None),
    ("local1-14", "One thousand liter capacity.", {"capacity": "One thousand liter capacity."},
     {"status": "found", "count": 8}, None),
    ("10-482", "यह खजूर लीटर के टंकी चाहिए मुझे।", {"capacity": "खजूर लीटर"},
     {"status": "catalogue", "ignored": True}, None),  # not a number: ask again
    ("10-579", "requirement이 1,000kL capacity", {"capacity": "1,000kL"},
     {"status": "found", "count": 8}, None),
    ("10-597", "मुझे नार्मल 1000 लीटर 1000 1100 लीटर वाली टंकी", {"capacity": "1000 ya 1100"},
     {"status": "found", "message_has": ["1000 or 1100 litre"]}, None),

    # --- needs asked on real calls
    ("local1-13", "मेरे यहां पर बारिश वगैरह बहुत होती है तो better model बता दीजिएगा",
     {"need": "barish"},
     {"status": "found", "weather": True}, None),
    ("local1-6", "Barish aur toofan ke liye", {"need": "barish aur toofan"},
     {"status": "found", "weather": True}, None),
    ("local1-2", "Mere ko Vectus ka cooler chahiye.", {"model": "Vectus ka cooler"},
     {"status": "found", "first": "Vectus Cool"}, None),
    ("10-566", "एक टंकी की रिक्वायरमेंट है 5000 L के वर्टिकल स्कूल की",
     {"model": "वर्टिकल स्कूल", "capacity": "5000 L"},
     {"status": "found", "message_has": ["Yes, Vectus Cool comes in 5000 litre"]}, None),
    ("10-563", "ek tanki ki requirement thi with school 5000 L.",
     {"model": "with school", "capacity": "5000 L"},
     {"status": "found", "first": "Vectus Cool"}, None),
    ("local1-18", "मुझे एक practice school model देख रहा हूं",
     {"model": "practice school model"}, {"status": "found", "first": "Vectus Cool"}, None),
    ("10-562", "(customer in Kochi, Kerala) 1000 L capacity",
     {"state": "Kerala", "capacity": 1000},
     {"status": "found", "brand": "Ganga"}, None),
]


@pytest.mark.parametrize("call,said,send,check,bot_said", CASES,
                         ids=[f"{c[0]}:{c[1][:30]}" for c in CASES])
def test_real_call(cat, call, said, send, check, bot_said):
    r = T.find(cat, **send)
    assert r["status"] == check["status"], (said, r["status"], r["message"][:200])
    if "closest" in check:
        assert r["closest_capacities"] == check["closest"]
    if "first" in check:
        assert names(r)[0] == check["first"], names(r)
    if "names" in check:
        assert names(r) == check["names"]
    if "count" in check:
        assert r["count"] == check["count"], names(r)
    if "caps" in check:
        assert r["tanks"][0]["capacities"] == check["caps"]
    if "layers" in check:
        assert r["tanks"][0]["layers"] == check["layers"]
    if "layer_named" in check:
        assert check["layer_named"] in r["tanks"][0]["layer_names"]
    if "feature_has" in check:
        assert any(check["feature_has"] in f.lower() for f in r["tanks"][0]["features"])
    for text in check.get("message_has", []):
        assert text in r["message"], r["message"]
    if "absent" in check:
        assert check["absent"] not in r["message"] and \
            all(check["absent"] not in n for n in names(r))
    if check.get("ignored"):
        assert "capacity" in r.get("ignored", {})
    if "brand" in check:
        assert r["brand"] == check["brand"]
    if check.get("weather"):
        for n in names(r):
            tank = next(t for t in cat.tanks if t.name == n)
            text = " ".join(tank.layer_names).lower()
            assert "strength" in text or "weather" in text or "stress-crack" in text
