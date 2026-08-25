"""Pluvial V3 R&D - Nigeria ground-truth qualification: Owerri Metro storm event (storm_event_id
NGA_2021_08_18_OwerriMetro), 17-18 August 2021. NO production code touched, NO model trained, NO
V3.1 calculation here - this generates and visually reviews the SAR change-detection masks only;
V3.1 evaluation happens in a separate step ONLY if these masks pass visual/physical review.

Event evidence (contemporary reporting, independently searched this session): prolonged rainfall
submerged major streets in Owerri metropolis around 17-18 August 2021, specifically naming Item
Street (Ikenegbu), Works Layout, Akwakuma, and Ihiagwa - four named locations within ONE storm
event, not four independent floods (kept statistically grouped under one storm_event_id per
instruction, never split into separate "events" later).

Coordinates (mixed precision, disclosed):
  - Item Street / Works Layout: Owerri Municipal centre proxy (5.4850, 7.0350) - Ikenegbu-area
    street-level geocoding was not available via accessible sources; both are central Owerri
    Municipal neighbourhoods near this point.
  - Akwakuma: 5.518025, 7.019526 (precise - "Orlu Rd, Amakohia-Akwakuma" geocoded directly).
  - Ihiagwa: 5.384, 6.995 (FUTO main campus, a precise, well-known landmark within Ihiagwa).

ONE wide AOI (centred between Ihiagwa and Akwakuma, 9.5km radius) covers all four named locations
in a single mask - each location is then inspected individually within that mask, never averaged
into one arbitrary point.

TWO independent orbits generated and reported SEPARATELY, never combined into one result, per
instruction - agreement between them is the credibility signal, not an average:
  - Orbit 22 (PRIMARY): pre=2021-08-07 (-10.8d), post=2021-08-19 (+1.2d after the event)
  - Orbit 30 (ROBUSTNESS CHECK): pre=2021-08-07 (-10.3d), post=2021-08-19 (+1.7d after the event)

Same methodology as every prior mask this session (Stage C, Ondo, Lagos): VV backscatter drop
<=-3dB, JRC GSW permanent-water exclusion, GLO-30 slope>5deg implausibility exclusion, PNG rendered
for genuine visual review - area-percentage alone is explicitly not a confidence determination.

Run: python scratch/v3_owerri_metro_sentinel1_masks.py
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.abspath("."))
from dotenv import load_dotenv
load_dotenv()

import ee
from app.utils.gee_client import init_gee

OUT_DIR = os.path.join(os.path.dirname(__file__), "v3_owerri_masks")
CENTER_LAT, CENTER_LON = 5.451, 7.007  # midpoint between Ihiagwa and Akwakuma
AOI_RADIUS_M = 9500

NAMED_LOCATIONS = {
    "Item_Street_Works_Layout": (5.4850, 7.0350),
    "Akwakuma": (5.518025, 7.019526),
    "Ihiagwa_FUTO": (5.384, 6.995),
}

ORBIT_PAIRS = {
    "orbit22_PRIMARY": {
        "pre": "COPERNICUS/S1_GRD/S1A_IW_GRDH_1SDV_20210807T052200_20210807T052229_039119_049DD7_76CE",
        "post": "COPERNICUS/S1_GRD/S1A_IW_GRDH_1SDV_20210819T052200_20210819T052229_039294_04A3E0_5354",
        "pre_date": "2021-08-07", "post_date": "2021-08-19 (+1.2d after event)",
    },
    "orbit30_ROBUSTNESS_CHECK": {
        "pre": "COPERNICUS/S1_GRD/S1A_IW_GRDH_1SDV_20210807T174509_20210807T174534_039127_049E1A_8E18",
        "post": "COPERNICUS/S1_GRD/S1A_IW_GRDH_1SDV_20210819T174510_20210819T174535_039302_04A423_E1FA",
        "pre_date": "2021-08-07", "post_date": "2021-08-19 (+1.7d after event)",
    },
}

BACKSCATTER_DROP_THRESHOLD_DB = -3.0
PERMANENT_WATER_OCCURRENCE_PCT = 50.0
SLOPE_PLAUSIBILITY_DEG = 5.0


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    init_gee()
    dem = ee.ImageCollection("COPERNICUS/DEM/GLO30_2024_1").select("DEM").mosaic()
    slope_deg = ee.Terrain.slope(dem)
    gsw_occurrence = ee.Image("JRC/GSW1_4/GlobalSurfaceWater").select("occurrence")

    aoi = ee.Geometry.Point([CENTER_LON, CENTER_LAT]).buffer(AOI_RADIUS_M)
    manifest = {"storm_event_id": "NGA_2021_08_18_OwerriMetro", "named_locations": NAMED_LOCATIONS, "results": {}}

    for pair_name, pair in ORBIT_PAIRS.items():
        print(f"\n=== {pair_name}: pre={pair['pre_date']} post={pair['post_date']} ===", flush=True)
        pre_img = ee.Image(pair["pre"]).select("VV")
        post_img = ee.Image(pair["post"]).select("VV")

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
        url = composite.getThumbURL({"region": region, "dimensions": 900, "format": "png"})
        png_path = os.path.join(OUT_DIR, f"OwerriMetro_{pair_name}_review.png")
        urllib.request.urlretrieve(url, png_path)
        print(f"  Rendered: {png_path}", flush=True)

        overall_stats = ee.Dictionary({
            "plausible_flood_pct": plausible_flood.reduceRegion(ee.Reducer.mean(), aoi, scale=20, maxPixels=1e9, bestEffort=True).get("flood_mask"),
            "permanent_water_pct": permanent_water.reduceRegion(ee.Reducer.mean(), aoi, scale=30, maxPixels=1e9, bestEffort=True).get("occurrence"),
        }).getInfo()
        overall_plausible = (overall_stats.get("plausible_flood_pct") or 0) * 100
        overall_water = (overall_stats.get("permanent_water_pct") or 0) * 100
        print(f"  Whole-AOI plausible flood: {overall_plausible:.2f}% | permanent water: {overall_water:.2f}%", flush=True)

        per_location = {}
        for loc_name, (lat, lon) in NAMED_LOCATIONS.items():
            loc_aoi = ee.Geometry.Point([lon, lat]).buffer(600)  # local neighbourhood scale
            loc_stats = plausible_flood.reduceRegion(ee.Reducer.mean(), loc_aoi, scale=20, maxPixels=1e9, bestEffort=True).get("flood_mask").getInfo()
            loc_pct = (loc_stats or 0) * 100
            per_location[loc_name] = round(loc_pct, 2)
            print(f"    {loc_name}: plausible flood within 600m = {loc_pct:.2f}%", flush=True)

        manifest["results"][pair_name] = {
            "pre_scene": pair["pre"], "post_scene": pair["post"],
            "pre_date": pair["pre_date"], "post_date": pair["post_date"],
            "whole_aoi_plausible_flood_pct": round(overall_plausible, 2),
            "whole_aoi_permanent_water_pct": round(overall_water, 2),
            "per_location_pct": per_location,
            "png_review_path": png_path,
        }

    manifest_path = os.path.join(OUT_DIR, "owerri_metro_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"\nManifest written to {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
