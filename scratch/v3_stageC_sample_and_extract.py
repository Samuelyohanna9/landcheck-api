"""Pluvial V3 R&D - STAGE C, step 2: stratified sampling from the visually-reviewed flood masks,
then physically-motivated feature extraction per sampled point. NO production code touched.

Mask confidence, assigned by ACTUAL visual inspection of the rendered PNGs (not just the area-
percentage statistic, which would have wrongly accepted all three):
  - Lekki:      LOW    - plausible-flood pixels are scattered, isolated speckle with no spatial
                          coherence across the whole frame. Consistent with SAR noise, not a real
                          flood signature, despite passing the naive 0.5-60% area threshold.
  - Abuja_FCT:  MEDIUM - real spatial clustering, concentrated in one part of the frame, though
                          still noisy elsewhere.
  - Maiduguri:  HIGH   - plausible-flood pixels trace a clear dendritic/channel-following pattern
                          plus a dense cluster - physically sensible for a dam-breach flood
                          following the drainage network downstream.
Per instruction, Lekki's labels are retained (not silently dropped) but flagged LOW confidence
throughout - any result where Lekki is the held-out city, or where Lekki dominates the training
fold, should be read with real skepticism. This is disclosed, not hidden.

Uses the EXACT scene IDs from the visually-reviewed manifest (not re-searched) so sampled labels
are guaranteed to come from the images actually inspected.

Physically-motivated predictors only (per instruction), HAND included ONLY as an optional
contextual feature (explicitly checked for dominance in the modelling step, not assumed benign):
  twi_hs (HydroSHEDS-based TWI - Stage A's best-performing raw signal), slope_deg, depression_300m
  (local ponding, same window as production), relative_elev_1000m (regional context), chirps_p99_mm
  (climatological design storm, same as production), event_rainfall_mm (ACTUAL CHIRPS daily total
  on the event date +/- 1 day - a genuinely new, event-specific predictor production doesn't have),
  sand_pct/clay_pct/hsg, runoff_mm/runoff_coefficient (SCS-CN using event_rainfall_mm, not the
  climatological figure - deliberately different from production's own runoff input), impervious_pct
  (parcel-scale, Esri LULC), distance_to_drainage_m (HydroSHEDS local channel, capped 2000m, flagged
  reliability caveat per hazard_pluvial.py's own comments), hand_m (MERIT Hydro, contextual only).

Run: python scratch/v3_stageC_sample_and_extract.py
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
from app.utils.hazard_local_data import compute_scs_runoff, derive_hydrologic_soil_group, DEFAULT_SITE_TYPE
from app.utils.hazard_pluvial import _fetch_impervious_fraction

OUT_DIR = os.path.join(os.path.dirname(__file__), "v3_stageC_masks")
POINTS_PER_CLASS = 8  # per event, per class (flooded/non-flooded) - kept small: this is a feasibility
                       # exercise for the pipeline itself, not a production-scale training run.

EVENTS = [
    {"city": "Lekki", "lat": 6.4650, "lon": 3.5658, "event_date": "2024-07-04", "mask_confidence": "LOW",
     "pre_scene": "S1A_IW_GRDH_1SDV_20240703T053017_20240703T053046_054592_06A521_9E4B",
     "post_scene": "S1A_IW_GRDH_1SDV_20240715T053017_20240715T053046_054767_06AB2E_4470"},
    {"city": "Abuja_FCT", "lat": 9.0765, "lon": 7.4898, "event_date": "2024-07-04", "mask_confidence": "MEDIUM",
     "pre_scene": "S1A_IW_GRDH_1SDV_20240628T174633_20240628T174658_054527_06A2CC_B025",
     "post_scene": "S1A_IW_GRDH_1SDV_20240710T174632_20240710T174657_054702_06A8E8_4B41"},
    {"city": "Maiduguri", "lat": 11.8333, "lon": 13.1500, "event_date": "2024-09-10", "mask_confidence": "HIGH",
     "pre_scene": "S1A_IW_GRDH_1SDV_20240905T172228_20240905T172253_055533_06C6CA_F843",
     "post_scene": "S1A_IW_GRDH_1SDV_20240917T172229_20240917T172254_055708_06CDB7_F730"},
]

BACKSCATTER_DROP_THRESHOLD_DB = -3.0
PERMANENT_WATER_OCCURRENCE_PCT = 50.0
SLOPE_PLAUSIBILITY_DEG = 5.0
AOI_RADIUS_M = 3000


def main() -> int:
    init_gee()
    dem = ee.ImageCollection("COPERNICUS/DEM/GLO30_2024_1").select("DEM").mosaic()
    slope_deg_img = ee.Terrain.slope(dem)
    slope_rad_img = slope_deg_img.multiply(math.pi / 180.0)
    gsw_occurrence = ee.Image("JRC/GSW1_4/GlobalSurfaceWater").select("occurrence")

    hydrosheds_acc = ee.Image("WWF/HydroSHEDS/15ACC").select("b1")
    hydrosheds_cell_area = hydrosheds_acc.projection().nominalScale().pow(2)
    twi_hs_img = hydrosheds_acc.multiply(hydrosheds_cell_area).divide(slope_rad_img.tan().max(0.001)).log().rename("twi_hs")
    local_channels = hydrosheds_acc.gt(100)
    local_channel_dist_img = (
        local_channels.fastDistanceTransform(30).sqrt().multiply(hydrosheds_acc.projection().nominalScale()).rename("drain_dist_m")
    )
    hand_img = ee.Image("MERIT/Hydro/v1_0_1").select("hnd")

    all_samples = []
    for ev in EVENTS:
        city, lat, lon, event_date = ev["city"], ev["lat"], ev["lon"], ev["event_date"]
        print(f"\n=== Sampling {city} (mask confidence: {ev['mask_confidence']}) ===", flush=True)
        aoi = ee.Geometry.Point([lon, lat]).buffer(AOI_RADIUS_M)

        pre_img = ee.Image(f"COPERNICUS/S1_GRD/{ev['pre_scene']}").select("VV")
        post_img = ee.Image(f"COPERNICUS/S1_GRD/{ev['post_scene']}").select("VV")
        diff_db = post_img.focal_median(50, "circle", "meters").subtract(pre_img.focal_median(50, "circle", "meters"))
        flood_candidate = diff_db.lte(BACKSCATTER_DROP_THRESHOLD_DB)
        permanent_water = gsw_occurrence.gt(PERMANENT_WATER_OCCURRENCE_PCT).unmask(0)
        steep_terrain = slope_deg_img.gt(SLOPE_PLAUSIBILITY_DEG).unmask(0)
        plausible_flood = flood_candidate.And(permanent_water.Not()).And(steep_terrain.Not())
        eligible_negative = plausible_flood.Not().And(permanent_water.Not())

        class_img = ee.Image(0).where(plausible_flood, 1).where(eligible_negative, 0).rename("cls")
        # Mask out anything that's neither a plausible-flood pixel NOR an eligible-negative pixel
        # (there is none by construction here, but this keeps the stratified sample honest).
        class_img = class_img.updateMask(plausible_flood.Or(eligible_negative))

        fc = class_img.stratifiedSample(
            numPoints=POINTS_PER_CLASS, classBand="cls", region=aoi, scale=20, seed=42, geometries=True,
        )
        try:
            features = fc.getInfo()["features"]
        except Exception as exc:
            print(f"[ERROR] {city} sampling failed: {exc!r}", flush=True)
            continue
        print(f"  Sampled {len(features)} points ({sum(1 for f in features if f['properties']['cls']==1)} flooded, "
              f"{sum(1 for f in features if f['properties']['cls']==0)} non-flooded)", flush=True)

        chirps_daily = ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY").select("precipitation")
        p99_image = chirps_daily.reduce(ee.Reducer.percentile([99])).rename("p99_daily_mm")
        event_start = ee.Date(event_date).advance(-1, "day")
        event_end = ee.Date(event_date).advance(1, "day")
        event_rain_img = chirps_daily.filterDate(event_start, event_end).sum().rename("event_rain_mm")

        for feat in features:
            plon, plat = feat["geometry"]["coordinates"]
            cls = feat["properties"]["cls"]
            pt = ee.Geometry.Point([plon, plat])
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
                    "chirps_p99_mm": p99_image.reduceRegion(ee.Reducer.mean(), wide_area, scale=5500, maxPixels=1e9).get("p99_daily_mm"),
                    "event_rain_mm": event_rain_img.reduceRegion(ee.Reducer.mean(), wide_area, scale=5500, maxPixels=1e9).get("event_rain_mm"),
                    "sand_pct": ee.Image("OpenLandMap/SOL/SOL_SAND-WFRACTION_USDA-3A1A1A_M/v02").select("b0").reduceRegion(ee.Reducer.mean(), local_area, scale=250, maxPixels=1e9).get("b0"),
                    "clay_pct": ee.Image("OpenLandMap/SOL/SOL_CLAY-WFRACTION_USDA-3A1A1A_M/v02").select("b0").reduceRegion(ee.Reducer.mean(), local_area, scale=250, maxPixels=1e9).get("b0"),
                    "drain_dist_m": local_channel_dist_img.reduceRegion(ee.Reducer.mean(), parcel, scale=90, maxPixels=1e9).get("drain_dist_m"),
                    "hand_m": hand_img.reduceRegion(ee.Reducer.mean(), local_area, scale=90, maxPixels=1e9, bestEffort=True).get("hnd"),
                }).getInfo()
                impervious_frac, _, _ = _fetch_impervious_fraction(parcel)
            except Exception as exc:
                print(f"    [ERROR] point ({plat:.4f},{plon:.4f}): {exc!r}", flush=True)
                continue

            elev = combined.get("elev_m")
            focal300 = combined.get("focal_300_m")
            focal1000 = combined.get("focal_1000_m")
            depression_300m = (focal300 - elev) if (elev is not None and focal300 is not None) else None
            relative_elev_1000m = (elev - focal1000) if (elev is not None and focal1000 is not None) else None

            sand = combined.get("sand_pct")
            clay = combined.get("clay_pct")
            hsg = derive_hydrologic_soil_group(sand, clay) if sand is not None and clay is not None else "B"
            event_rain = combined.get("event_rain_mm")
            scs = compute_scs_runoff(hsg, DEFAULT_SITE_TYPE, event_rain) if event_rain else None

            raw_drain_dist = combined.get("drain_dist_m")
            capped_drain_dist = min(float(raw_drain_dist), 2000.0) if raw_drain_dist is not None else None

            sample = {
                "city": city, "lat": plat, "lon": plon, "label": cls, "mask_confidence": ev["mask_confidence"],
                "twi_hs": combined.get("twi_hs"), "slope_deg": combined.get("slope_deg"),
                "depression_300m": depression_300m, "relative_elev_1000m": relative_elev_1000m,
                "chirps_p99_mm": combined.get("chirps_p99_mm"), "event_rain_mm": event_rain,
                "sand_pct": sand, "clay_pct": clay, "hsg": hsg,
                "runoff_mm": scs["runoff_mm"] if scs else None,
                "runoff_coefficient": scs["runoff_coefficient"] if scs else None,
                "impervious_pct": round(impervious_frac * 100, 1) if impervious_frac is not None else None,
                "drain_dist_m": capped_drain_dist,
                "hand_m": combined.get("hand_m"),
            }
            all_samples.append(sample)
            print(f"    [{cls}] ({plat:.4f},{plon:.4f}) twi={sample['twi_hs']} dep300={sample['depression_300m']} "
                  f"imperv={sample['impervious_pct']}% hand={sample['hand_m']}", flush=True)

    out_path = os.path.join(OUT_DIR, "stageC_samples.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_samples, f, indent=2)
    print(f"\n{len(all_samples)} total samples written to {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
