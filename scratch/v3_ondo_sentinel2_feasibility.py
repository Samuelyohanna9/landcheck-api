"""Pluvial V3 R&D - Ondo Town fallback check 1: Sentinel-2 optical feasibility. NO production code
touched, NO model trained, NO change to V3.1. Per explicit instruction: don't just ask whether an
image exists near 4-6 Oct 2024 - check the ACTUAL local cloud fraction over the Ondo Town AOI
(not just the whole-scene CLOUDY_PIXEL_PERCENTAGE metadata, which can be misleading if clouds sit
elsewhere in the scene), and only if a usably clear near-event scene exists, compute NDWI/MNDWI
against a clear pre-event reference - purely observational, no tuning to match the news reports.

Run: python scratch/v3_ondo_sentinel2_feasibility.py
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
AOI_RADIUS_M = 4000
OUT_DIR = os.path.join(os.path.dirname(__file__), "v3_ondo_masks")


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    init_gee()
    aoi = ee.Geometry.Point([LON, LAT]).buffer(AOI_RADIUS_M)
    event_dt = ee.Date(EVENT_DATE)

    s2 = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(aoi)
        .filterDate(event_dt.advance(-45, "day"), event_dt.advance(30, "day"))
        .sort("system:time_start")
    )
    n = s2.size().getInfo()
    print(f"Sentinel-2 SR scenes over Ondo Town, {EVENT_DATE} +/-45/30 days: {n}\n")
    if n == 0:
        print("[INFEASIBLE] No Sentinel-2 scenes at all in this window.")
        return 0

    info_list = s2.toList(n)
    scl = None
    scenes = []
    for i in range(n):
        img = ee.Image(info_list.get(i))
        meta = img.toDictionary(["system:index", "system:time_start", "CLOUDY_PIXEL_PERCENTAGE"]).getInfo()
        date_str = ee.Date(meta["system:time_start"]).format("YYYY-MM-dd").getInfo()
        days_from_event = ee.Date(meta["system:time_start"]).difference(event_dt, "day").getInfo()

        # Actual local cloud fraction over the AOI via the Scene Classification Layer (SCL):
        # classes 8/9/10 = cloud medium prob / cloud high prob / thin cirrus. This is the real
        # metric, not the whole-tile CLOUDY_PIXEL_PERCENTAGE metadata.
        scl_band = img.select("SCL")
        cloud_mask = scl_band.eq(8).Or(scl_band.eq(9)).Or(scl_band.eq(10))
        local_cloud_frac = cloud_mask.reduceRegion(
            ee.Reducer.mean(), aoi, scale=20, maxPixels=1e9, bestEffort=True
        ).get("SCL").getInfo()
        local_cloud_pct = (local_cloud_frac or 0) * 100

        scenes.append({
            "index": meta.get("system:index"), "date": date_str,
            "days_from_event": days_from_event,
            "tile_cloudy_pct": meta.get("CLOUDY_PIXEL_PERCENTAGE"),
            "aoi_local_cloud_pct": round(local_cloud_pct, 1),
        })
        print(f"  {meta.get('system:index'):50s} date={date_str}  days_from_event={days_from_event:+6.1f}  "
              f"tile_cloud%={meta.get('CLOUDY_PIXEL_PERCENTAGE'):.1f}  AOI_local_cloud%={local_cloud_pct:.1f}")

    import json
    with open(os.path.join(OUT_DIR, "ondo_s2_scene_list.json"), "w", encoding="utf-8") as f:
        json.dump(scenes, f, indent=2)

    # --- Identify the best near-event scene (closest to event date with AOI-local cloud < 20%) ----
    near_event_candidates = [s for s in scenes if s["days_from_event"] >= -1 and s["aoi_local_cloud_pct"] < 20]
    if not near_event_candidates:
        print("\n[INFEASIBLE] No Sentinel-2 scene within the post-event window has AOI-local cloud "
              "cover < 20%. Nigeria's October wet season means persistent cloud cover exactly when "
              "we need it - this is a real, reportable limitation, not a bug.")
        # still show what WAS closest, for transparency
        post_event = sorted([s for s in scenes if s["days_from_event"] >= -1], key=lambda s: s["days_from_event"])
        if post_event:
            print(f"  Closest post-event scene: {post_event[0]['date']} "
                  f"({post_event[0]['days_from_event']:+.1f}d), AOI-local cloud={post_event[0]['aoi_local_cloud_pct']:.1f}%")
        return 0

    best_post = min(near_event_candidates, key=lambda s: s["days_from_event"])
    print(f"\nBest near-event clear scene: {best_post['date']} ({best_post['days_from_event']:+.1f}d), "
          f"AOI-local cloud={best_post['aoi_local_cloud_pct']:.1f}%")

    # --- Best pre-event clear reference (closest scene before the event with low local cloud) -----
    pre_event_candidates = [s for s in scenes if s["days_from_event"] < -1 and s["aoi_local_cloud_pct"] < 20]
    if not pre_event_candidates:
        print("[INFEASIBLE] No usably clear PRE-event reference scene found either - cannot compute a change metric.")
        return 0
    best_pre = max(pre_event_candidates, key=lambda s: s["days_from_event"])
    print(f"Best pre-event clear reference: {best_pre['date']} ({best_pre['days_from_event']:+.1f}d), "
          f"AOI-local cloud={best_pre['aoi_local_cloud_pct']:.1f}%")

    # --- Observational NDWI/MNDWI change (no tuning, no threshold search) --------------------------
    def _get_image(index):
        return ee.Image(f"COPERNICUS/S2_SR_HARMONIZED/{index}")

    pre_img = _get_image(best_pre["index"])
    post_img = _get_image(best_post["index"])

    def _mndwi(img):
        return img.normalizedDifference(["B3", "B11"]).rename("mndwi")  # Green, SWIR1

    def _ndwi(img):
        return img.normalizedDifference(["B3", "B8"]).rename("ndwi")  # Green, NIR

    pre_mndwi = _mndwi(pre_img)
    post_mndwi = _mndwi(post_img)
    mndwi_diff = post_mndwi.subtract(pre_mndwi).rename("mndwi_diff")

    region = aoi.bounds()
    diff_vis = mndwi_diff.visualize(min=-0.5, max=0.5, palette=["8B0000", "ffffff", "00008B"])
    thumb_params = {"region": region, "dimensions": 768, "format": "png"}
    url = diff_vis.getThumbURL(thumb_params)
    import urllib.request
    png_path = os.path.join(OUT_DIR, "OndoTown_S2_MNDWI_diff_review.png")
    urllib.request.urlretrieve(url, png_path)
    print(f"\nMNDWI-difference review PNG (dark red = new water gain post-event, per this pure diff, "
          f"no thresholding applied): {png_path}")

    # Also render true-color pre/post for visual sanity-check
    for label, img in (("pre", pre_img), ("post", post_img)):
        tc = img.visualize(bands=["B4", "B3", "B2"], min=0, max=2500)
        url_tc = tc.getThumbURL(thumb_params)
        path_tc = os.path.join(OUT_DIR, f"OndoTown_S2_{label}_truecolor.png")
        urllib.request.urlretrieve(url_tc, path_tc)
        print(f"True-color {label} PNG: {path_tc}")

    stats = mndwi_diff.gt(0.15).rename("gain").reduceRegion(
        ee.Reducer.mean(), aoi, scale=20, maxPixels=1e9, bestEffort=True
    ).get("gain").getInfo()
    print(f"\nFraction of AOI with MNDWI increase > 0.15 (observational only, not a validated "
          f"flood-detection threshold): {(stats or 0)*100:.1f}%")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
