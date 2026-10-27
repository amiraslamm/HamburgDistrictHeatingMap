# DEP-71: Sprinkenhof — Analyze new district heating availability map

- Linear: https://linear.app/predium/issue/DEP-71/sprinkenhof-analyze-new-district-heating-availability-map
- Project: Sprinkenhof RPv3 | Team: Deployment | Priority: Medium | Estimate: 4 points
- Created by: Sebastian Palt | Assignee: Amir Aslam
- Status: Todo

## Background (from ticket description)

Hamburg published the draft district heating plan ("Entwurf des Wärmeplans"). Forwarded by Kristina Duus via an email from Annika Lieckfeldt (Referentin Beteiligungsmanagement, ZB 17).

- Map link: https://www.hamburg.de/politik-und-verwaltung/behoerden/bukea/themen/energie/kommunale-waermeplanung/waermeportal-hamburg
- The map ("Karten des Zielszenarios") lets you look up, by address, the technically/economically best-suited heat supply option for that location.
- A 30-day public comment period ("Beteiligungsphase") runs until **2026-04-14**, via https://hh.beteiligung.diplanung.de/
- Contact for questions: Zehra Hekim-Schroth (Zehra.Hekim-Schroth@bwai.hamburg.de)

## Task — comment from Sebastian Palt (2026-07-10)

> @muhammad.arhab Please analyze the map and show the impact on each building in an Excel.

**Impact categories to capture per building:**
1. Fernwärme (district heating) no longer available where it was before.
2. Fernwärme available at a different point in time than previously — the new map is more detailed/granular on timing (years) than before.

**Definition to apply:**
- "Decentral" in the map = no Fernwärme, but could still be a local network or heat pump.
  - Mark these buildings as **decentral** in the Excel, and propose **LWP** (local heat pump / Lokale Wärmepumpe) — but note Kristian (Duus) should make the final decision on this.

**Next step / handoff:**
- Share the resulting Excel with **Kristian Duus** (via Amir or Ibrahim).
- Kristian decides whether/how this should be implemented.

## Open questions / things to verify while working
- Confirm building list/scope to check against the map (likely Sprinkenhof portfolio export).
- Confirm current Fernwärme status per building (baseline) to diff against the new map.

## Pipeline built (2026-09-01)

Modeled on the colleague's Berlin BWV Fernwärme pipeline (`deployment_tools/bwv-analysis/fernwaerme/`: fetch WFS polygons → point-in-polygon spatial join against building lat/long → guardrail vs. current heating source).

- **Input**: `import/SprinkenhofLangLong.csv` — 811 buildings with `building_id`, `address`, `longitude`, `latitude`, `heating_type` (current: 351 District heating, 381 Natural gas, 28 Electricity, 23 Fuel oil, 16 Heat pump, 7 Wooden pellets, 3 LPG, 2 Wood). **This is the authoritative complete building list** — see [[project_dep71_waermeplanung]] memory; do not use the unrelated 26-building P&W export.

### v1 (superseded) — Eignungsprüfung proxy
First pass used `https://geodienste.hamburg.de/wfs_kommunale_waermeplanung` (the "Eignungsprüfung" §14 WPG pre-check layer), found via MetaVer, because no WFS/WMS for the real "Eignung Wärmeversorgungsarten"/Zielszenario layer turned up in MetaVer/Transparenzportal/GovData searches. Coarse 3-bucket result (existing/decentral/undetermined), no year/timing data. **Superseded by v2 below** — kept only as historical record; do not use for the actual deliverable.

### v2 (current) — real Zielszenario data
The user manually expanded the portal's "Weitere Fachinformationen" → Fachdaten → **Zielszenario** folder (not discoverable via catalog search) and opened the (i) info panel on each layer, which has "Legende" / "Datensatz herunterladen" / **"WMS-Adresse"** tabs — this is where the real service URLs actually live, not MetaVer.

