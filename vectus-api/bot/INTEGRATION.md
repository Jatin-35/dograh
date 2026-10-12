# Wiring the Vectus bot to the lookup API

Workflow: **Vectus Smart Care - outbound**, node **Main Agenda and Questions**.
Only that node changes. Do the steps in order and **publish** at the end —
saved drafts do not reach live calls.

## 1. Credential (Settings → Credentials → New)

| Field | Value |
|---|---|
| Name | `vectus-lookup-key` |
| Type | **API Key** |
| Header name | `X-API-Key` |
| API key | the `VECTUS_API_KEY` value from `~/dograh/vectus-api/.env` on the VM |

The key lives in the credential store, not in the tool's headers, so it is
masked in logs and tool-test output.

## 2. Tool (Tools → New → HTTP API)

| Field | Value |
|---|---|
| Name | `vectus_area_manager_lookup` |
| Method | `POST` |
| URL | `http://vectus-api:8080/lookup` (no trailing slash needed; both work) |
| Credential | `vectus-lookup-key` |
| Timeout | `5000` ms (the API answers in ~5 ms; this only matters if it is down) |
| Custom message | leave empty |

**Description** (the model reads this):

> Finds the Vectus Area Manager for the customer's location. Call it once the
> product and the customer's city, district or state are known. The response's
> `data.status` says what to do next — follow `data.message`. Never say an Area
> Manager name or number that is not in this tool's result.

**Parameters:**

| name | type | required | description |
|---|---|---|---|
| `product` | string | yes | `Water tank` for any water tank, tanki or tank model (Cool, Puff, Granito, Safe, Silk, Smart, T-90, TenX). `Moulding` for moulding products. |
| `city` | string | no | The customer's city or town, in English letters only (write `Samba`, not `सांबा`). Only the place name — no "Area Manager", "district", "mein". |
| `district` | string | no | The district, only if the customer said one. English letters. |
| `state` | string | no | The state, only if the customer said one. English letters. |

Test it from the tool page with `{"product": "Water tank", "city": "Samba"}` →
the response `data` must show Balvinder Kumar, 7006485067.

## 3. Node: Main Agenda and Questions

1. **Knowledge base:** remove `Vectus_KB_Flat_Records.txt` from the node. It is
   the only document attached, and it is what the bot read wrong numbers from.
   Product details are already written into the prompt itself.
2. **Tools:** attach `vectus_area_manager_lookup`.
3. **Prompt:** replace the whole `## Location & Area Manager` section (from
   that heading down to the line before `## Complaint / Warranty Support`)
   with the block below.
4. In `## Clarification Rules`, change "Do not retrieve Area Manager details
   until location is clear." to "Do not call vectus_area_manager_lookup until
   the city or district is clear."

```
## Location & Area Manager
Ask once: "Aapka city aur state kaunsa hai?" If noisy, confirm the normalized
city + state once, then wait.

Area Manager details come ONLY from the vectus_area_manager_lookup tool. Once
the city or district is clear, call the tool directly (no speech in the same
turn) with:
- product: "Water tank" for water tank/tanki/any tank model; "Moulding" for
  moulding products.
- city: only the place name in English letters (Samba, not सांबा). No extra
  words.
- district, state: only if the customer said them.
For Household or Bath-ware requirements, do not call the tool; use the
no-match line below.

Read data.status in the tool result (ignore the outer "status") and act:
- found: give the handoff with data.salesperson and data.contact_spoken.
- multiple: "Aapke area ke liye [number of entries in data.matches] Area Managers hain." Then for each
  entry in data.matches: "[salesperson] ji, unka number hai [contact_spoken]."
- confirm: ask once: "Kya aap [first suggestion city], [its state] ki baat kar
  rahe hain?" If yes, call the tool again with that city and state. If no, ask
  the city once more; if still no match, use the no-match line.
- need_state: "[data.place] kis state mein hai — [data.options joined by ya]?"
  Then call the tool again with the same city plus that state.
- too_many or need_location: ask "Aapka city ya district kaunsa hai?" and call
  the tool again.
- invalid_product: ask "Aapko water tank chahiye ya moulding products?" and
  call again.
- not_found, a tool error, or no data: use the no-match line.
Call the tool at most 3 times per call. Never repeat a call with the same
inputs.

Never speak an Area Manager name or number that is not in this turn's tool
result. Always speak the number digit by digit, exactly as contact_spoken.

Handoff format:
"Abhi main Area Manager ki details share kar rahi hoon. Aap please note kar
lijiye. Yehi details aapko WhatsApp par bhi share ho jayengi. Aapke area ke
Area Manager [salesperson] hain. Unka number hai [contact_spoken]."

No-match line:
"Is location ke liye exact Area Manager detail clear nahi aa rahi hai. Main
requirement note kar leti hoon, team confirm kar degi."

If customer doubts Area Manager info: call the tool again with the same
product, city and state, and repeat only that result. Do not restart
requirement questions.
```

## 4. Publish, then test on prod

Publish the workflow, then place these calls:

