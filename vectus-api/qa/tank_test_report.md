# Tank lookup — scenario test report

Run against `http://127.0.0.1:18080` on 2026-10-12 18:22. **181/181 passed.** Expected answers were written by hand from `Tank_details.xlsx` (Vectus brand unless a state or brand is given).


## capacity

| ID | Customer says | Bot sends | Result | Tanks returned | Pass |
|---|---|---|---|---|---|
| A01 | 1100 litre chahiye | `{"capacity": "1100"}` | found | Vectus Safe, Vectus Cool, Vectus Granito | ✅ |
| A02 | 550 litre | `{"capacity": 550}` | found | Vectus Safe, Vectus Cool, Vectus Granito | ✅ |
| A03 | 700 litre | `{"capacity": 700}` | found | Vectus Smart, Vectus Puff | ✅ |
| A04 | 500 litre | `{"capacity": 500}` | found | Vectus Loft, Vectus Smart, Vectus Puff, Vectus Silk, Vectus T-90, Vectus TenX | ✅ |
| A05 | 750 litre | `{"capacity": 750}` | found | Vectus Safe, Vectus Silk, Vectus Cool, Vectus Granito, Vectus T-90, Vectus TenX | ✅ |
| A06 | 1000 litre | `{"capacity": 1000}` | found | Vectus Loft, Vectus Mini, Vectus Smart, Vectus Puff, Vectus Silk, Vectus T-90, Vectus Horizontal Tank, Vectus TenX | ✅ |
| A07 | 1500 litre | `{"capacity": 1500}` | found | Vectus Smart, Vectus Puff, Vectus Safe, Vectus Silk, Vectus Cool, Vectus Granito, Vectus T-90, Vectus TenX | ✅ |
| A08 | 2000 litre | `{"capacity": 2000}` | found | Vectus Smart, Vectus Puff, Vectus Safe, Vectus Silk, Vectus Cool, Vectus Granito, Vectus T-90, Vectus TenX | ✅ |
| A09 | 3000 litre | `{"capacity": 3000}` | found | Vectus Smart, Vectus Puff, Vectus Safe, Vectus Cool, Vectus TenX | ✅ |
| A10 | 5000 litre | `{"capacity": 5000}` | found | Vectus Smart, Vectus Puff, Vectus Safe, Vectus Cool, Vectus TenX | ✅ |
| A11 | 100 litre | `{"capacity": 100}` | found | Vectus Loft | ✅ |
| A12 | 300 litre | `{"capacity": 300}` | found | Vectus Loft | ✅ |
| A13 | 10000 litre | `{"capacity": 10000}` | found | Vectus Chemical Tank, Vectus ISI tank | ✅ |
| A14 | 20000 litre | `{"capacity": 20000}` | found | Vectus Group | ✅ |
| A15 | 30000 litre | `{"capacity": "30,000"}` | found | Vectus Group | ✅ |

## not made

| ID | Customer says | Bot sends | Result | Tanks returned | Pass |
|---|---|---|---|---|---|
| B01 | 1200 litre | `{"capacity": 1200}` | no_match (closest 1100 / 1500) | — | ✅ |
| B02 | 400 litre | `{"capacity": 400}` | no_match (closest 300 / 500) | — | ✅ |
| B03 | 600 litre | `{"capacity": 600}` | no_match (closest 550 / 700) | — | ✅ |
| B04 | 800 litre | `{"capacity": 800}` | no_match (closest 750 / 1000) | — | ✅ |
| B05 | 1800 litre | `{"capacity": 1800}` | no_match (closest 1500 / 2000) | — | ✅ |
| B06 | 2500 litre | `{"capacity": 2500}` | no_match (closest 2000 / 3000) | — | ✅ |
| B07 | 4000 litre | `{"capacity": 4000}` | no_match (closest 3000 / 5000) | — | ✅ |
| B08 | 7000 litre | `{"capacity": 7000}` | no_match (closest 5000 / 10000) | — | ✅ |
| B09 | 12000 litre | `{"capacity": 12000}` | no_match (closest 10000 / 15000) | — | ✅ |
| B10 | 50000 litre | `{"capacity": 50000}` | no_match (closest 30000 / None) | — | ✅ |
| B11 | 50 litre | `{"capacity": 50}` | no_match (closest None / 100) | — | ✅ |