- **Data source**: `https://geodienste.hamburg.de/wfs_eignung_waermenetzversorgung_kwp` (typeName `de.hh.up:eignung_waermenetzversorgung`) — 26,150 polygons, EPSG:25832. This IS the real data behind the ticket's "Eignung Wärmeversorgungsarten" screenshot layer. Sibling layer `wfs_eignung_dezentr_waermeversorgung_kwp` (note abbreviated "dezentr", not "dezentrale" — had to get this exact name from the portal UI, guessing the URL failed) exposes the *same* underlying attributes, just symbolised by the other score — one fetch covers both.
- Each feature (per Gebietseinheit) carries **both** scores directly:
  - `eignungsstufe_waermenetz` (0–3) / `eignung_waermenetz` — network-heating suitability
  - `eignungsstufe_dezentral` (0–3) / `eignung_dezentral` — decentral-supply suitability
  - 0 = sehr wahrscheinlich ungeeignet, 1 = wahrscheinlich ungeeignet, 2 = wahrscheinlich geeignet, 3 = sehr wahrscheinlich geeignet
- **Still no year/timing attribute** — this remains the one sub-case of Sebastian's ask ("Fernwärme available at a different point in time") that no dataset found so far can address.
- **Classification**: network suitability ≥2 → "Network suitable" path; ≤1 → "Decentral suitable" path (LWP proposed, decentral score noted for confidence). Compared against current `heating_type` for possible loss/gain, same as v1.
- **Script**: `script/analyze_waermeplanung.py` — same fetch/paginate/reproject/snap-to-nearest-250m pattern as v1, now against the real layer. `script/plot_waermeplanung_map.py` renders the map, background shaded by the polygons' own `farbcode` (portal's real 4-level red gradient).
- **Output**: `export/DEP-71_Sprinkenhof_Waermeplanung_Eignung_Waermeversorgungsarten.xlsx` + `export/DEP-71_Sprinkenhof_Waermeplanung_map.jpg`.
- **Result counts** (2026-09-01 run): 608 network-suitable, 183 decentral-suitable, 20 outside Hamburg's Wärmeportal coverage (Neuwerk island, Geesthacht, + a handful of Hamburg addresses >250m from any polygon). Impact: 298 no change (already DH), 310 combined possible gain (227 "sehr wahrscheinlich geeignet" + 83 "wahrscheinlich geeignet"), 137 decentral per map (→ LWP proposed), **46 combined possible loss** (31 "sehr wahrscheinlich ungeeignet" + 15 "wahrscheinlich ungeeignet" network suitability while currently on DH) — these are the buildings most worth double-checking manually before sharing with Kristian Duus.
- **Superseded 2026-09-02 by v3 below** — Sebastian confirmed this was the wrong layer.

### v3 (current, correct) — real Zielszenario ("empfehlung") data
Sebastian sent the exact portal deep-link he meant: `https://geoportal-hamburg.de/waermeportal-hamburg/?...LAYERS=[...{"id":"35894","visibility":true}...]`. Layer IDs in a geoportal-hamburg.de deep-link resolve via `https://geoportal-hamburg.de/lgv-config/services-internet.json` (a ~6555-entry service list) — id 35894 = **"Zielszenario – kommunale Wärmeplanung Hamburg"**, WMS `wms_zielszenario_kwp` (layer `zielszenario`). The v2 layer (`eignung_waermenetzversorgung`) was a *different, earlier-stage suitability score* — one of several inputs into Zielszenario, not the final recommendation itself. Its own `kommentar` text even says so: "Wärmenetzlösungen sind in der Regel unwirtschaftlich, siehe Karte „Eignung Wärmeversorgungsarten"." (treats that layer as a mere cross-reference).

