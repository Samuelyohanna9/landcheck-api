"""Pluvial V3 R&D - Nigerian ground-truth qualification, Priority 1 (Ondo Town): Sentinel-1
same-orbit change-detection feasibility + visual-review mask for the 4 October 2024 Ondo Town flood.
NO production code touched, NO model trained, NO change to the frozen V3.1 formula regardless of
outcome (per explicit instruction - Ondo is testing the hypothesis, not helping construct it).

Event evidence (independently verified via WebSearch this session, not merely asserted):
  - NEMA DG (Hajia Zubaida Umar) conducted an on-the-spot assessment of Ondo Town, explicitly
    citing blocked waterways/drainage channels and poor drainage maintenance as contributing
    causes - an authoritative federal source, not just eyewitness reporting.
  - Businessday NG: eyewitnesses attributed flooding to blockage of drainage channels preventing
    free flow of water during heavy rain.
  - Event: heavy downpour started ~3pm Friday 4 October 2024, "almost all roads in town" affected -
    ~1,000 homes, 25 schools, 20 worship centres, 7,000+ people affected. No river/dam/coastal
    mechanism reported anywhere found - a genuinely defensible predominantly-pluvial candidate.
  - Named affected areas (for later visual cross-check, not all individually geocoded here):
    Ita-Nla/Itanla road, Oke-Odunwo, Odojomu, Oke-Idera, Iluyemi, Odo, Oka, Ijomu,
    Akure-Ondo Expressway, Bethlehem, Olorunishola, Fagun, Jilalu, New Town/Gani Street,
    Yaba Police Station, Ademulegun Road, Ife Road - spread across the town, consistent with the
    "almost all roads" reporting (i.e. this is not one isolated street, it's town-wide).
  - Ondo Town centre (Wikipedia): 7.088923N, 4.799094E.

KNOWN FEASIBILITY RISK (stated before running, not after): this is a short-duration pluvial flash
flood (hours, not days) - unlike the fluvial events this session's methodology has mostly handled,
floodwater from a event like this often recedes within 24-48 hours. Sentinel-1's revisit cadence at
this latitude is ~6-12 days. If no S1 acquisition happened to fall within roughly 1-2 days of
2024-10-04, the water will most likely already be gone by the next available pass, and this
approach may simply be INFEASIBLE for this event - that is a real, reportable result, not a
failure to fix.

Methodology identical to Stage B/C (v3_stageB_sentinel1_feasibility.py /
v3_stageC_generate_masks.py) - not re-derived: VV backscatter drop <=-3dB, same relative orbit
pre/post, JRC GSW permanent-water exclusion, GLO-30 slope>5deg implausibility exclusion, PNG
rendered for genuine visual review before any confidence is assigned. No area-percentage threshold
alone is treated as sufficient (per standing instruction).

Run: python scratch/v3_ondo_sentinel1_feasibility.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath("."))
from dotenv import load_dotenv
load_dotenv()

import ee
from app.utils.gee_client import init_gee

EVENT_DATE = "2024-10-04"
LAT, LON = 7.088923, 4.799094
AOI_RADIUS_M = 4000  # covers Ondo Town's urban extent - "almost all roads in town" were affected

BACKSCATTER_DROP_THRESHOLD_DB = -3.0
PERMANENT_WATER_OCCURRENCE_PCT = 50.0
SLOPE_PLAUSIBILITY_DEG = 5.0

OUT_DIR = os.path.join(os.path.dirname(__file__), "v3_ondo_masks")


def _s1_collection(aoi):
    return (
        ee.ImageCollection("COPERNICUS/S1_GRD")
        .filterBounds(aoi)
        .filter(ee.Filter.eq("instrumentMode", "IW"))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
        .select("VV")
    )


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    init_gee()

    aoi = ee.Geometry.Point([LON, LAT]).buffer(AOI_RADIUS_M)
    event_dt = ee.Date(EVENT_DATE)

    s1 = _s1_collection(aoi)

    # --- Feasibility: list EVERY S1 scene within +/-20 days of the event with its exact date/orbit,
    # so the "was there a pass close enough to catch transient floodwater" question is answered from
    # real data, not assumed.
    window = s1.filterDate(event_dt.advance(-20, "day"), event_dt.advance(20, "day")).sort("system:time_start")
    n_scenes = window.size().getInfo()
    print(f"Sentinel-1 VV/IW scenes within +/-20 days of {EVENT_DATE} at Ondo Town: {n_scenes}")
    if n_scenes:
        info_list = window.toList(n_scenes)
        for i in range(n_scenes):
            img = ee.Image(info_list.get(i))
            meta = img.toDictionary(["system:index", "system:time_start", "relativeOrbitNumber_start"]).getInfo()
            date_str = ee.Date(meta["system:time_start"]).format("YYYY-MM-dd").getInfo()
            days_from_event = ee.Date(meta["system:time_start"]).difference(event_dt, "day").getInfo()
            print(f"  {meta.get('system:index')}  date={date_str}  orbit={meta.get('relativeOrbitNumber_start')}  "
                  f"days_from_event={days_from_event:+.1f}")

    # --- Pre/post selection: pre = latest scene strictly before the event; post = earliest scene
    # on/after the event, matched to the SAME relative orbit as pre (hard same-orbit requirement).
    pre_col = s1.filterDate(event_dt.advance(-30, "day"), event_dt).sort("system:time_start", False)
    post_col_all = s1.filterDate(event_dt, event_dt.advance(15, "day")).sort("system:time_start", True)

    pre_count = pre_col.size().getInfo()
    if pre_count == 0:
        print("\n[INFEASIBLE] No pre-event Sentinel-1 scene found in the 30 days before the event.")
        return 0
    pre_img = ee.Image(pre_col.first())
    pre_meta = pre_img.toDictionary(["system:index", "system:time_start", "relativeOrbitNumber_start"]).getInfo()
    pre_orbit = pre_meta.get("relativeOrbitNumber_start")
    pre_date = ee.Date(pre_meta["system:time_start"]).format("YYYY-MM-dd").getInfo()
    print(f"\nPRE  scene: {pre_meta.get('system:index')}  date={pre_date}  orbit={pre_orbit}")

    post_same_orbit = post_col_all.filter(ee.Filter.eq("relativeOrbitNumber_start", pre_orbit))
    post_count = post_same_orbit.size().getInfo()
    if post_count == 0:
        print(f"[INFEASIBLE] No same-orbit (orbit={pre_orbit}) post-event scene found within 15 days after {EVENT_DATE}.")
        print("This is the real, reportable feasibility result: same-orbit SAR change detection is")
        print("not usable for THIS event with THIS constraint. Not attempting a different-orbit")
        print("comparison (would repeat the Maiduguri overestimation mistake from Stage B).")
        return 0

    post_img = ee.Image(post_same_orbit.sort("system:time_start", True).first())
    post_meta = post_img.toDictionary(["system:index", "system:time_start", "relativeOrbitNumber_start"]).getInfo()
    post_date = ee.Date(post_meta["system:time_start"]).format("YYYY-MM-dd").getInfo()
    days_after_event = ee.Date(post_meta["system:time_start"]).difference(event_dt, "day").getInfo()
    print(f"POST scene: {post_meta.get('system:index')}  date={post_date}  orbit={post_meta.get('relativeOrbitNumber_start')}  "
          f"({days_after_event:.1f} days after the event)")

    if days_after_event > 3:
        print(f"\n[CAUTION] The nearest same-orbit post-event scene is {days_after_event:.1f} days after the "
              f"event. For a short-duration pluvial flash flood, floodwater may well have already "
              f"receded by then - a negative/weak result below would NOT necessarily mean the area "
              f"didn't flood, only that SAR didn't catch it in time. This will be reported as an "
              f"explicit caveat on any mask produced, not silently treated as a clean non-detection.")

    # --- Generate + visually render the mask (same methodology as Stage B/C) ---------------------
    dem = ee.ImageCollection("COPERNICUS/DEM/GLO30_2024_1").select("DEM").mosaic()
    slope_deg = ee.Terrain.slope(dem)
    gsw_occurrence = ee.Image("JRC/GSW1_4/GlobalSurfaceWater").select("occurrence")

    pre_smooth = pre_img.focal_median(50, "circle", "meters")
    post_smooth = post_img.focal_median(50, "circle", "meters")
    diff_db = post_smooth.subtract(pre_smooth).rename("diff_db")

    flood_candidate = diff_db.lte(BACKSCATTER_DROP_THRESHOLD_DB)
    permanent_water = gsw_occurrence.gt(PERMANENT_WATER_OCCURRENCE_PCT).unmask(0)
    steep_terrain = slope_deg.gt(SLOPE_PLAUSIBILITY_DEG).unmask(0)
    plausible_flood = flood_candidate.And(permanent_water.Not()).And(steep_terrain.Not()).rename("flood_mask")

    region = aoi.bounds()
    diff_vis = diff_db.visualize(min=-8, max=2, palette=["000033", "3366cc", "99ccff", "ffffff"])
    water_vis = permanent_water.selfMask().visualize(palette=["00ffff"])
    mask_vis = plausible_flood.selfMask().visualize(palette=["ff0000"])
    composite = ee.ImageCollection([diff_vis, water_vis, mask_vis]).mosaic()
    thumb_params = {"region": region, "dimensions": 768, "format": "png"}
    url = composite.getThumbURL(thumb_params)

    import urllib.request
    png_path = os.path.join(OUT_DIR, "OndoTown_20241004_flood_mask_review.png")
    urllib.request.urlretrieve(url, png_path)
    print(f"\nRendered review PNG: {png_path}")

    stats = ee.Dictionary({
        "plausible_flood_pct": plausible_flood.reduceRegion(ee.Reducer.mean(), aoi, scale=20, maxPixels=1e9, bestEffort=True).get("flood_mask"),
        "permanent_water_pct": permanent_water.reduceRegion(ee.Reducer.mean(), aoi, scale=30, maxPixels=1e9, bestEffort=True).get("occurrence"),
    }).getInfo()
    plausible_pct = (stats.get("plausible_flood_pct") or 0) * 100
    water_pct = (stats.get("permanent_water_pct") or 0) * 100
    print(f"Plausible flood: {plausible_pct:.1f}% of AOI | Permanent water (JRC GSW): {water_pct:.1f}% of AOI")

    import json
    manifest = {
        "location": "Ondo Town, Ondo State, Nigeria", "lat": LAT, "lon": LON,
        "event_date": EVENT_DATE, "mechanism": "pluvial/drainage (NEMA DG + eyewitness/Businessday sourced)",
        "pre_scene": pre_meta.get("system:index"), "pre_date": pre_date, "orbit": pre_orbit,
        "post_scene": post_meta.get("system:index"), "post_date": post_date,
        "days_post_scene_after_event": days_after_event,
        "plausible_flood_pct": round(plausible_pct, 2), "permanent_water_pct": round(water_pct, 2),
        "png_review_path": png_path,
        "note": "AREA PERCENTAGE ALONE IS NOT A CONFIDENCE DETERMINATION - see PNG for visual review "
                "against named affected streets before any mask_confidence is assigned.",
    }
    with open(os.path.join(OUT_DIR, "ondo_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"Manifest written to {os.path.join(OUT_DIR, 'ondo_manifest.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
