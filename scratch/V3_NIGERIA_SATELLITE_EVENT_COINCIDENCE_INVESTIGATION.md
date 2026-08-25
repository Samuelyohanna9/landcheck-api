# Nigeria Satellite-Event Coincidence Scan — 5-Cluster Investigation Result

Reverse-search methodology (per explicit instruction): screen Sentinel-1 acquisitions across 19
Nigerian cities (2015-present) for coincidence with locally-extreme (>p95) CHIRPS rainfall, THEN
search for authoritative/news evidence only among the survivors — rather than finding documented
floods first and asking whether satellite happened to observe them (the approach that stalled on
Ondo/Minna/Lagos). Computational scan: `scratch/v3_nigeria_satellite_event_coincidence_scan.py`,
full results in `scratch/v3_nigeria_coincidence_scan_results.json`.

Cross-city clustering (dates where 2+ independent cities hit the coincidence simultaneously,
implying a single regional rain system) identified 5 top candidate storm systems, investigated in
this order per instruction: **2018-07-25 → 2021-08-31 → 2021-08-07 → 2025-09-20 → 2015-07-10**.

**PRE-REGISTERED GATE per city** (fixed before investigation, per instruction): a city advances only
if ALL of the following hold — documented flooding on or tightly around the date; rainfall/drainage
as predominant mechanism; geographically specific affected locations; S1 acquisition close enough to
plausibly observe water (already confirmed computationally for every city below, or the city would
not have made this shortlist).

## Outcome table

| Cluster (storm_cluster_id) | City | Flood confirmed on/near date? | Mechanism | Exact locations? | Evidence tier | S1 timing | Advance? |
|---|---|---|---|---|---|---|---|
| NGA_2018_07_25 | Calabar | No | - | - | None found | ✓ confirmed | **No** |
| NGA_2018_07_25 | Uyo | No (Uyo *did* flood 21 Jun 2018 - different date) | - | - | None for this date | ✓ confirmed | **No** |
| NGA_2018_07_25 | PortHarcourt | No | - | - | None found | ✓ confirmed | **No** |
| NGA_2018_07_25 | Aba | No | - | (Ariaria Market is a known chronic low-lying flood point - not date-specific) | None for this date | ✓ confirmed | **No** |
| NGA_2018_07_25 | Owerri | No (Owerri *did* flood 8 Jun 2018 - different date) | - | - | None for this date | ✓ confirmed | **No** |
| NGA_2021_08_31 | Aba | No | - | - | None found | ✓ confirmed | **No** |
| NGA_2021_08_31 | BeninCity | No | - | (Uselu-Ugbowo Rd, Adolor Junction, Ikpoba-Okha are known chronic points - not date-specific) | None for this date | ✓ confirmed | **No** |
| NGA_2021_08_31 | Enugu | No | - | - | None found | ✓ confirmed | **No** |
| NGA_2021_08_31 | Owerri | No | - | (Egbu, Uratta Rd, Amakohia, MCC Rd are known chronic points - not date-specific) | None for this date | ✓ confirmed | **No** |
| NGA_2021_08_31 | Warri | No | - | - | None found | ✓ confirmed | **No** |
| NGA_2021_08_07 | Abuja | No (Abuja's documented 2021 flood was 12-13 Sep - 5 weeks later) | - | - | Confirmed WRONG date | ✓ confirmed | **No** |
| NGA_2021_08_07 | BeninCity | No | - | - | None for this date | ✓ confirmed | **No** |
| NGA_2021_08_07 | Owerri | No | - | - | None for this date | ✓ confirmed | **No** |
| NGA_2021_08_07 | Warri | No | Academic lit flags Lagdo Dam/river influence alongside rainfall for Warri generally - added mechanism-contamination risk regardless of date | - | None for this date | ✓ confirmed | **No** |
| NGA_2025_09_20 | Akure | No | - | - | None found | ✓ confirmed | **No** |
| NGA_2025_09_20 | Ilorin | No | - | (extensive chronic flood-zone list found: Okelele, Amilengbe, Isale Koko, Asa Dam, Baboko, etc. - not date-specific) | None for this date | ✓ confirmed | **No** |
| NGA_2025_09_20 | OndoTown | No | - | - | None found | ✓ confirmed | **No** |
| NGA_2025_09_20 | Osogbo | No | - | - | None found | ✓ confirmed | **No** |
| NGA_2015_07_10 | Aba | No | - | - | None found | ✓ confirmed | **No** |
| NGA_2015_07_10 | Enugu | No | - | - | None found | ✓ confirmed | **No** |
| NGA_2015_07_10 | Owerri | No | - | - | None found | ✓ confirmed | **No** |
| NGA_2015_07_10 | PortHarcourt | No | - | - | None found | ✓ confirmed | **No** |

**Result: 0 of 22 city-date candidates advance.** Every candidate already had confirmed satellite
timing (same-orbit pre-event scene available, computed in the scan itself) - the gate that failed in
every single case was event-specific documentation, not satellite revisit.

## Real, honest attempts made (not a cursory pass)

Approximately 25 WebSearch/WebFetch calls across the 5 clusters: regional multi-state searches,
per-city per-date searches, exact-date-string searches ("25 July 2018"), ReliefWeb disaster-page
lookups (blocked, as throughout this session), an academic review paper (blocked), NiMet/FloodList
lookups, Wikipedia's dedicated 2018/2021/2025 Nigeria-floods articles (accessible, checked directly),
and known local-outlet searches (Vanguard, Guardian, ThisDay, Leadership, Premium Times - mostly
blocked at fetch, search-index summaries used where fetch failed). One genuine tool-conflation
caught and corrected: an early search result attributed a "163 deaths / North Nigeria" disaster to
20 September 2025 in the Akure/Ilorin/Osogbo region; cross-checked against Wikipedia's dedicated
article, which confirmed that disaster (Mokwa) actually occurred 28 May 2025 in Niger State - an
unrelated event, not used.

## Interpretation

This directly answers the diagnostic question posed before starting: **the reverse-search strategy
successfully solved the satellite-timing problem (every one of these 22 candidates has a real,
already-confirmed same-orbit acquisition close to a real rainfall extreme) but still could not
produce a single dated, geographically-specific, accessible confirmation of actual urban flooding.**
The bottleneck is not Sentinel-1 revisit cadence (solved) and not rainfall extremity (solved) - it is
open, accessible, dated, geolocated Nigerian flood-event documentation itself. This is consistent
with, and reinforces, the DTM/ReliefWeb inaccessibility already hit repeatedly on Ondo/Minna/Lagos.

General (non-date-specific) chronic-flood-zone evidence WAS found for several cities (Ariaria
Market/Aba, Uselu-Ugbowo/Adolor Junction/Benin City, Egbu-Uratta-Amakohia/Owerri, the extensive
Ilorin list) - useful context for understanding WHERE these cities flood repeatedly, but explicitly
not usable as event-specific spatial ground truth per the standing evidence-quality rule.

## Status

No masks generated (per instruction - this stage only screens candidates, doesn't spend the
expensive spatial-ground-truth effort). No V3.1 calculation performed. All storm_cluster_id values
preserved above for future reference if any of these dates is ever independently confirmed through a
source not accessible this session (e.g. if DTM access is obtained separately).
