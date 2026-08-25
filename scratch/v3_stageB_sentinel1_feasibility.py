"""Pluvial V3 R&D - STAGE B: ground-truth feasibility via Sentinel-1 SAR change detection. NO
production code touched, NO score proposed - this tests whether a credible OBSERVED flood mask can
be produced at all for documented Nigerian urban flood events, as a prerequisite for Stage C
(which is NOT authorized yet).

Three events, chosen for exact/near-exact documented dates (Sentinel-1 has operated since 2014, so
only post-2014 events are usable) and geographic/mechanism diversity:
  1. Lekki, Lagos - 4 July 2024 (Wikipedia "2024 Lekki flood" - explicit 10-hour rainfall event,
     explicitly attributed to a blocked drainage system ("System 156") - the cleanest documented
     pure-pluvial/urban-drainage event found this session, with an exact date.
  2. Abuja/FCT - 4 July 2024 (same national heavy-rainfall event per government/news reporting -
     "10 states plus the FCT" affected) - same date, different city, tests geographic transfer.
  3. Maiduguri, Borno - 10 September 2024 (Alau Dam breach) - dam-related/fluvial, NOT pluvial, but
     already has an independent published UNOSAT preliminary satellite flood assessment (found via
     WebSearch this session) - included specifically as a cross-check: if this script's own
     methodology can reproduce a plausible flood signal at a location UNOSAT independently already
     mapped, that's real evidence the methodology itself is sound, separate from whether any given
     result is pluvial-relevant.

Methodology (change detection, not a trained classifier):
  - Sentinel-1 GRD, IW mode, VV polarization (COPERNICUS/S1_GRD, free/open/commercial-clear per
    Copernicus Sentinel Data licence, no restriction).
  - Pre-event: latest available image in the 30 days before the event date.
  - Post-event: earliest available image in the 0-15 days after the event date.
  - Flood candidate = post_VV - pre_VV <= -3 dB (a standard SAR flood-detection threshold - water's
    specular reflection causes a sharp backscatter drop versus the pre-event baseline).
  - Permanent water excluded via JRC Global Surface Water (JRC_GSW1_4_GlobalSurfaceWater, occurrence
    band > 50% masked out) - free/open/commercial-clear, attribution "Source: EC JRC/Google".
  - Terrain plausibility mask: excludes slope > 5 degrees (Copernicus GLO-30) - steep-slope SAR
    "flood" signals are typically layover/shadow radiometric artifacts, not real inundation.

Every image ID, acquisition date, and orbit number used is printed and preserved below - a
permanent methodology record, not a throwaway result.

Run: python scratch/v3_stageB_sentinel1_feasibility.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath("."))
from dotenv import load_dotenv
load_dotenv()

import ee
from app.utils.gee_client import init_gee

BACKSCATTER_DROP_THRESHOLD_DB = -3.0
PERMANENT_WATER_OCCURRENCE_PCT = 50.0
SLOPE_PLAUSIBILITY_DEG = 5.0
AOI_RADIUS_M = 3000

EVENTS = [
    {"name": "Lekki, Lagos", "lat": 6.4650, "lon": 3.5658, "event_date": "2024-07-04",
     "mechanism": "pluvial/urban drainage (high confidence - explicit source)"},
    {"name": "Abuja/FCT", "lat": 9.0765, "lon": 7.4898, "event_date": "2024-07-04",
     "mechanism": "urban rainfall (moderate confidence - city-wide report, not site-specific)"},
    {"name": "Maiduguri, Borno", "lat": 11.8333, "lon": 13.1500, "event_date": "2024-09-10",
     "mechanism": "dam-related/fluvial (NOT pluvial - included as an independent-reference cross-check; UNOSAT published its own preliminary satellite flood assessment for this exact event)"},
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
    init_gee()
    dem = ee.ImageCollection("COPERNICUS/DEM/GLO30_2024_1").select("DEM").mosaic()
    slope_deg = ee.Terrain.slope(dem)
    gsw_occurrence = ee.Image("JRC/GSW1_4/GlobalSurfaceWater").select("occurrence")

    for ev in EVENTS:
        name, lat, lon, event_date = ev["name"], ev["lat"], ev["lon"], ev["event_date"]
        print(f"\n=== {name} ({event_date}) - {ev['mechanism']} ===", flush=True)
        aoi = ee.Geometry.Point([lon, lat]).buffer(AOI_RADIUS_M)

        event_dt = ee.Date(event_date)
        pre_start = event_dt.advance(-30, "day")
        pre_end = event_dt
        post_start = event_dt
        post_end = event_dt.advance(15, "day")

        s1 = _s1_collection(aoi)
        pre_col = s1.filterDate(pre_start, pre_end).sort("system:time_start", False)
        post_col = s1.filterDate(post_start, post_end).sort("system:time_start", True)

        pre_count = pre_col.size().getInfo()
        post_count = post_col.size().getInfo()
        print(f"  Pre-event images available (30d before): {pre_count}", flush=True)
        print(f"  Post-event images available (0-15d after): {post_count}", flush=True)
        if pre_count == 0 or post_count == 0:
            print("  [INFEASIBLE] No Sentinel-1 coverage in the required window at this AOI.", flush=True)
            continue

        pre_img = ee.Image(pre_col.first())
        post_img = ee.Image(post_col.first())
        pre_meta = pre_img.toDictionary(["system:index", "system:time_start", "relativeOrbitNumber_start"]).getInfo()
        post_meta = post_img.toDictionary(["system:index", "system:time_start", "relativeOrbitNumber_start"]).getInfo()
        print(f"  PRE  image: {pre_meta.get('system:index')} orbit={pre_meta.get('relativeOrbitNumber_start')} time={ee.Date(pre_meta.get('system:time_start')).format('YYYY-MM-dd').getInfo()}", flush=True)
        print(f"  POST image: {post_meta.get('system:index')} orbit={post_meta.get('relativeOrbitNumber_start')} time={ee.Date(post_meta.get('system:time_start')).format('YYYY-MM-dd').getInfo()}", flush=True)
        same_orbit = pre_meta.get("relativeOrbitNumber_start") == post_meta.get("relativeOrbitNumber_start")
        print(f"  Same relative orbit (consistent look geometry): {same_orbit}", flush=True)

        # Smoothing (median filter) before differencing - standard SAR speckle-noise practice, not
        # a scoring choice.
        pre_smooth = pre_img.focal_median(50, "circle", "meters")
        post_smooth = post_img.focal_median(50, "circle", "meters")
        diff_db = post_smooth.subtract(pre_smooth).rename("diff_db")

        flood_candidate = diff_db.lte(BACKSCATTER_DROP_THRESHOLD_DB)
        permanent_water = gsw_occurrence.gt(PERMANENT_WATER_OCCURRENCE_PCT).unmask(0)
        steep_terrain = slope_deg.gt(SLOPE_PLAUSIBILITY_DEG).unmask(0)
        plausible_flood = flood_candidate.And(permanent_water.Not()).And(steep_terrain.Not())

        stats = ee.Dictionary({
            "raw_candidate_pct": flood_candidate.reduceRegion(ee.Reducer.mean(), aoi, scale=20, maxPixels=1e9, bestEffort=True).get("diff_db"),
            "permanent_water_pct": permanent_water.reduceRegion(ee.Reducer.mean(), aoi, scale=30, maxPixels=1e9, bestEffort=True).get("occurrence"),
            "plausible_flood_pct": plausible_flood.reduceRegion(ee.Reducer.mean(), aoi, scale=20, maxPixels=1e9, bestEffort=True).get("diff_db"),
        }).getInfo()
        raw_pct = (stats.get("raw_candidate_pct") or 0) * 100
        water_pct = (stats.get("permanent_water_pct") or 0) * 100
        plausible_pct = (stats.get("plausible_flood_pct") or 0) * 100
        print(f"  Raw backscatter-drop candidate area: {raw_pct:.1f}% of {AOI_RADIUS_M}m-radius AOI", flush=True)
        print(f"  Permanent water (JRC GSW) within AOI: {water_pct:.1f}%", flush=True)
        print(f"  PLAUSIBLE flood mask (water+terrain masked): {plausible_pct:.1f}% of AOI", flush=True)
        credible = 0.5 < plausible_pct < 60.0
        print(f"  Credibility check (0.5%-60% of AOI, i.e. neither near-zero noise nor implausibly total): {'PLAUSIBLE' if credible else 'QUESTIONABLE - needs visual inspection, not auto-trusted'}", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
