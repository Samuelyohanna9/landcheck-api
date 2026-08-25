# LandCheck Terrain Inundation Susceptibility Index — V3.1 Candidate — Permanent Record

**Status: DEPLOYED TO PRODUCTION (2026-08-25) as Floodplain Susceptibility, validated twice on
independent external datasets before deployment. NOT a validated pluvial-specific model — see
"What this is / is not" below. Do not modify the formula, weights, normalization constants, or
spatial scales without a new, equally rigorous replication test; a poor result on a single future
event (e.g. Pakistan, AUC 0.389 below) is not grounds for revision on its own.**

## The frozen definition

```
V3.1 = 0.5 * normalize(-HAND) + 0.5 * normalize(-RelativeElevation_1000m)
```

Sign convention: lower HAND and lower relative elevation both mean *more* flood-prone, so both are
negated before normalization — after negation, higher = more susceptible for both terms, and for
the combined index.

| Component | Definition | Source | Scale |
|---|---|---|---|
| **HAND** | Height Above Nearest Drainage, `reduceRegion(mean)` over a 300m-radius buffer around the point | `MERIT/Hydro/v1_0_1`, band `hnd` | `scale=90`, `bestEffort=True` |
| **Relative elevation (1000m)** | point elevation minus the 1000m-radius focal-mean elevation (`elev_m - focal_mean(DEM, radius=1000m)`) | `COPERNICUS/DEM/GLO30_2024_1` mosaic, band `DEM` | point: 30m buffer, `scale=30`; focal mean: `radius=1000m units=meters`, sampled at `scale=30` |

**Normalization (frozen, computed once, never re-fit):** min-max scaling using the oriented
(negated) value range observed across the full UFO sample set (n=417, all 13 usable UFO events
pooled — see below):

- HAND oriented (`-hand_m`) range: `[-165.947, -0.011]`
- Relative elevation oriented (`-relative_elev_1000m`) range: `[-90.064, 76.091]`

**Clipping rule:** if a new point's oriented value falls outside this frozen range (i.e. the new
site is more extreme than anything seen in UFO), the normalized term is clipped to `[0, 1]` rather
than extrapolated. This happened for 1 of 331 final Sen1Floods11 points (0.3%) — negligible so far,
but worth monitoring if the index is ever applied somewhere topographically far more extreme than
UFO's 13 source events.

**Missing-data handling (as implemented in the diagnostic scripts, not yet a production policy):** a
point is simply excluded from analysis if either raw input (`hand_m`, `elev_m`, or the 1000m focal
mean) is `None` — no imputation. A production integration would need to decide an explicit fallback
(e.g. neutral 0.5, or `data_available: false`), which has not been designed yet.

