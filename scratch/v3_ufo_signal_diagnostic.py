"""Pluvial V3 R&D - UFO diagnostic, STEP 2: extract V3 physical predictors at the candidate points
sampled in step 1 (v3_ufo_sample_points.py -> v3_ufo_candidate_points.json) and compute the SAME
no-fit rank/Mann-Whitney-style AUC diagnostic used for Hadejia (v3_hadfcdr25_signal_diagnostic.py) -
NOT a fitted model, per the standing "do not train or modify any model" rule. NO production code
touched.

UFO's own label documentation states the "surface water" class includes pre-existing permanent or
seasonal water bodies, not just flood-caused inundation. Candidate positive (label==1) points are
therefore filtered here against JRC Global Surface Water occurrence (JRC/GSW1_4/GlobalSurfaceWater,
occurrence>50% = permanent water, excluded) before being used as "flooded" - otherwise depression/
HAND signal would be trivially inflated by pre-existing rivers/lakes sitting in topographic lows by
definition, not by flood-specific inundation.

Sign conventions are fixed BEFORE running (FEATURE_HIGHER_MEANS_FLOOD_PRONE below), learning from a
real mid-analysis correction needed in the Hadejia script: relative_elev_1000m, drain_dist_m, hand_m
are all physically expected to be LOWER at flood-prone points, so their oriented AUC is reported as
1 - raw_rank_auc directly - never eyeballed after the fact.

14 completely independent events (per UFO's own STAC metadata - not our own mechanism inference):
8 Fluvial, 4 Pluvial (HTX/Houston-Harvey, KTM/Khartoum, NSW/Kempsey, SLC/San-Lucia), 2 Storm-surge
(BEI/Beira, SPS/San-Pedro-Sula). Results are reported per-event, aggregated across all 14, AND
broken out for the Pluvial-driver subset specifically - UFO's own field, not an assumption.

Run: python scratch/v3_ufo_signal_diagnostic.py
"""
import json
import math
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.abspath("."))
from dotenv import load_dotenv
load_dotenv()

import ee
from app.utils.gee_client import init_gee
from app.utils.hazard_pluvial import _fetch_impervious_fraction

CANDIDATES_PATH = os.path.join(os.path.dirname(__file__), "v3_ufo_candidate_points.json")
OUT_SAMPLES_PATH = os.path.join(os.path.dirname(__file__), "v3_ufo_samples.json")

PERMANENT_WATER_OCCURRENCE_PCT = 50.0

FEATURES = ["twi_hs", "slope_deg", "depression_300m", "relative_elev_1000m", "drain_dist_m", "hand_m", "impervious_pct"]

# True = higher raw value expected at flood-prone points; False = lower raw value expected -> the
# oriented AUC below reports 1-raw for these so that, in every printed number, >0.5 always means
# "in the physically expected direction," matching the interpretation the diagnostic is built for.
FEATURE_HIGHER_MEANS_FLOOD_PRONE = {
    "twi_hs": True,               # higher topographic wetness index -> wetter, more flood-prone
    "slope_deg": False,           # flatter (lower slope) -> water pools, more flood-prone
    "depression_300m": True,      # more of a local basin relative to surroundings -> more flood-prone
    "relative_elev_1000m": False, # lower than the regional 1km context -> more flood-prone
    "drain_dist_m": False,        # closer to a drainage channel -> more flood-prone
    "hand_m": False,              # lower height-above-nearest-drainage -> more flood-prone
    "impervious_pct": True,       # more paved/built surface -> less infiltration, more flood-prone (contextual - see V2 double-count note)
}

PLUVIAL_EVENTS = {"HTX", "KTM", "NSW", "SLC"}


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


def _oriented_auc(pos, neg, feature):
    raw = _rank_auc(pos, neg)
    if math.isnan(raw):
        return raw
    return raw if FEATURE_HIGHER_MEANS_FLOOD_PRONE[feature] else 1.0 - raw


