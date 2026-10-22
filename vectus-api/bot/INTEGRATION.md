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
| URL | `http://vectus-api:8080/tanks?compact=1&summary=1` |
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
| `detail` | `full` only when the customer asks what a layer does or about features beyond the layer names; otherwise leave it out (layer names and comparisons are always included). |

`compact=1` drops data repeated from `data.message`; `summary=1` makes a
single-tank answer its summary line plus layer names, without the feature
sentences, unless `detail` is `full`. Whatever a tool returns stays in
Gemini's context and is re-read on every later turn. The layer names stay in
because without them the bot looks up again for "foam / UV hai?", and a
second lookup costs more than the card. Saving: about 3–4% on product-heavy calls.

Prompt line (Tank Product Questions): `Send detail = "full" only when the
customer asks what a layer does or about features beyond the layer names.`

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

## Which Area Manager (`data.area_manager_product`)

Every answer about a product carries the `vectus_area_manager_lookup` product
Vectus set for it (confirmed by Vectus on 2026-10-22):

| Products | Area Manager product |
|---|---|
| Water tanks, tank accessories (T-Base, tank filter), bath-ware | `Water tank` |
| Household, Maali garden items, and the garden pipe (listed under bath-ware) | `Moulding` |
| Road safety | `null`: not confirmed by Vectus yet; the bot notes the requirement and does not look an Area Manager up |

Two products in one ask get one value: `Water tank` if either needs it.

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

---

# Product lookup (`POST /products`)

Answers bath-ware, household and Maali garden questions (cistern, seat cover,
taps and tap items, sinks, showers, health faucets, accessories, buckets, tubs,
dustbins, stools, planters, garden pipes...) from `data/products.json`, which
was read page by page from the Vectus catalogue PDF. Warranty is included;
prices are deliberately not in the data. Water tanks stay with `/tanks`.

## Tool (Tools → New → HTTP API)

| Field | Value |
|---|---|
| Name | `vectus_product_lookup` |
| Method | `POST` |
| URL | `http://vectus-api:8080/products?compact=1&summary=1` |
| Credential | the same one as `vectus_area_manager_lookup` |
| Timeout | `5000` ms |

**Description:**

> Looks up Vectus bath-ware, household and garden products (cistern, toilet
> seat cover, PTMT taps and items like angle cock or bib cock, kitchen sinks,
> showers, health faucets, bath accessories, buckets, tubs, dustbins, stools,
> planters, garden pipes) with their models, sizes, colours and warranty. Call
> it for ANY non-tank product question, before saying whether Vectus sells
> something. Send only the product words the customer said. Answer only from
> `data.message`; never say Vectus does or does not sell something that is not
> in this tool's result, and never quote a price.

**Parameters** (all `string`, all optional):

| name | description |
|---|---|
| `product` | The product the customer named, in their words, e.g. `seat cover`, `flush tank`, `angle cock`, `balti`, `gamla`, `garden pipe`. Only the product words, no extra sentence. |
| `model` | A model or series name if the customer said one, e.g. `Hush`, `Chic Dual`, `Ganga`. |
| `item` | A tap item if separate from the product, e.g. `angle cock`, `swan neck`. |
| `category` | `bathware`, `household` or `garden`, only when the customer asks generally what Vectus has in that range. |
| `detail` | `full` only when the customer asks about features; otherwise leave it out. |

`compact=1` drops the lists and cards that repeat `data.message` (tested: no
fact is lost); `summary=1` is accepted so the URL reads like the tank tool's;
features come only with `detail = full`.

## What it returns (`data.status`)

| status | meaning |
|---|---|
| `found` | one model (details, warranty, a tap series' items), or a tap item and every series that makes it, with warranty per series |
| `product` | Vectus sells it: the models and warranty; ask which model |
| `multiple` | two or more products in one ask ("bucket aur mug"): one answer each, all in `data.message` |
| `partial` | Vectus sells part of it, but a word is not in the list ("brass" tap, "shoe" rack): do not confirm that part |
| `model_not_found` | the product is sold, but not that model: the real models |
| `item_not_in_model` | that tap series does not make that item: the series that do |
| `category` / `catalogue` | the products in a range / in all ranges |
| `ambiguous` | a name several products share (Silk, Smart, Pro): ask which product |
| `not_sold` | plumbing supply pipes and fittings (PPR, CPVC, UPVC, GI) |
| `clarify` | a bare "pipe": garden, washing-machine and PVC waste pipes are sold, supply pipes are not |
| `use_tank_lookup` | a water tank question: call `vectus_tank_lookup` instead |
| `no_match` | not in the list (also wash basin, commode, geyser): do not say yes or no; the Area Manager will confirm |
| `unavailable` | the file failed to load: give no details, say the Area Manager will confirm |

## Main Agenda prompt change

Replace the bath-ware/household stop-gap line, and narrow "For plumbing
items/pipes/fittings, say Vectus currently does not deal" to supply pipes, with:

```
## Bath-ware, Household & Garden Questions
For ANY non-tank product question — does Vectus sell X, models, sizes, colours,
warranty, tap items like angle cock — call vectus_product_lookup with no speech
in the same turn. Send only the product words the customer said (plus model or
item if said). Answer only from data.message, in 1-2 short sentences; never quote
a price.
- found / product: answer what was asked; for a list, say a few names and ask which.
- multiple: answer each product briefly, in the order asked.
- partial: offer only what data.message says Vectus has; never confirm the rest.
- model_not_found / item_not_in_model / ambiguous: offer the options given, or ask which.
- not_sold: say Vectus does not sell plumbing supply pipes or fittings.
- clarify: ask which pipe they need.
- use_tank_lookup: call vectus_tank_lookup instead.
- no_match, unavailable or a tool error: do not say whether Vectus sells it; say
  the Area Manager will confirm, and offer their number.
Do not call it again for a question already answered in this call.
```

Attach `vectus_product_lookup` to the node. Publish only after the test calls.

## Test calls

| Say | Expect |
|---|---|
| toilet seat cover milta hai? | yes, Silk, Smart, Pro, Hush; 24 months |
| soft close seat cover | Hush |
| angle cock | the series list; 36 / 24 / 12 months by series |
| PPR pipe | Vectus does not sell supply pipes |
| brass tap | the brass kitchen tap with flexible spout, 12 months |
| chrome tap | no chrome tap confirmed; PTMT taps offered |
| geyser | not confirmed either way; Area Manager will confirm |
| 1000 litre tanki | the tank lookup answers, not this tool |

## Updating the products

Edit `data/products.json`, run `python -m pytest tests/test_products*.py`, then on
the VM: `cd ~/dograh/vectus-api && docker compose up -d --build` and check
`/health` shows `"products": 29` and `"products_error": null`. A file with a
hole in it (a product without models, a model without details, a warranty not
like "24 months", a price in a description, a tap series without items) is
never half-used: `/products` answers `unavailable` and `products_error` names
the product, while the Area Manager and tank lookups keep working.
