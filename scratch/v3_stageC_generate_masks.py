"""Pluvial V3 R&D - STAGE C, step 1: generate the observed flood masks that will become training
labels, and render each as a PNG for genuine visual review (not just a summary statistic) before
any point is sampled from it. NO production code touched.

Three events, ALL same-orbit (the different-orbit Maiduguri pair from Stage B was re-run here with
a same-orbit post-image found on retry - the only Maiduguri result usable as a training label is
THIS one, not the Stage B different-orbit result):
  1. Lekki, Lagos - 2024-07-04 (pluvial/urban drainage). Pre 2024-07-03, post 2024-07-15, orbit 95.
  2. Abuja/FCT - 2024-07-04 (urban rainfall, city-wide report). Pre 2024-06-28, post 2024-07-10,
     orbit 30.
  3. Maiduguri, Borno - 2024-09-10 dam breach (dam-related/fluvial, NOT pluvial - included for city
     diversity and cross-checked against UNOSAT's independent published assessment). Pre 2024-09-05,
     post 2024-09-17, orbit 161 (same-orbit pair confirmed live before this script was written).

Methodology identical to Stage B (backscatter drop <= -3dB, JRC GSW permanent-water exclusion,
GLO-30 slope>5deg implausibility exclusion) - not re-derived, just re-run with the confirmed
same-orbit Maiduguri pair and PNG rendering added.

Run: python scratch/v3_stageC_generate_masks.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath("."))
from dotenv import load_dotenv
load_dotenv()

import ee
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from app.utils.gee_client import init_gee

BACKSCATTER_DROP_THRESHOLD_DB = -3.0
PERMANENT_WATER_OCCURRENCE_PCT = 50.0
SLOPE_PLAUSIBILITY_DEG = 5.0
AOI_RADIUS_M = 3000
OUT_DIR = os.path.join(os.path.dirname(__file__), "v3_stageC_masks")

EVENTS = [
    {"city": "Lekki", "lat": 6.4650, "lon": 3.5658, "event_date": "2024-07-04",
     "pre_start": "2024-06-04", "pre_end": "2024-07-04", "post_start": "2024-07-04", "post_end": "2024-07-19",
     "mechanism": "pluvial/urban drainage"},
    {"city": "Abuja_FCT", "lat": 9.0765, "lon": 7.4898, "event_date": "2024-07-04",
     "pre_start": "2024-06-04", "pre_end": "2024-07-04", "post_start": "2024-07-04", "post_end": "2024-07-19",
     "mechanism": "urban rainfall (city-wide report)"},
    {"city": "Maiduguri", "lat": 11.8333, "lon": 13.1500, "event_date": "2024-09-10",
     "pre_start": "2024-08-11", "pre_end": "2024-09-10", "post_start": "2024-09-10", "post_end": "2024-09-25",
     "mechanism": "dam-related/fluvial (NOT pluvial - city-diversity + UNOSAT cross-check only)"},
]


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
    dem = ee.ImageCollection("COPERNICUS/DEM/GLO30_2024_1").select("DEM").mosaic()
    slope_deg = ee.Terrain.slope(dem)
    gsw_occurrence = ee.Image("JRC/GSW1_4/GlobalSurfaceWater").select("occurrence")

    manifest = []
    for ev in EVENTS:
        city, lat, lon = ev["city"], ev["lat"], ev["lon"]
        print(f"\n=== {city} ({ev['event_date']}) - {ev['mechanism']} ===", flush=True)
        aoi = ee.Geometry.Point([lon, lat]).buffer(AOI_RADIUS_M)
        s1 = _s1_collection(aoi)

        pre_col = s1.filterDate(ev["pre_start"], ev["pre_end"]).sort("system:time_start", False)
        post_col = s1.filterDate(ev["post_start"], ev["post_end"]).sort("system:time_start", True)
        pre_img = ee.Image(pre_col.first())
        pre_meta = pre_img.toDictionary(["system:index", "system:time_start", "relativeOrbitNumber_start"]).getInfo()
        pre_orbit = pre_meta.get("relativeOrbitNumber_start")

        post_same_orbit = post_col.filter(ee.Filter.eq("relativeOrbitNumber_start", pre_orbit)).sort("system:time_start", True)
        post_img = ee.Image(post_same_orbit.first())
        post_meta = post_img.toDictionary(["system:index", "system:time_start", "relativeOrbitNumber_start"]).getInfo()

        pre_date = ee.Date(pre_meta["system:time_start"]).format("YYYY-MM-dd").getInfo()
        post_date = ee.Date(post_meta["system:time_start"]).format("YYYY-MM-dd").getInfo()
        print(f"  PRE  {pre_meta.get('system:index')} orbit={pre_orbit} {pre_date}", flush=True)
        print(f"  POST {post_meta.get('system:index')} orbit={post_meta.get('relativeOrbitNumber_start')} {post_date}", flush=True)

        pre_smooth = pre_img.focal_median(50, "circle", "meters")
        post_smooth = post_img.focal_median(50, "circle", "meters")
        diff_db = post_smooth.subtract(pre_smooth).rename("diff_db")

        flood_candidate = diff_db.lte(BACKSCATTER_DROP_THRESHOLD_DB)
        permanent_water = gsw_occurrence.gt(PERMANENT_WATER_OCCURRENCE_PCT).unmask(0)
        steep_terrain = slope_deg.gt(SLOPE_PLAUSIBILITY_DEG).unmask(0)
        plausible_flood = flood_candidate.And(permanent_water.Not()).And(steep_terrain.Not()).rename("flood_mask")

        # --- Render a real PNG for visual review: diff_db backdrop, permanent water, plausible mask.
        region = aoi.bounds()
        scale_m = 20
        thumb_params = {"region": region, "dimensions": 512, "format": "png"}

        diff_vis = diff_db.visualize(min=-8, max=2, palette=["000033", "3366cc", "99ccff", "ffffff"])
        water_vis = permanent_water.selfMask().visualize(palette=["00ffff"])
        mask_vis = plausible_flood.selfMask().visualize(palette=["ff0000"])
        composite = ee.ImageCollection([diff_vis, water_vis, mask_vis]).mosaic()
        url = composite.getThumbURL(thumb_params)

        import urllib.request
        png_path = os.path.join(OUT_DIR, f"{city}_flood_mask_review.png")
        urllib.request.urlretrieve(url, png_path)
        print(f"  Rendered review PNG: {png_path}", flush=True)

        stats = ee.Dictionary({
            "plausible_flood_pct": plausible_flood.reduceRegion(ee.Reducer.mean(), aoi, scale=scale_m, maxPixels=1e9, bestEffort=True).get("flood_mask"),
            "permanent_water_pct": permanent_water.reduceRegion(ee.Reducer.mean(), aoi, scale=30, maxPixels=1e9, bestEffort=True).get("occurrence"),
        }).getInfo()
        plausible_pct = (stats.get("plausible_flood_pct") or 0) * 100
        water_pct = (stats.get("permanent_water_pct") or 0) * 100
        print(f"  Plausible flood: {plausible_pct:.1f}% of AOI | Permanent water: {water_pct:.1f}% of AOI", flush=True)

        manifest.append({
            "city": city, "lat": lat, "lon": lon, "event_date": ev["event_date"], "mechanism": ev["mechanism"],
            "pre_scene": pre_meta.get("system:index"), "pre_date": pre_date, "orbit": pre_orbit,
            "post_scene": post_meta.get("system:index"), "post_date": post_date,
            "plausible_flood_pct": round(plausible_pct, 2), "permanent_water_pct": round(water_pct, 2),
            "png_review_path": png_path,
        })

    import json
    manifest_path = os.path.join(OUT_DIR, "mask_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"\nManifest written to {manifest_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
