# Nigeria Pluvial Ground-Truth Search — Closure of Broad Historical/Open-Data Search

## 5-state UNOSAT product (29 Jul 2025, Adamawa/Borno/Gombe/Taraba/Yobe) — REJECT for pluvial

Decomposed the single dissolved multipolygon into 20,925 individual disjoint patches (total 706 km²,
matching the product's own stated figure). Reverse-geocoded the 7 largest patches (66.55 km² down to
9.19 km², collectively the large majority of total area) via OSM Nominatim:

| Patch area | Location | LGA |
|---|---|---|
| 66.55 km² | Fufore | Adamawa |
| 23.77 km² | Danada, Lau | Taraba |
| 22.95 km² | Tignon, Larmurde | Adamawa |
| 19.61 km² | Jimeta, Girei | Adamawa (Yola metro) |
| 13.54 km² | Numan | Adamawa |
| 13.38 km² | (Larmurde area) | Adamawa |
| 10.33 km² | Kursigi, Funakaye | Gombe |

Every one of these is a Benue River floodplain LGA. Independently confirmed via news search: this is
the 27 July 2025 Yola/Adamawa flood (25+ dead, 5,560 displaced) - **mechanism explicitly "torrential
rain and suspected water release from the Bole dam,"** with an UBRBDA team investigating a nearby
dam-construction project's possible role. Affected LGAs per the news report (Fufore, Yola South,
Yola North, Girei, Demsa, Numan, Lamurde) match the reverse-geocoded patches exactly.

**Verdict: MIXED (rainfall + dam-related), fluvial-dominant. REJECT for pluvial validation.**
Preserved for the floodplain/fluvial branch catalogue per standing instruction - genuine
high-resolution Sentinel-2 observed extent exists (706 km², 20,925 patches, full geometry
preserved in `scratchpad/GDACS_Nigeria/unosat/jul2025_5states/`).

## Net result of the full open-data search (Ondo, Minna, Lagos mainland, 5-cluster satellite scan,
   Owerri Metro, Nekede, Amauzari, UNOSAT/GDACS systematic enumeration, FAO EVE event-discovery,
   Oshodi-Isolo Oct 2024, Ago Palace Way Sept 2025, 5-state UNOSAT decomposition)

**Zero events reach ACCEPT.** Every candidate that reached the spatial-evidence stage failed for a
specific, understood, external reason:
- Ondo, Lagos mainland, Nekede: satellite arrived too late (Finding A)
- Owerri Metro: satellite timing excellent, but same-day rainfall confounds attribution (Finding B)
- Ago Palace Way: satellite timing reasonable, but geometry-verified spatial correspondence test
  failed - the visually-suggestive SAR pattern does not correspond to the reported road
- Dikwa, Mokwa, Gashua/Bursari, 5-state UNOSAT product: mechanism independently confirmed as
  fluvial/dam-related, not pluvial - genuinely excellent spatial data, wrong mechanism
- Gujba: mechanism unresolved (rural rainfall, not clearly urban-pluvial)
- Minna: never reached the spatial-evidence stage (source inaccessibility + date uncertainty)

**This is not a failure of effort.** The search covered: historical Sentinel-1/2 reconstruction,
reverse satellite-date searching across 19 cities, UNOSAT's full product catalogue (via a newly
discovered, reusable HDX CKAN API route), GDACS/JRC's event infrastructure, FAO EVE's 156k-record
African flood-monitoring index, academic GPS/participatory-mapping studies (Hadejia/Lateef, Lagos/
Aniramu-Orimoogunje), NIHSA's public infrastructure, and multiple individually-investigated named
events. The consistent finding is structural: short-duration Nigerian urban pluvial flooding is
poorly served by any of the open, historical, remotely-sensed archives searched, either because the
observation cadence misses it (revisit-timing) or because the water that IS visible from orbit
tends to belong to larger, longer-duration, mechanistically distinct (fluvial/dam) events that are
easier to observe precisely because they last longer and cover more area.

## Status

Per instruction, broad historical/open-data searching for Nigerian pluvial ground truth is now
closed. V3.1 remains completely frozen and untouched throughout - no event was ever used to test or
adjust it; every candidate failed qualification before reaching that stage. The next appropriate
step is targeted data acquisition from agencies/researchers (NIHSA's formal Data Request process;
Lukumon Lateef for the fuller Hadejia participatory-mapping dataset; Aniramu/Orimoogunje for the
398-household Lagos GPS survey data) - not a fallback, but the correct next step once public
reconstruction has been demonstrated inadequate for this specific validation need.