- **Data source**: `https://geodienste.hamburg.de/wfs_zielszenario_kwp` (typeName `de.hh.up:zielszenario`) — 26,160 polygons, same Gebietseinheit grid as v2 (confirmed same BG##### codes), so the spatial-join/snap-250m logic carried over unchanged.
- **This is where the timing data actually lives** — the `empfehlung` field (Hamburg's own single final recommendation per area, not a suitability score) has 7 values, including explicit rollout years:
  - Dezentrale Wärmeversorgung (17,348 citywide)
  - Vorhandenes Wärmenetz und Wärmenetzverdichtung (5,908)
  - Wärmenetzausbau ab 2026 / 2030 / 2035 (804 / 465 / 944)
  - Wärmenetzausbau, Eigeninitiative notwendig (503)
  - Für dezentral und Wärmenetz gleichwertig geeignet (178)
  - This **resolves the previously-unaddressed sub-case** of Sebastian's original comment ("Fernwärme available at a different point in time... more detailed in terms of years").
- Also carries `betreiber` (current network operator) and `betreiber_vorschlag` (proposed operator for planned expansion) — bonus info not available in v2, included as extra output columns.
- **Classification**: `category` = the translated `empfehlung` value directly (no thresholding needed — Hamburg's own recommendation is already the right granularity). `impact` compares current `heating_type` against category; for non-DH buildings the impact text carries the specific timing (e.g. "expansion planned from 2030"). `proposed_action`: LWP proposal only for pure "Decentral heat supply" (matches Sebastian's original wording exactly); for "Eigeninitiative" areas, proposes requesting a connection and names the proposed operator.
- **Script**: `script/analyze_waermeplanung.py` (rewritten), `script/plot_waermeplanung_map.py` (rewritten, background now shaded by Zielszenario's own farbcode: dark red = existing network, lighter oranges = expansion tiers by year, blue = decentral, purple = equally suited), `streamlit_app.py` (rewritten to match — WMS toggle now points at `wms_zielszenario_kwp`).
- **Output**: `export/DEP-71_Sprinkenhof_Waermeplanung_Zielszenario.xlsx` + same-named map JPG.
- **Result counts** (2026-09-02 run): 275 no change, 243 decentral (→ LWP proposed), 189 possible gain (84 existing-network / 36 owner-initiative / 29 exp.-from-2026 / 31 exp.-from-2035 / 9 exp.-from-2030), **68 possible loss** (up from v2's 46 — the corrected data is notably stricter about what counts as network-viable), 19 outside coverage, 13 equally suited, 4 no-recommendation-data. **These numbers are the correct ones to use going forward** — v1 and v2 counts in this file are historical only.
- **Next step**: share v3 output with Kristian Duus (per Sebastian's instruction, via Amir or Ibrahim). The 68 "possible loss" buildings are the highest-priority manual spot-check, followed by the 19 "outside coverage" + 4 "no map match".

### v3.1 (2026-09-04) — added `expected_availability` column
Direct, clean answer to "when will this building get district heating" — pulls the year out of the `category` text into its own field: "Already available" (existing network, or building already on DH today regardless of area category), "2026"/"2030"/"2035" (expansion-tier categories), "No fixed date — requires owner request" (Eigeninitiative), "Not planned (decentral recommended)", "Uncertain — equally suited either way", "Not applicable" (outside coverage / no-match). **Gotcha #1 (fixed)**: initially used the literal string "N/A" for the not-applicable case — `pandas.read_excel` silently converts "N/A" back to `NaN` on read (default `na_values` behavior), so the Streamlit app showed blank cells despite `classify()` writing the correct value. Fixed by using "Not applicable" instead.

**Gotcha #2 (fixed, caught by the user)**: "Existing network / densification" buildings that are NOT currently on district heating (e.g. still on Natural gas) were incorrectly labeled "Already available" — that category means the network *infrastructure* exists in the area, not that this specific building is connected. Fixed: `CATEGORY_TO_AVAILABILITY[EXISTING_NETWORK_CATEGORY]` now reads "Available now — not yet connected" (only reached when `is_dh` is False; the `is_dh=True` branch earlier in `_expected_availability` still correctly returns "Already available"). Verified zero mismatches: no row has "Already available" with a non-district-heating `heating_type`. Final distribution: 343 already available, 243 not planned, 84 available-not-connected, 36 owner-request, 31 in 2035, 29 in 2026, 23 not applicable, 13 uncertain, 9 in 2030.
