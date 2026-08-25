# V3 Thread 1 — Historical Sentinel-1 Reconstruction: Closure & Consolidated Evidence State

Closes the historical-SAR-reconstruction line of Nigerian pluvial ground-truth work pursued this
session (Ondo Town → Minna → Lagos mainland → the 19-city/5-cluster satellite-event coincidence
scan → Owerri Metro → Nekede → Amauzari). Preserves the three distinct evidence states the project
now stands on, per explicit instruction, so future work starts from an accurate picture rather than
re-litigating settled questions.

## 1. V3.1 Terrain Susceptibility — REPLICATED, FROZEN

`V3.1 = 0.5 * normalize(-HAND) + 0.5 * normalize(-RelativeElevation_1000m)`

Full definition, frozen constants, and both independent validations (UFO: 11/13 events, median AUC
0.734; Sen1Floods11: 10/11 events, median AUC 0.729) preserved in
`scratch/V3_1_TERRAIN_INDEX_PERMANENT_RECORD.md`. Status unchanged by anything in this document -
no Nigerian event was used to construct or adjust it, and none of the work below is grounds to
revisit it. Real, transferable, general terrain-inundation-susceptibility signal - explicitly not
yet shown to be pluvial-specific or Nigeria-specific.

## 2. Nigeria Pluvial Validation — UNRESOLVED (not failed)

No genuinely qualified Nigerian pluvial spatial ground truth was obtained this session. This is a
data-availability gap, not a negative result about V3.1 or about Nigeria - every candidate that
reached the spatial-evidence stage failed for a specific, understood, external reason (see below),
never because the terrain hypothesis was tested and found wanting.

Candidates carried furthest, all preserved in detail in their own records:
- **Ondo Town** (4 Oct 2024) - `scratch/V3_NIGERIA_GROUNDTRUTH_QUALIFICATION.md`: strong
  event/mechanism evidence (NEMA DG on-site assessment, named streets), but no usable spatial
  evidence exists - S1 arrived 3.7 days late (null), S2 was cloud-blocked, no map/GIS product found.
- **Minna**: UNRESOLVED - DTM/IOM completely inaccessible, and the one detailed local story found
  appears to be a 2026 event, not 2024. Never reached the spatial-evidence stage.
- **Lagos mainland** (28 Jun 2024) - same file: strong event/mechanism evidence (officials'
  attribution, MMIA terminal flooded), S1 arrived 5.2 days late (weak/inconclusive), S2 cloud-blocked,
  December 2024 DTM assessment (81 locations/14 LGAs) remains blocked at the source.
- **Owerri Metro** (17-18 Aug 2021) - `scratch/v3_owerri_masks/OWERRI_METRO_RESULT.md`: the best
  documented event (named streets across 4 locations) with the best satellite timing found all
  session (+1.2d and +1.7d, two independent orbits) - SUGGESTIVE/UNCONFIRMED, not usable, because
  19.7mm of same-day rain between the two passes confounds attribution (see Finding B below).
- **Nekede** (9 Aug 2021) - `scratch/v3_nekede_masks/NEKEDE_RESULT.md`: strong, precisely-located
  mechanism evidence (CHIRPS confirms 66.2mm+19.4mm trigger), clean (non-confounded) same-orbit
  pair at +5.2 days - clean NULL result, consistent with water having already receded (Finding A).
- **Amauzari** (27-28 Aug 2021): closed without generating a mask - LGA-level location only, erosion
  mechanism contamination risk, weaker timing (+4.7d best case) than Nekede's already-null case made
  the expected value of the expensive step too low to justify.
- **5-cluster satellite-coincidence scan** (2018-07-25, 2021-08-31, 2021-08-07, 2025-09-20,
  2015-07-10; 22 city-date candidates across 19 cities) -
  `scratch/V3_NIGERIA_SATELLITE_EVENT_COINCIDENCE_INVESTIGATION.md`: 0/22 advanced - every
  candidate had confirmed satellite timing but no accessible source tied documented flooding to the
  specific date.

## 3. Historical Sentinel-1 Reconstruction — INSUFFICIENT as the primary Nigerian ground-truth
   strategy (methodological finding, not a data gap)

Two independent, complementary failure modes were found and are now documented as permanent
methodological lessons for any future Nigerian (or generally short-duration-pluvial) SAR
ground-truth work:

**Finding A - the "too late" failure mode** (Ondo 3.7d, Lagos 5.2d, Nekede 5.2d - all null or weak):
short-duration urban pluvial flooding can recede within hours to ~1-2 days. Once the nearest
same-orbit Sentinel-1 pass is more than ~2 days after the event, the water is very often already
gone - a null SAR result under these conditions is uninformative, not evidence of non-flooding.

**Finding B - the "confounded-even-when-close" failure mode** (Owerri Metro, +1.2d/+1.7d): even
when satellite timing is excellent, a second rainfall event between the pre- and post-event
acquisitions (or even between two same-day passes at different times) can produce a strong SAR
change signal that cannot be safely attributed to the originally-reported event. CHIRPS (or an
equivalent rainfall record) must be checked across the ENTIRE pre-to-post window, not just around
the target event date, before any mask is trusted - a check that was not systematically applied
before Ondo/Lagos/Stage-B/C and should be applied on any future SAR-based work.

**Combined implication**: for phenomena with characteristic durations of hours to ~1 day, there is
often no safe Sentinel-1 window at all - too-late misses the water, close-enough risks a confounding
rain event. This is not a fixable search-effort problem; it is a structural mismatch between the
observation cadence Sentinel-1 offers and the phenomenon being validated. Reverse-searching from
satellite availability (the 5-cluster scan) solved the timing half of this problem computationally
but could not solve the confounding half, and still could not find event documentation for any of
its 22 survivors - a second, independent confirmation that the bottleneck is Nigerian event
documentation infrastructure, not satellite scheduling.

## Status / what does NOT change

No production code (`hazard_pluvial.py`, router, PDF, frontend) is touched by anything in this
document. The existing product architecture already reflects the right posture for this evidence
state - `_flood_recommendation()` in `app/routers/hazards.py` already excludes pluvial from
decision-making, and the Rainfall/Surface-Water branch is already labelled experimental in
customer-facing output (from the earlier Flood Product Decision). River inundation and Floodplain
Susceptibility remain shippable with their existing bounded wording; nothing here changes that.

## Closed / not pursued further absent new instruction

Historical Sentinel-1 mask reconstruction as the primary Nigerian pluvial ground-truth strategy is
closed for now, per instruction - not to be resumed by continuing to search for a luckier date/city
combination. The next research direction (non-satellite-dependent Nigerian ground truth: agencies,
researchers, crowdsourced data, road/traffic reports, geotagged social media, field collection) has
been named but not started - awaiting direction on whether/how to begin it.
