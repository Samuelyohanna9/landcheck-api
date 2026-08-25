"""Pluvial V3 R&D - independent replication test STEP 2: extract HAND + relative elevation at the
Sen1Floods11 candidate points (step 1) and score them with the FROZEN V3.1 index exactly as decided
against UFO - no weight change, no normalization re-fit, no new candidate combinations. NO
production code touched, NO model trained.

V3.1 = 0.5 * minmax_frozen(-hand_m) + 0.5 * minmax_frozen(-relative_elev_1000m)

The min-max normalization range is the FROZEN constant computed once from the full UFO sample set
(all 13 usable UFO events pooled, n=417) and is NOT recomputed here - Sen1Floods11 values are
rescaled through that fixed range and clipped to [0,1] if they fall outside it (tracked and
reported, since extrapolation beyond the range the index was defined on is a real caveat, not
hidden). This is the correct way to test genuine out-of-sample generalization: if the range were
recomputed from Sen1Floods11 itself, the index would be silently re-tuned to the new dataset, not
frozen.

Frozen constants (from scratch/v3_ufo_samples.json, oriented so higher = more flood-prone):
  hand_m oriented (-hand_m) range:               [-165.947, -0.011]
  relative_elev_1000m oriented (-rel_elev) range: [-90.064, 76.091]

JRC Global Surface Water permanent-water exclusion applied to candidate positives exactly as for
UFO (occurrence > 50% excluded) - Sen1Floods11's "water" label is a hand classification of the
Sentinel-1 scene and, like UFO, is not guaranteed to exclude pre-existing rivers/lakes/wetlands.

Run: python scratch/v3_sen1floods11_frozen_index_diagnostic.py
"""
import json
import math
import os
import sys

sys.path.insert(0, os.path.abspath("."))
from dotenv import load_dotenv
load_dotenv()

import ee
from app.utils.gee_client import init_gee

CANDIDATES_PATH = os.path.join(os.path.dirname(__file__), "v3_sen1floods11_candidate_points.json")
OUT_SAMPLES_PATH = os.path.join(os.path.dirname(__file__), "v3_sen1floods11_samples.json")

PERMANENT_WATER_OCCURRENCE_PCT = 50.0

# FROZEN from UFO (scratch/v3_ufo_samples.json, n=417) - never recomputed here.
HAND_ORIENTED_MIN, HAND_ORIENTED_MAX = -165.94714902903826, -0.011056458756802398
RELELEV_ORIENTED_MIN, RELELEV_ORIENTED_MAX = -90.06438029231361, 76.09087863884835

PLUVIAL_EVENTS = {"Spain"}  # the only confidently-pluvial event in this dataset (see step 1 notes)


def _rank_auc(pos, neg):
    pairs = concordant = ties = 0
    for p in pos:
        for n in neg:
            pairs += 1
            if p > n:
                concordant += 1
            elif p == n:
                ties += 1
    return (concordant + 0.5 * ties) / pairs if pairs else float("nan")


def _frozen_index(hand_m, relative_elev_1000m):
    hand_oriented = -hand_m
    relelev_oriented = -relative_elev_1000m
    hand_scaled = (hand_oriented - HAND_ORIENTED_MIN) / (HAND_ORIENTED_MAX - HAND_ORIENTED_MIN)
    relelev_scaled = (relelev_oriented - RELELEV_ORIENTED_MIN) / (RELELEV_ORIENTED_MAX - RELELEV_ORIENTED_MIN)
    clipped = False
    if not (0.0 <= hand_scaled <= 1.0):
        clipped = True
        hand_scaled = min(1.0, max(0.0, hand_scaled))
    if not (0.0 <= relelev_scaled <= 1.0):
        clipped = True
        relelev_scaled = min(1.0, max(0.0, relelev_scaled))
    return 0.5 * hand_scaled + 0.5 * relelev_scaled, clipped