## spoken

| ID | Customer says | Bot sends | Result | Tanks returned | Pass |
|---|---|---|---|---|---|
| C01 | gyarah sau litre | `{"capacity": "gyarah sau"}` | found | Vectus Safe, Vectus Cool, Vectus Granito | ✅ |
| C02 | saade saat sau | `{"capacity": "saade saat sau"}` | found | Vectus Safe, Vectus Silk, Vectus Cool, Vectus Granito, Vectus T-90, Vectus TenX | ✅ |
| C03 | dedh hazaar | `{"capacity": "dedh hazaar"}` | found | Vectus Smart, Vectus Puff, Vectus Safe, Vectus Silk, Vectus Cool, Vectus Granito, Vectus T-90, Vectus TenX | ✅ |
| C04 | do hazaar litre | `{"capacity": "do hazaar"}` | found | Vectus Smart, Vectus Puff, Vectus Safe, Vectus Silk, Vectus Cool, Vectus Granito, Vectus T-90, Vectus TenX | ✅ |
| C05 | paanch hazaar | `{"capacity": "paanch hazaar"}` | found | Vectus Smart, Vectus Puff, Vectus Safe, Vectus Cool, Vectus TenX | ✅ |
| C06 | 1.1k | `{"capacity": "1.1k"}` | found | Vectus Safe, Vectus Cool, Vectus Granito | ✅ |
| C07 | 1100L | `{"capacity": "1100L"}` | found | Vectus Safe, Vectus Cool, Vectus Granito | ✅ |
| C08 | 1100.0 (LLM float) | `{"capacity": 1100.0}` | found | Vectus Safe, Vectus Cool, Vectus Granito | ✅ |
| C09 | 1000 ya 1500 (choice) | `{"capacity": "1000 ya 1500"}` | found | Vectus Loft, Vectus Mini, Vectus Smart, Vectus Puff, Vectus Safe, Vectus Silk, Vectus Cool, Vectus Granito, Vectus T-90, Vectus Horizontal Tank, Vectus TenX | ✅ |
| C10 | 300 tak (up to) | `{"capacity": "300 tak"}` | found | Vectus Loft | ✅ |
| C11 | 5000 se upar | `{"capacity": "5000 se upar"}` | found | Vectus Smart, Vectus Puff, Vectus Safe, Vectus Cool, Vectus TenX | ✅ |
| C12 | 1000 se 1500 ke beech | `{"capacity": "1000 se 1500 ke beech"}` | found | Vectus Loft, Vectus Mini, Vectus Smart, Vectus Puff, Vectus Safe, Vectus Silk, Vectus Cool, Vectus Granito, Vectus T-90, Vectus Horizontal Tank, Vectus TenX | ✅ |
| C13 | 'bada wala' (no number) | `{"capacity": "bada wala"}` | catalogue | — | ✅ |

## colour

