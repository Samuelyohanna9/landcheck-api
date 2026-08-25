# Ago Palace Way, Okota (Oshodi-Isolo LGA) — Final status: UNRESOLVED, spatial correspondence test FAILED

**Not used for V3.1 evaluation. V3.1 was never tested here — the candidate label failed
qualification before reaching that stage, which is a different conclusion from "V3.1 failed."**

## Event/mechanism (confirmed, strong)
23 September 2025, Ago Palace Way / Apple Junction / Ago Bridge, Okota. Lagos State Government's
own Commissioner for Environment and Water Resources confirmed flash flooding from heavy rainfall,
drainage channels overflowing, and illegal structures obstructing drainage. CHIRPS confirms real
rainfall (17.2mm 22 Sep, 13.3mm 23 Sep, 20.0mm 24 Sep). This remains a well-documented, defensible
pluvial/urban-drainage event by every criterion except spatial corroboration.

## Spatial correspondence test (failed)
Same-orbit S1 pair (orbit 1: pre 13 Sep -9.2d, post 25 Sep +2.8d) generated an initially
visually-suggestive linear pattern. Per instruction, the exact OSM geometry for Ago Palace Way and
several comparison roads (Okota Road, Nwachuku Drive, Community Road, Balogun Street, Canal Avenue)
was extracted and buffered into 60m corridors, then checked against the same flood mask:

- Ago Palace Way corridor: **0.000%** flood-classified pixels
- Community Road (unrelated, not reported flooded): **1.19%**

The visually-suggestive linear feature does not correspond to Ago Palace Way or any named
comparison road checked. Per explicit instruction, the search was NOT widened to identify what
that feature actually is - doing so after the fact would be post-hoc label hunting, exactly the
failure mode this test was designed to prevent.

## Interpretation
The procedure worked as intended: it rejected an initially convincing visual read once checked
against real geometry, rather than confirming it. This is a genuine positive result for the
validation workflow itself, independent of this specific candidate's outcome. Both the
approximate-AOI and precisely-refined-AOI results are preserved (`AgoPalaceWay_20250923_
APPROXIMATE_AOI_review.png`, `..._REFINED_AOI_review.png`, `..._WITH_ROAD_OVERLAY.png`) as a
permanent record of the test, not overwritten.

## Status
CLOSED. Not to be reopened by searching for an alternative street/feature that happens to match
the observed SAR pattern.