def main() -> int:
    with open(CANDIDATES_PATH, encoding="utf-8") as f:
        candidates = json.load(f)
    print(f"Loaded {len(candidates)} candidate points "
          f"({sum(1 for p in candidates if p['label']==1)} pos / {sum(1 for p in candidates if p['label']==0)} neg)")

    init_gee()
    dem = ee.ImageCollection("COPERNICUS/DEM/GLO30_2024_1").select("DEM").mosaic()
    hand_img = ee.Image("MERIT/Hydro/v1_0_1").select("hnd")
    gsw_occurrence_img = ee.Image("JRC/GSW1_4/GlobalSurfaceWater").select("occurrence")

    all_samples = []
    n_excluded_permanent_water = 0
    n_clipped = 0
    for i, pt_info in enumerate(candidates):
        lat, lon = pt_info["lat"], pt_info["lon"]
        pt = ee.Geometry.Point([lon, lat])
        parcel = pt.buffer(30)
        local_area = pt.buffer(300)
        try:
            combined = ee.Dictionary({
                "elev_m": dem.reduceRegion(ee.Reducer.mean(), parcel, scale=30, maxPixels=1e9).get("DEM"),
                "focal_1000_m": dem.focal_mean(radius=1000, units="meters").reduceRegion(ee.Reducer.mean(), parcel, scale=30, maxPixels=1e9).get("DEM"),
                "hand_m": hand_img.reduceRegion(ee.Reducer.mean(), local_area, scale=90, maxPixels=1e9, bestEffort=True).get("hnd"),
                "gsw_occurrence_pct": gsw_occurrence_img.reduceRegion(ee.Reducer.mean(), parcel, scale=30, maxPixels=1e9, bestEffort=True).get("occurrence"),
            }).getInfo()
        except Exception as exc:
            print(f"  [ERROR] point {i} {pt_info['location_code']} ({lat:.4f},{lon:.4f}): {exc!r}", flush=True)
            continue

        gsw_occ = combined.get("gsw_occurrence_pct")
        if pt_info["label"] == 1 and gsw_occ is not None and gsw_occ > PERMANENT_WATER_OCCURRENCE_PCT:
            n_excluded_permanent_water += 1
            continue

        elev = combined.get("elev_m")
        focal1000 = combined.get("focal_1000_m")
        hand_m = combined.get("hand_m")
        if elev is None or focal1000 is None or hand_m is None:
            continue
        relative_elev_1000m = elev - focal1000

        index_value, clipped = _frozen_index(hand_m, relative_elev_1000m)
        if clipped:
            n_clipped += 1

        sample = {
            "location_code": pt_info["location_code"], "flood_driver": pt_info["flood_driver"],
            "chip_id": pt_info["chip_id"], "label": pt_info["label"], "lat": lat, "lon": lon,
            "hand_m": hand_m, "relative_elev_1000m": relative_elev_1000m,
            "gsw_occurrence_pct": gsw_occ, "v3_1_index": index_value, "index_clipped": clipped,
        }
        all_samples.append(sample)
        if (i + 1) % 50 == 0:
            print(f"  ... {i+1}/{len(candidates)} candidates processed, {len(all_samples)} kept so far", flush=True)

    print(f"\n{'='*100}")
    print(f"Total candidates: {len(candidates)} | Excluded as permanent water: {n_excluded_permanent_water} | "
          f"Final samples: {len(all_samples)} | Clipped (outside UFO's frozen range): {n_clipped}")
    print(f"{'='*100}")

    with open(OUT_SAMPLES_PATH, "w", encoding="utf-8") as f:
        json.dump(all_samples, f, indent=2)
    print(f"Samples preserved: {OUT_SAMPLES_PATH}")

    events = sorted(set(s["location_code"] for s in all_samples))
    print(f"\n{'='*100}\nPER-EVENT V3.1 FROZEN INDEX AUC (unchanged formula/weights/normalization from UFO)\n{'='*100}")
    print(f"{'Event':10s} {'Mechanism':24s} {'n_pos':6s} {'n_neg':6s} {'AUC':8s}")
    per_event_auc = {}
    for loc in events:
        ev_samples = [s for s in all_samples if s["location_code"] == loc]
        flooded = [s["v3_1_index"] for s in ev_samples if s["label"] == 1]
        non_flooded = [s["v3_1_index"] for s in ev_samples if s["label"] == 0]
        driver = ev_samples[0]["flood_driver"]
        if len(flooded) < 2 or len(non_flooded) < 2:
            print(f"{loc:10s} {driver:24s} {len(flooded):<6d} {len(non_flooded):<6d} {'n/a':8s}")
            per_event_auc[loc] = None
            continue
        auc = _rank_auc(flooded, non_flooded)
        per_event_auc[loc] = auc
        print(f"{loc:10s} {driver:24s} {len(flooded):<6d} {len(non_flooded):<6d} {auc:<8.3f}")

    def _summarize(loc_subset, title):
        vals = [per_event_auc[loc] for loc in loc_subset if per_event_auc.get(loc) is not None]
        print(f"\n{title}")
        if not vals:
            print("  no usable events")
            return
        vals_sorted = sorted(vals)
        n = len(vals_sorted)
        median = vals_sorted[n // 2] if n % 2 else (vals_sorted[n // 2 - 1] + vals_sorted[n // 2]) / 2
        n_correct = sum(1 for v in vals if v > 0.5)
        print(f"  median AUC={median:.3f}  correct-direction={n_correct}/{n}  events={loc_subset if len(loc_subset)<6 else '...'}")

    print(f"\n{'='*100}\nAGGREGATE\n{'='*100}")
    _summarize(events, "ALL usable Sen1Floods11 events (fluvial/dam-related/mixed/unknown - NOT a pluvial-specific test):")
    fluvial_only = [e for e in events if all_samples and any(
        s["location_code"] == e and s["flood_driver"] == "Fluvial" for s in all_samples)]
    _summarize(fluvial_only, "Fluvial-only subset (for reference - most of this dataset):")
    pluvial_only = [e for e in events if e in PLUVIAL_EVENTS]
    if pluvial_only:
        vals = [per_event_auc[e] for e in pluvial_only if per_event_auc.get(e) is not None]
        print(f"\nPluvial-only subset: n={len(pluvial_only)} event(s) ({pluvial_only}) - "
              f"{'AUC=' + format(vals[0], '.3f') if vals else 'no data'}. "
              f"NOT reported as a median/aggregate - a single event has no meaningful aggregate statistic.")
    else:
        print("\nPluvial-only subset: no confidently pluvial-mechanism events in this dataset.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