def main() -> int:
    with open(CANDIDATES_PATH, encoding="utf-8") as f:
        candidates = json.load(f)
    print(f"Loaded {len(candidates)} candidate points "
          f"({sum(1 for p in candidates if p['label']==1)} pos / {sum(1 for p in candidates if p['label']==0)} neg)")

    init_gee()
    dem = ee.ImageCollection("COPERNICUS/DEM/GLO30_2024_1").select("DEM").mosaic()
    slope_deg_img = ee.Terrain.slope(dem)
    slope_rad_img = slope_deg_img.multiply(math.pi / 180.0)
    hydrosheds_acc = ee.Image("WWF/HydroSHEDS/15ACC").select("b1")
    hydrosheds_cell_area = hydrosheds_acc.projection().nominalScale().pow(2)
    twi_hs_img = hydrosheds_acc.multiply(hydrosheds_cell_area).divide(slope_rad_img.tan().max(0.001)).log().rename("twi_hs")
    local_channels = hydrosheds_acc.gt(100)
    local_channel_dist_img = (
        local_channels.fastDistanceTransform(30).sqrt().multiply(hydrosheds_acc.projection().nominalScale()).rename("drain_dist_m")
    )
    hand_img = ee.Image("MERIT/Hydro/v1_0_1").select("hnd")
    gsw_occurrence_img = ee.Image("JRC/GSW1_4/GlobalSurfaceWater").select("occurrence")

    all_samples = []
    n_excluded_permanent_water = 0
    for i, pt_info in enumerate(candidates):
        lat, lon = pt_info["lat"], pt_info["lon"]
        pt = ee.Geometry.Point([lon, lat])
        parcel = pt.buffer(30)
        local_area = pt.buffer(300)
        wide_area = pt.buffer(1000)
        try:
            combined = ee.Dictionary({
                "twi_hs": twi_hs_img.reduceRegion(ee.Reducer.mean(), local_area, scale=450, maxPixels=1e9, bestEffort=True).get("twi_hs"),
                "slope_deg": slope_deg_img.reduceRegion(ee.Reducer.mean(), parcel, scale=30, maxPixels=1e9).get("slope"),
                "elev_m": dem.reduceRegion(ee.Reducer.mean(), parcel, scale=30, maxPixels=1e9).get("DEM"),
                "focal_300_m": dem.focal_mean(radius=300, units="meters").reduceRegion(ee.Reducer.mean(), parcel, scale=30, maxPixels=1e9).get("DEM"),
                "focal_1000_m": dem.focal_mean(radius=1000, units="meters").reduceRegion(ee.Reducer.mean(), parcel, scale=30, maxPixels=1e9).get("DEM"),
                "drain_dist_m": local_channel_dist_img.reduceRegion(ee.Reducer.mean(), parcel, scale=90, maxPixels=1e9).get("drain_dist_m"),
                "hand_m": hand_img.reduceRegion(ee.Reducer.mean(), local_area, scale=90, maxPixels=1e9, bestEffort=True).get("hnd"),
                "gsw_occurrence_pct": gsw_occurrence_img.reduceRegion(ee.Reducer.mean(), parcel, scale=30, maxPixels=1e9, bestEffort=True).get("occurrence"),
            }).getInfo()
            impervious_frac, _, _ = _fetch_impervious_fraction(parcel)
        except Exception as exc:
            print(f"  [ERROR] point {i} {pt_info['location_code']} ({lat:.4f},{lon:.4f}): {exc!r}", flush=True)
            continue

        gsw_occ = combined.get("gsw_occurrence_pct")
        if pt_info["label"] == 1 and gsw_occ is not None and gsw_occ > PERMANENT_WATER_OCCURRENCE_PCT:
            n_excluded_permanent_water += 1
            continue

        elev = combined.get("elev_m")
        focal300 = combined.get("focal_300_m")
        focal1000 = combined.get("focal_1000_m")
        depression_300m = (focal300 - elev) if (elev is not None and focal300 is not None) else None
        relative_elev_1000m = (elev - focal1000) if (elev is not None and focal1000 is not None) else None
        raw_drain_dist = combined.get("drain_dist_m")
        capped_drain_dist = min(float(raw_drain_dist), 2000.0) if raw_drain_dist is not None else None

        sample = {
            "location_code": pt_info["location_code"], "location": pt_info["location"],
            "flood_driver": pt_info["flood_driver"], "chip_id": pt_info["chip_id"],
            "label": pt_info["label"], "lat": lat, "lon": lon,
            "twi_hs": combined.get("twi_hs"), "slope_deg": combined.get("slope_deg"),
            "depression_300m": depression_300m, "relative_elev_1000m": relative_elev_1000m,
            "drain_dist_m": capped_drain_dist, "hand_m": combined.get("hand_m"),
            "impervious_pct": round(impervious_frac * 100, 1) if impervious_frac is not None else None,
            "gsw_occurrence_pct": gsw_occ,
        }
        all_samples.append(sample)
        if (i + 1) % 50 == 0:
            print(f"  ... {i+1}/{len(candidates)} candidates processed, {len(all_samples)} kept so far", flush=True)

    print(f"\n{'='*100}")
    print(f"Total candidates: {len(candidates)} | Excluded as permanent water (JRC GSW>{PERMANENT_WATER_OCCURRENCE_PCT}%): {n_excluded_permanent_water} | Final samples: {len(all_samples)}")
    print(f"{'='*100}")

    with open(OUT_SAMPLES_PATH, "w", encoding="utf-8") as f:
        json.dump(all_samples, f, indent=2)
    print(f"Samples preserved: {OUT_SAMPLES_PATH}")

    events = sorted(set(s["location_code"] for s in all_samples))

    def _report(samples, title):
        print(f"\n{'='*100}\n{title}\n{'='*100}")
        flooded = [s for s in samples if s["label"] == 1]
        non_flooded = [s for s in samples if s["label"] == 0]
        print(f"n_pos={len(flooded)}  n_neg={len(non_flooded)}")
        for feat in FEATURES:
            pos = [s[feat] for s in flooded if s.get(feat) is not None]
            neg = [s[feat] for s in non_flooded if s.get(feat) is not None]
            if len(pos) < 2 or len(neg) < 2:
                print(f"  {feat:22s}: insufficient data (pos={len(pos)}, neg={len(neg)})")
                continue
            auc = _oriented_auc(pos, neg, feat)
            print(f"  {feat:22s}: oriented AUC={auc:.3f}  (n_pos={len(pos)}, n_neg={len(neg)})")

    # --- Per-event AUC table (the core requested experiment: 14 independent trials) ---------------
    print(f"\n{'='*100}\nPER-EVENT ORIENTED AUC  (>0.5 = correct physically-expected direction)\n{'='*100}")
    header = f"{'Event':6s} {'Driver':12s} {'n_pos':6s} {'n_neg':6s} " + " ".join(f"{f[:14]:14s}" for f in FEATURES)
    print(header)
    per_event_auc = defaultdict(dict)
    for loc in events:
        ev_samples = [s for s in all_samples if s["location_code"] == loc]
        flooded = [s for s in ev_samples if s["label"] == 1]
        non_flooded = [s for s in ev_samples if s["label"] == 0]
        driver = ev_samples[0]["flood_driver"] if ev_samples else "?"
        row = f"{loc:6s} {driver:12s} {len(flooded):<6d} {len(non_flooded):<6d} "
        for feat in FEATURES:
            pos = [s[feat] for s in flooded if s.get(feat) is not None]
            neg = [s[feat] for s in non_flooded if s.get(feat) is not None]
            if len(pos) < 2 or len(neg) < 2:
                row += f"{'n/a':14s} "
                per_event_auc[feat][loc] = None
                continue
            auc = _oriented_auc(pos, neg, feat)
            row += f"{auc:<14.3f} "
            per_event_auc[feat][loc] = auc
        print(row)

    # --- Aggregate: median AUC + count of events with AUC>0.5, across ALL 14 and PLUVIAL-only -----
    def _aggregate(loc_subset, title):
        print(f"\n{'='*100}\n{title}\n{'='*100}")
        print(f"{'Feature':22s} {'median_AUC':12s} {'n_correct_dir':14s} {'n_events':10s}")
        for feat in FEATURES:
            vals = [per_event_auc[feat][loc] for loc in loc_subset if per_event_auc[feat].get(loc) is not None]
            if not vals:
                print(f"  {feat:22s}: no data")
                continue
            vals_sorted = sorted(vals)
            n = len(vals_sorted)
            median = vals_sorted[n // 2] if n % 2 else (vals_sorted[n // 2 - 1] + vals_sorted[n // 2]) / 2
            n_correct = sum(1 for v in vals if v > 0.5)
            print(f"  {feat:22s} {median:<12.3f} {n_correct}/{n:<12d} {n:<10d}")

    _aggregate(events, "AGGREGATE ACROSS ALL 14 UFO EVENTS")
    _aggregate([e for e in events if e in PLUVIAL_EVENTS], "AGGREGATE ACROSS PLUVIAL-DRIVER EVENTS ONLY (HTX/KTM/NSW/SLC, per UFO's own ufo:flood_driver field)")

    _report(all_samples, "POOLED (all 14 events combined, all points) - for reference only, NOT the primary result (pools non-independent events, use per-event table above for the real experiment)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