| ID | Customer says | Bot sends | Result | Tanks returned | Pass |
|---|---|---|---|---|---|
| D01 | green colour mein | `{"colour": "green"}` | found | Vectus Smart, Vectus Safe | ✅ |
| D02 | neela | `{"colour": "neela"}` | found | Vectus Smart, Vectus Safe | ✅ |
| D03 | peela tank | `{"colour": "peela"}` | found | Vectus T-90, Vectus TenX | ✅ |
| D04 | yellow | `{"colour": "yellow"}` | found | Vectus T-90, Vectus TenX | ✅ |
| D05 | safed | `{"colour": "safed"}` | found | Vectus Loft, Vectus Mini, Vectus Smart, Vectus Puff, Vectus Safe, Vectus Silk, Vectus Cool, Vectus Horizontal Tank, Vectus TenX | ✅ |
| D06 | kala / black | `{"colour": "kala"}` | found | Vectus Chemical Tank, Vectus ISI tank, Vectus Underground, Vectus Group | ✅ |
| D07 | ruby | `{"colour": "ruby"}` | found | Vectus Granito | ✅ |
| D08 | emerald | `{"colour": "Emerald"}` | found | Vectus Granito | ✅ |
| D09 | pearl | `{"colour": "pearl"}` | found | Vectus Granito | ✅ |
| D10 | red (not in sheet) | `{"colour": "red"}` | unknown_colour | — | ✅ |
| D11 | green ya blue | `{"colour": "green ya blue"}` | found | Vectus Smart, Vectus Safe | ✅ |
| D12 | Ganga, green | `{"colour": "green", "brand": "Ganga"}` | found | Ganga Health | ✅ |
| D13 | American spelling field | `{"color": "green"}` | found | Vectus Smart, Vectus Safe | ✅ |

## cap+colour

| ID | Customer says | Bot sends | Result | Tanks returned | Pass |
|---|---|---|---|---|---|
| E01 | 1100 green | `{"capacity": 1100, "colour": "green"}` | found | Vectus Safe | ✅ |
| E02 | 1000 green | `{"capacity": 1000, "colour": "green"}` | found | Vectus Smart | ✅ |
| E03 | 1500 blue | `{"capacity": 1500, "colour": "blue"}` | found | Vectus Smart, Vectus Safe | ✅ |
| E04 | 1100 yellow | `{"capacity": 1100, "colour": "yellow"}` | no_match (closest 1000 / 1500) | — | ✅ |
| E05 | 3000 yellow | `{"capacity": 3000, "colour": "yellow"}` | found | Vectus TenX | ✅ |
| E06 | 3000 ruby | `{"capacity": 3000, "colour": "ruby"}` | no_match (closest 2000 / None) | — | ✅ |
| E07 | 10000 white | `{"capacity": 10000, "colour": "white"}` | no_match (closest 5000 / None) | — | ✅ |
| E08 | 200 white | `{"capacity": 200, "colour": "white"}` | found | Vectus Loft | ✅ |
| E09 | 5000 black | `{"capacity": 5000, "colour": "black"}` | found | Vectus Chemical Tank, Vectus ISI tank, Vectus Underground | ✅ |

## model

| ID | Customer says | Bot sends | Result | Tanks returned | Pass |
|---|---|---|---|---|---|
| F01 | Vectus Cool batao | `{"model": "Vectus Cool"}` | found | Vectus Cool | ✅ |
| F02 | Smart | `{"model": "Smart"}` | found | Vectus Smart | ✅ |
| F03 | Mini | `{"model": "Mini"}` | found | Vectus Mini | ✅ |
| F04 | Loft | `{"model": "Loft"}` | found | Vectus Loft | ✅ |
| F05 | TenX | `{"model": "TenX"}` | found | Vectus TenX | ✅ |
| F06 | Granito | `{"model": "Granito"}` | found | Vectus Granito | ✅ |
| F07 | Silk | `{"model": "Silk"}` | found | Vectus Silk | ✅ |
| F08 | T-90 | `{"model": "T-90"}` | found | Vectus T-90 | ✅ |
| F09 | Chemical tank | `{"model": "chemical"}` | found | Vectus Chemical Tank | ✅ |
| F10 | ISI tank | `{"model": "ISI"}` | found | Vectus ISI tank | ✅ |
| F11 | Underground | `{"model": "underground"}` | found | Vectus Underground | ✅ |
| F12 | Vectus group | `{"model": "group"}` | found | Vectus Group | ✅ |
| F13 | Horizontal | `{"model": "horizontal"}` | found | Vectus Horizontal Tank | ✅ |
| F14 | Safe | `{"model": "safe"}` | found | Vectus Safe | ✅ |
| F15 | Puff | `{"model": "puff"}` | found | Vectus Puff | ✅ |

