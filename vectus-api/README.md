# Vectus lookup API

Finds the Vectus sales contact for a caller's location and product. It replaces
knowledge-base retrieval for this data: the answer is a phone number, so it must
be looked up exactly, not retrieved by similarity.

Source of truth: `data/Vectus_KB_Revised.txt`, in the format Vectus sends it.
To update, replace the file and redeploy. Startup refuses a malformed KB.

## Contract

`POST /lookup`, header `X-API-Key`, body:

```json
{"product": "Water tank", "city": "Noida", "district": null, "state": null}
```

`product` is required (`Water tank` / `Moundling`; "tank", "moulding",
"पानी की टंकी" etc. accepted). At least one of `city`, `district` or `state` is
needed. `state` alone returns everyone covering that state.

It always answers HTTP 200. The bot branches on `status` and follows `message`:

| status | meaning | bot does |
|---|---|---|
| `found` | one person | say `salesperson` + `contact_spoken` |
| `multiple` | 2–5 people (split territories such as Madurai, Mysore, Delhi) | share each from `matches` |
| `too_many` | more than 5 people | ask for the city/district, look up again |
| `confirm` | no exact match; sound-alikes in `suggestions` | "Did you mean …?", look up again |
| `need_state` | the place exists in several states (`options`) | ask which state |
| `not_found` | nothing listed | "our team will call you back" |
| `invalid_product` | product missing or unknown | ask: water tank or moulding |
| `need_location` | no place given | ask for city/district |

A number is only ever returned for an exact or curated-alias match. A fuzzy
match only produces a `confirm`. `VectusState` is never used or returned.

Matching order: exact name → name without filler ("district", "sector 62",
"ke paas", "Area Manager for") → alias in `app/aliases.py` → fuzzy suggestion.
Add new spoken variants to `PLACE_ALIASES`; startup rejects aliases that point
nowhere.

What else it understands, all taken from real calls:

- A state inside the city text, anywhere: "Noida, UP", "Area Manager Jammu and
  Kashmir Samba". A state alone in the city field is a state lookup.
- Hindi script. Curated names answer directly; any other name is
  transliterated and matched by sound, and is only ever a `confirm`, because
  speech-to-text is unreliable (it wrote Kathua as "कटुआ" and "कट हुआ").
- Tank model names as the product (Puff, Granito, Cool, "Vectus ka cooler").
- The old KB's forms ("Agra - M", "West UP", "Delhi" for Noida/Gurgaon).
- A state that was corrected (a typo, or heard in Hindi) never releases
  numbers for a whole state without a `confirm` first.

## Tests

`pytest -q -s` runs about 450 tests, including:

- `test_matrix.py` — every record × 30+ phrasings (~25,700 requests). An
  independent oracle checks that every returned number is allowed.
- `test_misspellings.py` — ~14,000 generated typos. None may return a wrong
  number, and at least 93% must get the right answer or suggestion.
- `test_db_inputs.py` — the KB version stored in Dograh's DB (576 records, all
  looked up exactly as written there) and what real callers said on Vectus calls.
- `test_edge_cases.py` — formatting, junk, states, alternate names, Hindi,
  look-alike names, ambiguity, products and the HTTP surface.

## Deploy (on the Dograh VM)

```bash
cd ~/dograh/vectus-api
cp .env.example .env && sed -i "s/^VECTUS_API_KEY=.*/VECTUS_API_KEY=$(openssl rand -hex 32)/" .env
docker compose up -d --build
docker compose logs -f vectus-api          # "loaded 753 place records ..."
docker exec vectus-api python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/health').read())"
```

No port is published. The service joins Dograh's Docker network and is
reachable only from Dograh's containers at `http://vectus-api:8080`.

## Dograh HTTP tool

- Method `POST`, URL `http://vectus-api:8080/lookup`
- Header `X-API-Key: <value from vectus-api/.env>`
- Parameters: `product` (required), `city`, `district`, `state`, all strings
- Description: *"Find the Vectus sales contact for the caller. Call once you
  know the product and at least a city, district or state. Follow the
  `message` field in the response."*

## Develop

```bash
uv venv && uv pip install -r requirements.txt -r requirements-dev.txt
.venv/Scripts/python -m pytest -q        # includes a sweep of every KB record
```