| Say | Expect |
|---|---|
| tanki, Samba, Jammu Kashmir | Balvinder Kumar, 7 0 0 6 4 8 5 0 6 7 (RAG gave a made-up number here) |
| water tank, Noida | Sonu Kumar |
| tank, Lakhimpur | the bot asks UP or Assam |
| tank, a city said in Hindi (e.g. "बरेली") | Dharmendra Singh (the bot should pass `Bareilly`) |
| tank, Madurai | two Area Managers |
| tank, a city Vectus does not cover | no-match line |

Rollback: re-publish the previous workflow version (the tool and credential
can stay; they do nothing unless the node uses them).

---

# Tank lookup (`POST /tanks`)

Answers every water-tank product question (capacity, colour, layers,
guarantee, model, comparison) from `data/Tank_details.xlsx`, by exact filter
over all rows. Replaces the product table that used to be written into the
Main Agenda prompt (which had drifted from the Excel on 8 models).

## Tool (Tools → New → HTTP API)

| Field | Value |
|---|---|
| Name | `vectus_tank_lookup` |
| Method | `POST` |
| URL | `http://vectus-api:8080/tanks` |
| Credential | the same one as `vectus_area_manager_lookup` |
| Timeout | `5000` ms |

**Description:**

> Looks up Vectus, Ganga and Waterwell water tanks: capacities, colours,
> layers, guarantee and model details. Call it for ANY tank product question
> or before confirming a capacity. Send only what the customer said; every
> field is optional. Answer only from `data.message` — never state a capacity,
> colour, layer count or guarantee that is not in this tool's result.

**Parameters** (all `string`, all optional):

| name | description |
|---|---|
| `model` | Tank model(s) the customer named, e.g. `Cool`, `Puff`, `T-90`, `TenX`, or `Cool aur Puff` to compare. Empty if none. |
| `capacity` | Litres the customer asked for, e.g. `1100`, or a range `1000 se 1500`. |
| `colour` | Colour the customer asked for, e.g. `green`, `peela`. |
| `layers` | Number of layers asked for, e.g. `5`. |
| `guarantee` | Guarantee years asked for, e.g. `15`. |
| `need` | What the tank is for, only if said: `cooling` (heat/garmi/thanda), `underground`, `small space`, `commercial`, `chemical`. |
| `brand` | `Vectus`, `Ganga` or `Waterwell`, only if the customer named one. |
| `state` | The customer's state, so the right brand is used (south → Ganga). |

## What it returns (`data.status`)

| status | meaning |
|---|---|
| `found` | the matching tanks (at most 4; `more` lists the rest) |
| `not_available` | the named model lacks that capacity/colour: its real values, closest capacities, and other tanks that do have it |
| `no_match` | no tank has that combination: closest capacities, or what each relaxed filter gives |
| `not_in_brand` | the brand sells it under another name (Vectus Pro → Vectus Smart), or only another brand has it (Ganga Mini → Vectus Mini) |
| `unknown_model` / `unknown_colour` | not in the catalogue; the real list is returned |
| `catalogue` | nothing to filter on: ask the capacity |
| `unavailable` | the sheet failed to load: give no details, say the Area Manager will confirm |

`data.message` always carries the facts in one sentence per tank, ready to
paraphrase. Home enquiries are not offered Chemical, ISI, Underground or the
15,000+ L tank unless the customer asks for them, says commercial/underground,
or nothing else matches (black, 10000 L, 1–2 layers).

## Main Agenda prompt change

Delete these sections whole: `## Product Information Usage`,
`## Water Tank Product Database` (every layer subsection) and
`## Capacity / Option Matching`. Put this in their place:

```
## Tank Product Questions
For ANY tank question — capacity, colour, layers, guarantee, model details,
comparison, "which tanks in X litre", or before confirming a capacity — call
vectus_tank_lookup with no speech in the same turn. Send only what the customer
said: model, capacity, colour, layers, guarantee, need, brand, plus state if known.
Answer only from data.message, in 1-2 short sentences: say tank names and the
detail asked, not the full capacity list unless asked. Never state a capacity,
colour, layer count or guarantee that is not in this turn's tool result.
- found with several tanks: say the names, ask which the customer prefers.
- not_available / no_match: say it is not available and offer the closest options given.
- not_in_brand: offer the tank named in data.message.
- unknown_model: confirm the model name once.
- unavailable or a tool error: give no tank details; say the Area Manager will confirm them.
Do not call it again for a question already answered in this call.
```

In `## Recommendation + FAQ`, replace the heat/rooftop sentence with:
`For heat/rooftop/sunlight/cooling, call vectus_tank_lookup with need = cooling.`

Attach `vectus_tank_lookup` to the node. Publish only after the test calls.

## Updating the catalogue

Replace `data/Tank_details.xlsx` (same columns), then on the VM:
`cd ~/dograh/vectus-api && docker compose up -d --build`, and check
`/health` shows `"tanks": 39` and `"tanks_error": null`. A sheet it cannot
read (missing column, a name without a brand, no capacities, a blank
guarantee) is never half-used: `/tanks` answers `unavailable` and
`tanks_error` says which row is wrong, while the Area Manager lookup keeps
working. Fix the sheet and rebuild.
