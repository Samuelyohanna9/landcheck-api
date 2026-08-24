"""V3 DIAGNOSTIC (not blind validation) - physical investigation of the Phase 3 finding that HAND
gives near-identical readings to genuine fluvial locations and false-positive dry/highland
controls (Phase 3 floodplain AUC 0.550, specificity 0.000).

Reuses the 20 Phase 3 locations - now explicitly DEVELOPMENT/DIAGNOSTIC DATA, never to be reused
as blind validation for V3, per the explicit instruction that follows a failed blind validation.

Extracts, for every location, four MERIT Hydro signals over the same 300m local area
compute_floodplain_risk() already uses: HAND (hnd), upstream contributing area (upa, km2), channel
width (wth, m - 0 or very small where MERIT's routed network doesn't resolve a channel at all),
and the SAME capped (2000m) distance-to-mapped-river-channel signal the frozen engine already
computes but never scores. Does not modify hazard_floodplain.py or any production file - read-only
diagnostic queries mirroring its existing formulas exactly.

Run: python scratch/v3_diagnostic_hand_false_positives.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath("."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv()

import ee
from app.utils.gee_client import init_gee
from phase3_locations import LOCATIONS

LOCAL_AREA_RADIUS_M = 300
RIVER_DIST_CAP_M = 2000.0


def main() -> int:
    init_gee()
    merit = ee.Image("MERIT/Hydro/v1_0_1")
    hand_img = merit.select("hnd")
    upa_img = merit.select("upa")
    wth_img = merit.select("wth")
    wat_img = merit.select("wat")
    river_mask = wat_img.gt(0)
    river_dist_img = (
        river_mask.fastDistanceTransform(30).sqrt().multiply(wat_img.projection().nominalScale()).rename("river_dist_m")
    )

    pct_reducer = ee.Reducer.median().combine(ee.Reducer.max(), sharedInputs=True)

    rows = []
    for loc in LOCATIONS:
        name, lat, lon, group = loc["name"], loc["lat"], loc["lon"], loc["group"]
        local_area = ee.Geometry.Point([lon, lat]).buffer(LOCAL_AREA_RADIUS_M)
        try:
            hand_stats = hand_img.reduceRegion(pct_reducer, local_area, scale=90, maxPixels=1e9, bestEffort=True).getInfo()
            other = ee.Dictionary({
                "upa_km2_max": upa_img.reduceRegion(ee.Reducer.max(), local_area, scale=90, maxPixels=1e9, bestEffort=True).get("upa"),
                "upa_km2_median": upa_img.reduceRegion(ee.Reducer.median(), local_area, scale=90, maxPixels=1e9, bestEffort=True).get("upa"),
                "wth_m_max": wth_img.reduceRegion(ee.Reducer.max(), local_area, scale=90, maxPixels=1e9, bestEffort=True).get("wth"),
                "river_dist_m": river_dist_img.reduceRegion(ee.Reducer.mean(), local_area, scale=90, maxPixels=1e9, bestEffort=True).get("river_dist_m"),
            }).getInfo()
        except Exception as exc:
            print(f"[ERROR] {name}: {exc!r}", flush=True)
            continue

        raw_dist = other.get("river_dist_m")
        capped_dist = min(float(raw_dist), RIVER_DIST_CAP_M) if raw_dist is not None else RIVER_DIST_CAP_M
        row = {
            "name": name, "group": group,
            "hand_median": hand_stats.get("hnd_median"), "hand_max": hand_stats.get("hnd_max"),
            "upa_km2_max": other.get("upa_km2_max"), "upa_km2_median": other.get("upa_km2_median"),
            "wth_m_max": other.get("wth_m_max"), "river_dist_m_capped": round(capped_dist, 1),
        }
        rows.append(row)
        print(
            f"{name} [{group}]: HAND_median={row['hand_median']}m | upa_max={row['upa_km2_max']}km2 | "
            f"wth_max={row['wth_m_max']}m | river_dist_capped={row['river_dist_m_capped']}m",
            flush=True,
        )

    print("\n=== GROUPED SUMMARY (sites with HAND_median < 20m only - the 'low HAND' subset) ===", flush=True)
    low_hand = [r for r in rows if r["hand_median"] is not None and r["hand_median"] < 20]
    print(f"{len(low_hand)} of {len(rows)} locations have HAND_median < 20m\n", flush=True)
    for r in sorted(low_hand, key=lambda x: x["hand_median"]):
        print(
            f"  [{r['group']:8s}] {r['name']:36s} HAND={r['hand_median']:5.1f}m | "
            f"upa_max={r['upa_km2_max'] if r['upa_km2_max'] is not None else 'None':>10} km2 | "
            f"wth_max={r['wth_m_max'] if r['wth_m_max'] is not None else 'None':>6} m | "
            f"river_dist={r['river_dist_m_capped']:6.1f}m",
            flush=True,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