## model+cap

| ID | Customer says | Bot sends | Result | Tanks returned | Pass |
|---|---|---|---|---|---|
| G01 | Cool 1100? | `{"model": "Cool", "capacity": 1100}` | found | Vectus Cool | ✅ |
| G02 | Smart 1100? | `{"model": "Smart", "capacity": 1100}` | not_available (closest 1000 / 1500) | — | ✅ |
| G03 | Granito 3000? | `{"model": "Granito", "capacity": 3000}` | not_available (closest 2000 / None) | — | ✅ |
| G04 | Cool 1000? | `{"model": "Cool", "capacity": 1000}` | not_available (closest 750 / 1100) | — | ✅ |
| G05 | Mini 500? | `{"model": "Mini", "capacity": 500}` | not_available (closest None / 1000) | — | ✅ |
| G06 | T-90 1100? | `{"model": "T-90", "capacity": 1100}` | not_available (closest 1000 / 1500) | — | ✅ |
| G07 | TenX 5000? | `{"model": "TenX", "capacity": 5000}` | found | Vectus TenX | ✅ |
| G08 | Silk 700? | `{"model": "Silk", "capacity": 700}` | not_available (closest 500 / 750) | — | ✅ |
| G09 | Loft 100? | `{"model": "Loft", "capacity": 100}` | found | Vectus Loft | ✅ |
| G10 | Loft 2000? | `{"model": "Loft", "capacity": 2000}` | not_available (closest 1000 / None) | — | ✅ |
| G11 | Underground 3000? | `{"model": "underground", "capacity": 3000}` | not_available (closest 2000 / 5000) | — | ✅ |
| G12 | Chemical 10000? | `{"model": "chemical", "capacity": 10000}` | found | Vectus Chemical Tank | ✅ |
| G13 | Safe 650? (old prompt's wrong value) | `{"model": "Safe", "capacity": 650}` | not_available (closest 550 / 750) | — | ✅ |
| G14 | Cool 1200? | `{"model": "Cool", "capacity": 1200}` | not_available (closest 1100 / 1500) | — | ✅ |

## model+colour

| ID | Customer says | Bot sends | Result | Tanks returned | Pass |
|---|---|---|---|---|---|
| H01 | Puff blue? | `{"model": "Puff", "colour": "blue"}` | not_available | — | ✅ |
| H02 | Safe green? | `{"model": "Safe", "colour": "green"}` | found | Vectus Safe | ✅ |
| H03 | TenX green? | `{"model": "TenX", "colour": "green"}` | not_available | — | ✅ |
| H04 | Granito white? | `{"model": "Granito", "colour": "white"}` | not_available | — | ✅ |
| H05 | TenX yellow? | `{"model": "TenX", "colour": "yellow"}` | found | Vectus TenX | ✅ |

## model+colour+cap

| ID | Customer says | Bot sends | Result | Tanks returned | Pass |
|---|---|---|---|---|---|
| H06 | Safe 1100 green? | `{"model": "Safe", "capacity": 1100, "colour": "green"}` | found | Vectus Safe | ✅ |
| H07 | Smart 1100 green? | `{"model": "Smart", "capacity": 1100, "colour": "green"}` | not_available (closest 1000 / 1500) | — | ✅ |

## layers

| ID | Customer says | Bot sends | Result | Tanks returned | Pass |
|---|---|---|---|---|---|
| I01 | 5 layer wala | `{"layers": 5}` | found | Vectus Cool | ✅ |
| I02 | 4 layer | `{"layers": "4 layer"}` | found | Vectus Puff, Vectus Safe, Vectus Silk | ✅ |
| I03 | 6 layer | `{"layers": 6}` | found | Vectus Granito, Vectus T-90 | ✅ |
| I04 | 10 layer | `{"layers": 10}` | found | Vectus Horizontal Tank, Vectus TenX | ✅ |
| I05 | 3 layer | `{"layers": "teen"}` | found | Vectus Loft, Vectus Mini, Vectus Smart | ✅ |
| I06 | 1 layer | `{"layers": 1}` | found | Vectus Chemical Tank | ✅ |
| I07 | 7 layer (none) | `{"layers": 7}` | no_match | — | ✅ |
| I08 | Waterwell 5 layer | `{"layers": 5, "brand": "Waterwell"}` | found | Waterwell Italian | ✅ |

## guarantee

| ID | Customer says | Bot sends | Result | Tanks returned | Pass |
|---|---|---|---|---|---|
| I09 | 15 saal guarantee | `{"guarantee": "15 saal"}` | found | Vectus Mini, Vectus Safe, Vectus Cool, Vectus Granito, Vectus Horizontal Tank, Vectus TenX | ✅ |
| I10 | 10 saal | `{"guarantee": 10}` | found | Vectus Loft, Vectus Smart, Vectus Puff, Vectus Silk, Vectus T-90 | ✅ |
| I11 | bina guarantee | `{"guarantee": "no guarantee"}` | found | Vectus Chemical Tank, Vectus Underground, Vectus Group | ✅ |
| I12 | 20 saal (none) | `{"guarantee": 20}` | no_match | — | ✅ |

## guarantee+cap

| ID | Customer says | Bot sends | Result | Tanks returned | Pass |
|---|---|---|---|---|---|
| I13 | 1100 with 15 saal | `{"capacity": 1100, "guarantee": 15}` | found | Vectus Safe, Vectus Cool, Vectus Granito | ✅ |
| I14 | 1000 with 15 saal | `{"capacity": 1000, "guarantee": 15}` | found | Vectus Mini, Vectus Horizontal Tank, Vectus TenX | ✅ |

## mould

| ID | Customer says | Bot sends | Result | Tanks returned | Pass |
|---|---|---|---|---|---|
| I15 | blow moulded 1000 | `{"capacity": 1000, "mould": "blow"}` | found | Vectus Loft, Vectus Silk, Vectus T-90, Vectus Horizontal Tank, Vectus TenX | ✅ |

## need

| ID | Customer says | Bot sends | Result | Tanks returned | Pass |
|---|---|---|---|---|---|
| J01 | garmi mein thanda rahe | `{"need": "garmi mein thanda"}` | found | Vectus Puff, Vectus Cool, Vectus Granito, Vectus T-90, Vectus Horizontal Tank, Vectus TenX | ✅ |
| J02 | thanda + 1100 | `{"need": "thanda pani", "capacity": 1100}` | found | Vectus Cool, Vectus Granito | ✅ |
| J03 | underground 2000 | `{"need": "underground", "capacity": 2000}` | found | Vectus Underground | ✅ |
| J04 | kam jagah (small space) | `{"need": "kam jagah"}` | found | Vectus Loft, Vectus Mini, Vectus Horizontal Tank | ✅ |
| J05 | commercial 20000 | `{"need": "commercial", "capacity": 20000}` | found | Vectus Group | ✅ |
| J06 | chemical storage | `{"need": "chemical"}` | found | Vectus Chemical Tank | ✅ |
| J07 | 'sasta' (unknown) | `{"need": "sasta", "capacity": 700}` | found | Vectus Smart, Vectus Puff | ✅ |

## brand

| ID | Customer says | Bot sends | Result | Tanks returned | Pass |
|---|---|---|---|---|---|
| K01 | Tamil Nadu, 1100 | `{"state": "Tamil Nadu", "capacity": 1100}` | found | Ganga Health, Ganga Cool, Ganga Granito | ✅ |
| K02 | Kerala, green | `{"state": "Kerala", "colour": "green"}` | found | Ganga Health | ✅ |
| K03 | Karnataka (KA) | `{"state": "KA", "model": "Cool"}` | found | Ganga Cool | ✅ |
| K04 | Telangana | `{"state": "Telangana", "model": "Puff"}` | found | Ganga Puff | ✅ |
| K05 | Andhra Pradesh | `{"state": "AP", "model": "TenX"}` | found | Ganga TenX | ✅ |
| K06 | UP default Vectus | `{"state": "UP", "capacity": 1100}` | found | Vectus Safe, Vectus Cool, Vectus Granito | ✅ |
| K07 | TN but asks Vectus | `{"state": "TN", "brand": "Vectus", "model": "Cool"}` | found | Vectus Cool | ✅ |
| K08 | Waterwell 1100 | `{"brand": "Waterwell", "capacity": 1100}` | found | Waterwell Health, Waterwell Italian, Waterwell Granito | ✅ |
| K09 | Sintex (competitor) | `{"brand": "Sintex", "capacity": 700}` | found | Vectus Smart, Vectus Puff | ✅ |
| K10 | all brands, Cool | `{"brand": "all", "model": "Cool"}` | found | Vectus Cool, Ganga Cool, Waterwell Italian | ✅ |
| K11 | 'water well' spelling | `{"brand": "water well", "model": "Silk"}` | found | Waterwell Silk | ✅ |

## brand gap

| ID | Customer says | Bot sends | Result | Tanks returned | Pass |
|---|---|---|---|---|---|
| L01 | Vectus Pro | `{"model": "Pro"}` | not_in_brand | — | ✅ |
| L02 | Vectus Health | `{"model": "Health"}` | not_in_brand | — | ✅ |
| L03 | Vectus Italian | `{"model": "Italian"}` | not_in_brand | — | ✅ |
| L04 | Ganga Safe | `{"model": "Safe", "brand": "Ganga"}` | not_in_brand | — | ✅ |
| L05 | Waterwell Cool | `{"model": "Waterwell Cool"}` | not_in_brand | — | ✅ |
| L06 | Ganga Mini (TN) | `{"model": "Mini", "state": "TN"}` | not_in_brand | — | ✅ |
| L07 | Ganga Smart | `{"model": "Smart", "brand": "Ganga"}` | not_in_brand | — | ✅ |

## heard

| ID | Customer says | Bot sends | Result | Tanks returned | Pass |
|---|---|---|---|---|---|
| M01 | vector school | `{"model": "vector school"}` | found | Vectus Cool | ✅ |
| M02 | vectors school | `{"model": "vectors school"}` | found | Vectus Cool | ✅ |
| M03 | कूल (Hindi script) | `{"model": "कूल"}` | found | Vectus Cool | ✅ |
| M04 | ten x | `{"model": "ten x"}` | found | Vectus TenX | ✅ |
| M05 | 10x | `{"model": "10x"}` | found | Vectus TenX | ✅ |
| M06 | t90 | `{"model": "t90"}` | found | Vectus T-90 | ✅ |
| M07 | granite | `{"model": "granite"}` | found | Vectus Granito | ✅ |
| M08 | granitto (typo) | `{"model": "granitto"}` | found | Vectus Granito | ✅ |
| M09 | puf | `{"model": "puf"}` | found | Vectus Puff | ✅ |
| M10 | पफ | `{"model": "पफ"}` | found | Vectus Puff | ✅ |
| M11 | Victus Cool | `{"model": "Victus Cool"}` | found | Vectus Cool | ✅ |
| M12 | sintex | `{"model": "sintex"}` | unknown_model | — | ✅ |
| M13 | supreme | `{"model": "supreme"}` | unknown_model | — | ✅ |

## phrase

| ID | Customer says | Bot sends | Result | Tanks returned | Pass |
|---|---|---|---|---|---|
| N01 | Cool 1100 litre | `{"model": "Cool 1100 litre"}` | found | Vectus Cool | ✅ |
| N02 | Granito 3000 | `{"model": "Granito 3000"}` | not_available (closest 2000 / None) | — | ✅ |
| N03 | green safe | `{"model": "green safe"}` | found | Vectus Safe | ✅ |
| N04 | blue puff | `{"model": "blue puff"}` | not_available | — | ✅ |
| N05 | 5 layer wala | `{"model": "5 layer wala"}` | found | Vectus Cool | ✅ |
| N06 | water tank | `{"model": "water tank"}` | catalogue | — | ✅ |
| N07 | T-90 1000 | `{"model": "T-90 1000"}` | found | Vectus T-90 | ✅ |

## compare

| ID | Customer says | Bot sends | Result | Tanks returned | Pass |
|---|---|---|---|---|---|
| O01 | Cool aur Puff mein fark | `{"model": "Cool aur Puff"}` | found | Vectus Cool, Vectus Puff | ✅ |
| O02 | Smart vs Safe | `{"model": "Smart vs Safe"}` | found | Vectus Smart, Vectus Safe | ✅ |
| O03 | Smart, Safe and Cool | `{"model": "Smart, Safe and Cool"}` | found | Vectus Smart, Vectus Safe, Vectus Cool | ✅ |
| O04 | TN: Cool aur Safe | `{"model": "Cool aur Safe", "state": "TN"}` | found | Ganga Cool, Ganga Health | ✅ |
| O05 | Granito ya TenX | `{"model": "Granito ya TenX"}` | found | Vectus Granito, Vectus TenX | ✅ |

## junk

| ID | Customer says | Bot sends | Result | Tanks returned | Pass |
|---|---|---|---|---|---|
| P01 | (nothing given) | `{}` | catalogue | — | ✅ |
| P02 | LLM nulls | `{"model": "null", "capacity": "", "colour": "None"}` | catalogue | — | ✅ |
| P03 | only a state | `{"state": "UP"}` | catalogue | — | ✅ |
| P04 | unknown fields | `{"product": "Water tank", "foo": 1, "model": "Cool"}` | found | Vectus Cool | ✅ |
| P05 | very long text | `{"model": "cool cool cool cool cool cool cool cool cool cool cool cool` | found | Vectus Cool | ✅ |
| P06 | relaxation: 10 layer + green | `{"layers": 10, "colour": "green"}` | no_match | — | ✅ |

## Multi-turn flows, HTTP and load

| ID | Check | Pass |
|---|---|---|
| FL1 | 1100 → options → 'Cool wala' → confirmed | ✅ |
| FL2 | Smart 1100 → not made → takes 1500 → confirmed | ✅ |
| FL3 | green → Smart, Safe → '1100 chahiye' → Safe only | ✅ |
| FL4 | TN cooling → Ganga options → Ganga Cool 750 confirmed | ✅ |
| FL5 | Kerala asks 'Safe' → told Ganga Health is the same tank | ✅ |
| FL6 | Cool vs Puff → picks Puff 700 → confirmed | ✅ |
| H1 | no API key → 401 | ✅ |
| H2 | wrong API key → 401 | ✅ |
| H3 | trailing slash works (no redirect) | ✅ |
| H4 | broken JSON → 422, no crash | ✅ |
| H5 | /health reports 39 tanks | ✅ |
| H6 | GET /tanks not allowed | ✅ |
| PF1 | 10 concurrent callers × 40 requests: 0 errors, p95 < 200 ms (p50 10.4 ms, p95 24.3 ms) | ✅ |

## Sample replies the bot reads

**A01 — 1100 litre chahiye**

> Tanks (1100 litre): Vectus Safe: 4 layer, White / Green / Blue, 550 to 5000 litres (550, 750, 1100, 1500, 2000, 3000, 5000), 15 year guarantee, Roto moulded; Vectus Cool: 5 layer, White, 550 to 5000 litres (550, 750, 1100, 1500, 2000, 3000, 5000), 15 year guarantee, Roto moulded; Vectus Granito: 6 layer, Ruby / Emerald / Pearl, 550 to 2000 litres (550, 750, 1100, 1500, 2000), 15 year guarantee, Roto moulded. Say these names briefly and ask which one the customer prefers.

**B01 — 1200 litre**

> No Vectus tank comes in 1200 litre. Closest options. At 1100 litre: Vectus Safe, Vectus Cool, Vectus Granito. At 1500 litre: Vectus Smart, Vectus Puff, Vectus Safe, Vectus Silk. Tell the customer briefly and ask what they prefer.

**G02 — Smart 1100?**

> Vectus Smart does not come in 1100 litre; it comes in 500 to 5000 litres (500, 700, 1000, 1500, 2000, 3000, 5000). Closest: 1000 or 1500 litre. For reference, Vectus Smart: 3 layer, White / Green / Blue, 500 to 5000 litres (500, 700, 1000, 1500, 2000, 3000, 5000), 10 year guarantee, Roto moulded. Tanks with 1100 litre: Vectus Safe: 4 layer, White / Green / Blue, 550 to 5000 litres (550, 750, 1100, 1500, 2000, 3000, 5000), 15 year guarantee, Roto moulded; Vectus Cool: 5 layer, White, 550 to 5000 litres (550, 750, 1100, 1500, 2000, 3000, 5000), 15 year guarantee, Roto moulded; Vectus Granito: 6 layer, Ruby / Emerald / Pearl, 550 to 2000 litres (550, 750, 1100, 1500, 2000), 15 year guarantee, Roto moulded. Tell the customer briefly and ask what they prefer.

**L01 — Vectus Pro**

> Vectus does not sell Pro; the same tank in Vectus is Vectus Smart. Vectus Smart: 3 layer, White / Green / Blue, 500 to 5000 litres (500, 700, 1000, 1500, 2000, 3000, 5000), 10 year guarantee, Roto moulded. Layers: Enhanced Aesthetic Layer, UV-Protective Sun-Shield Layer, Advanced Anti-Bacterial Layer. Features: Anti-Bacterial Layer: Guarantees water purity and safety with an inner anti-bacterial layer, effectively preventing bacterial contamination; UV-Protective Sun-Shield Layer: Provides reliable protection against harmful UV rays, safeguarding water quality; Enhanced Aesthetic Layer: Crafted with high-quality raw material, with various colour options which add an aesthetic appeal to the tank; Lightweight and Durable Construction: Engineered with a lightweight yet durable design; Injection-Moulded Threaded Lid: Features a robust injection-moulded threaded lid for secure sealing.

**O01 — Cool aur Puff mein fark**

> Compare: Vectus Cool: 5 layer, White, 550 to 5000 litres (550, 750, 1100, 1500, 2000, 3000, 5000), 15 year guarantee, Roto moulded, layers: Titanium-Loaded Layer, Temperature-Resistant Foam Layer, UV-Protective Sun-Shield Layer, Temperature-Resistant Foam Layer, Advanced Anti-Microbial layer (Microban); Vectus Puff: 4 layer, White, 500 to 5000 litres (500, 700, 1000, 1500, 2000, 3000, 5000), 10 year guarantee, Roto moulded, layers: Titanium-Loaded Layer, UV-Protective Sun-Shield Layer, Temperature-Resistant Foam Layer, Advanced Anti-Bacterial Layer. Answer the customer's comparison briefly from these.

**E04 — 1100 yellow**

> No Vectus tank comes in 1100 litre with Lime Yellow. Closest options. At 1000 litre: Vectus T-90, Vectus TenX. At 1500 litre: Vectus T-90, Vectus TenX. Tell the customer briefly and ask what they prefer.