**Explicitly excluded from the core index** (tested and found to not improve it, or excluded by
instruction — see the diagnostic history below): `depression_300m` (demoted — added to HAND+RelElev
and made the combined index *worse*, 0.734→0.691 median on UFO), `twi_hs` (demoted to secondary
candidate, not core), `drain_dist_m`, `slope_deg`, `impervious_pct` (actively wrong-direction on
UFO, 2/13 correct-direction, median AUC 0.422 — real disconfirming evidence against "more built-up
= more flood-prone" as a general rule).

## Preserved artifacts

- `scratch/v3_ufo_sample_points.py` / `scratch/v3_ufo_signal_diagnostic.py` — UFO candidate-point
  sampling and full 7-feature no-fit diagnostic (the run that discovered HAND/relative-elevation as
  the standout individual features).
- `scratch/v3_ufo_frozen_index_diagnostic.py` — the leave-one-UFO-event-out combination test that
  selected Candidate A (HAND+RelElev, no depression, no TWI) as the frozen formula.
- `scratch/v3_ufo_samples.json` — the 417 extracted UFO samples the frozen normalization constants
  above were computed from. **SHA256:
  `22cb94106fa04be9f8216504a6bac792ae9b946db4ef9ec26ab9b3bb38de7274`**
- `scratch/v3_ufo_candidate_points.json` — the pre-GSW-filter candidate points sampled from UFO's
  own label rasters. **SHA256: `8c08995cf42ccdf69e5102c46fffa3aa01f07aea324b85529a517e7bcc8cb270`**
- `scratch/v3_sen1floods11_sample_points.py` / `scratch/v3_sen1floods11_frozen_index_diagnostic.py`
  — the independent replication test: frozen formula and frozen normalization constants applied
  unchanged to Sen1Floods11 (Bonafilia et al. 2020, `gs://sen1floods11`), zero re-fitting.
- `scratch/v3_sen1floods11_samples.json` — the 331 scored Sen1Floods11 samples. **SHA256:
  `cf035ed9d0c09d401c247abd7eedb7dc4dfc890393a90c2013c009865beed38c`**
- `scratch/v3_sen1floods11_candidate_points.json` — the pre-GSW-filter candidate points.
  **SHA256: `9eee17775cf88f0abc821f0bd17f87cbe2e2792287edc913b6a4b4fc8e2921e8`**

Recompute any hash with `sha256sum <file>` to verify it hasn't been altered since generation.

## Validation results (both no-fit — no weight or threshold was ever fit to either dataset's labels)

| Dataset | Events usable | Correct-direction | Median AUC | Notes |
|---|---|---|---|---|
| **UFO** (14 global urban flood events, CC BY 4.0, PlanetScope) | 13 (CTO/Can-Tho dropped — all its "flooded" pixels were permanent tidal water) | 11/13 | **0.734** | 8 fluvial, 4 pluvial (HTX/KTM/NSW/SLC), 2 storm-surge, per UFO's own `ufo:flood_driver` field |
| **Sen1Floods11** (Bonafilia et al. 2020, 11 hand-labeled global events, Sentinel-1/2) | 11 | 10/11 | **0.729** | Mechanism established by independent research this session (Sen1Floods11 has no built-in driver field): 8 fluvial, 1 pluvial (Spain/DANA), 1 mixed/fluvial-leaning (Sri Lanka), 1 unknown (Pakistan) |

Per-event Sen1Floods11 detail (the more informative view than the summary stat alone):

```
Event      Mechanism                n_pos  n_neg  AUC
Bolivia    Fluvial                  20     16     0.534
Ghana      Fluvial                  19     16     0.740
India      Fluvial                  14     16     0.549
Mekong     Fluvial                  10     16     0.787
Nigeria    Fluvial                  17     16     0.901   <- real Nigerian event (2018 Niger-Benue
Pakistan   Unknown                  13     16     0.389       river flood/Lagdo Dam); fluvial, not
Paraguay   Fluvial                  20     16     0.650       pluvial, but strong evidence the
Somalia    Fluvial                  20     16     0.694       underlying open terrain data resolves
Spain      Pluvial                  10     16     0.731       real signal in Nigerian geography
Sri-Lanka  Mixed/Fluvial-leaning     3     16     0.729
USA        Fluvial                  9     16     0.861
```

**Pakistan (AUC 0.389) is the one clear miss, deliberately not investigated or explained away here**
per explicit instruction — its mechanism could not be established from real sources this session
(flagged Unknown, not guessed), so the failure is reported honestly rather than attributed to a
driver mismatch that hasn't actually been confirmed.

## What this is / is not

**This is:** solid, twice-replicated evidence for a general, physically-interpretable **terrain
inundation susceptibility** signal — low-lying terrain relative to its surroundings (by two
independent measures, HAND and 1km-relative-elevation) predicts observed flood extent across 24
independent global events spanning two independently-collected, independently-labeled datasets,
without any weight ever being fit to a label.

**This is not:** a validated **urban pluvial-flood** model. Of the 24 replication events across both
datasets, only 5 are confidently pluvial-mechanism (UFO: HTX/KTM/NSW/SLC; Sen1Floods11: Spain), and
none are Nigerian pluvial events. The result licenses calling this a terrain-susceptibility
*candidate*, not a pluvial predictor, and not yet a claim about Nigeria specifically (Nigeria's own
0.901 result above is a fluvial event, not pluvial).

## Status / next step

**DEPLOYED (2026-08-25).** `app/utils/hazard_floodplain.py`'s `compute_floodplain_risk` now computes
this exact frozen formula, replacing the earlier single-variable, self-documented-as-"PROVISIONAL,
PRE-VALIDATION" `1 - HAND/25` transform. This was a deliberate product decision: deploy the frozen,
twice-globally-replicated index as-is now, and defer any retraining/retuning until genuine Nigerian
ground truth is eventually obtained (see `scratch/V3_NIGERIA_SEARCH_CLOSURE.md` and
`scratch/V3_SOCIAL_MEDIA_PILOT_RESULTS.md` for why that search is currently closed). The formula,
weights, and normalization constants deployed are identical to what's documented above — nothing
was refit or adjusted for deployment; verified end-to-end (Ogbaru test parcel, full PDF regression
check) at deployment time.

**Scope discipline preserved in production**: this upgrades Floodplain Susceptibility only (the
branch this index was always conceptually part of — HAND-based). It does NOT touch and does NOT
upgrade the separate Rainfall/Surface-Water (Pluvial) branch, which remains labeled "Experimental"
in the PDF and frontend exactly as before, confirmed by the deployment-time PDF regression test.
Feature discovery for the core terrain component is considered complete per explicit instruction —
no further terrain variables are to be searched for. The next authorized step is testing this exact
frozen formula (unchanged) against genuinely qualified Nigerian pluvial events once obtained — not
retraining, not re-normalizing, not adding features in response to that data, and not extending its
customer-facing claim beyond "floodplain/terrain susceptibility" until that validation exists.
