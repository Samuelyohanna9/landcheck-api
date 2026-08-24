"""Gate check before finalizing the pluvial double-count fix (V2 plan, Section 2).

DIAGNOSTIC ONLY. No production code touched.

User's explicit instruction: before retaining 300m neighbourhood-scale imperviousness as an
independently-weighted 30% component, compute its correlation with parcel-scale imperviousness
across the 35 real-building diagnostic parcels. If still highly saturated/correlated, STOP and
report instead of proceeding - and do not pick a new weight based on the flood/control labels.

Parcel-scale (30m radius) values are hardcoded below, copied verbatim from the completed
test_impervious_buffer_scale.py run earlier this session (34 of 35 parcels read exactly 100.0%,
one at 98.3%). This script computes the missing exact-300m reading for the same 35 parcels (the
prior run tested 250m and 400m, not 300m specifically) and reports both the correlation and a
plain-language breakdown of how many parcels actually diverge.

Run: python scratch/test_impervious_scale_correlation.py
"""
import math
import os
import sys

sys.path.insert(0, os.path.abspath("."))
from dotenv import load_dotenv
load_dotenv()

import ee

from app.utils.gee_client import init_gee
from app.utils.hazard_lulc import LULC_ASSETS, _is_known_class_code, _load_landcover_source_image

# (location, group, lat, lon, parcel_scale_30m_pct) - the last column copied verbatim from the
# completed test_impervious_buffer_scale.py run.
PARCELS = [
    ("Ogunpa (Ibadan)", "flooded", 7.37720, 3.88912, 100.0),
    ("Ogunpa (Ibadan)", "flooded", 7.37769, 3.89013, 100.0),
    ("Ogunpa (Ibadan)", "flooded", 7.37718, 3.88698, 100.0),
    ("Ogunpa (Ibadan)", "flooded", 7.37925, 3.88601, 100.0),
    ("Ogunpa (Ibadan)", "flooded", 7.37835, 3.88768, 100.0),
    ("Bodija GRA (Ibadan)", "control", 7.42479, 3.90894, 100.0),
    ("Bodija GRA (Ibadan)", "control", 7.42714, 3.90553, 100.0),
    ("Bodija GRA (Ibadan)", "control", 7.42660, 3.90940, 100.0),
    ("Bodija GRA (Ibadan)", "control", 7.42803, 3.90738, 100.0),
    ("Bodija GRA (Ibadan)", "control", 7.42901, 3.90821, 100.0),
    ("Makurdi", "flooded", 7.73417, 8.54071, 100.0),
    ("Makurdi", "flooded", 7.73395, 8.54082, 100.0),
    ("Makurdi", "flooded", 7.73076, 8.53926, 100.0),
    ("Makurdi", "flooded", 7.72984, 8.54004, 100.0),
    ("Makurdi", "flooded", 7.72979, 8.53987, 100.0),
    ("Abuja Asokoro/Maitama", "control", 9.04828, 7.52819, 100.0),
    ("Abuja Asokoro/Maitama", "control", 9.04881, 7.52974, 100.0),
    ("Abuja Asokoro/Maitama", "control", 9.04535, 7.52837, 100.0),
    ("Abuja Asokoro/Maitama", "control", 9.04605, 7.52891, 100.0),
    ("Abuja Asokoro/Maitama", "control", 9.04822, 7.52785, 100.0),
    ("Ogbaru", "flooded", 6.16127, 6.75475, 98.3),
    ("Ogbaru", "flooded", 6.16087, 6.74740, 100.0),
    ("Ogbaru", "flooded", 6.16070, 6.74596, 100.0),
    ("Ogbaru", "flooded", 6.16702, 6.74283, 100.0),
    ("Ogbaru", "flooded", 6.16820, 6.74516, 100.0),
    ("Lokoja", "flooded", 7.80008, 6.73485, 100.0),
    ("Lokoja", "flooded", 7.80148, 6.73494, 100.0),
    ("Lokoja", "flooded", 7.80301, 6.73456, 100.0),
    ("Lokoja", "flooded", 7.79972, 6.73410, 100.0),
    ("Lokoja", "flooded", 7.80276, 6.73150, 100.0),
    ("Jos", "control", 9.89665, 8.85903, 100.0),
    ("Jos", "control", 9.89812, 8.85812, 100.0),
    ("Jos", "control", 9.89641, 8.85844, 100.0),
    ("Jos", "control", 9.89803, 8.86001, 100.0),
    ("Jos", "control", 9.89834, 8.85875, 100.0),
]


def _fraction_at_scale(image, class_colors, geom):
    hist = image.reduceRegion(
        reducer=ee.Reducer.frequencyHistogram(), geometry=geom, scale=10, maxPixels=int(1e9), bestEffort=True,
    ).get("landcover")
    raw_hist = hist.getInfo() or {}
    raw_pixels = sum(float(v) for v in raw_hist.values())
    valid_hist = {k: v for k, v in raw_hist.items() if _is_known_class_code(k, class_colors)}
    valid_pixels = sum(float(v) for v in valid_hist.values())
    if not valid_hist or (raw_pixels and valid_pixels / raw_pixels < 0.5):
        return None
    built_pixels = sum(float(v) for k, v in valid_hist.items() if int(float(k)) == 7)
    return min(1.0, built_pixels / valid_pixels)


def _resolve_asset(base_geom):
    for asset_id, class_colors in LULC_ASSETS:
        try:
            candidate_image = _load_landcover_source_image(asset_id)
            frac = _fraction_at_scale(candidate_image, class_colors, base_geom)
        except Exception:
            continue
        if frac is not None:
            return candidate_image, class_colors, frac
    return None, None, None


def _pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return float("nan")
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    denom = math.sqrt(vx * vy)
    return cov / denom if denom else float("nan")


def main() -> int:
    init_gee()
    pairs = []
    for loc, group, lat, lon, parcel_30m_pct in PARCELS:
        geom_300 = ee.Geometry.Point([lon, lat]).buffer(300)
        image, class_colors, _ = _resolve_asset(ee.Geometry.Point([lon, lat]).buffer(30))
        if image is None:
            print(f"[WARN] {loc} ({lat:.5f},{lon:.5f}): no LULC asset resolved - skipping", flush=True)
            continue
        try:
            frac_300 = _fraction_at_scale(image, class_colors, geom_300)
        except Exception as exc:
            print(f"[ERROR] {loc} ({lat:.5f},{lon:.5f}): {exc!r}", flush=True)
            continue
        pct_300 = round(frac_300 * 100, 1) if frac_300 is not None else None
        diff = round(parcel_30m_pct - pct_300, 1) if pct_300 is not None else None
        pairs.append((loc, group, parcel_30m_pct, pct_300, diff))
        print(f"{loc} [{group}] ({lat:.5f},{lon:.5f}): 30m={parcel_30m_pct}% | 300m={pct_300}% | diff={diff}pp", flush=True)

    if not pairs:
        print("No parcels computed.", flush=True)
        return 1

    xs = [p[2] for p in pairs]
    ys = [p[3] for p in pairs if p[3] is not None]
    xs_paired = [p[2] for p in pairs if p[3] is not None]
    r = _pearson(xs_paired, ys)

    big_divergence = [p for p in pairs if p[4] is not None and abs(p[4]) > 10]
    small_divergence = [p for p in pairs if p[4] is not None and abs(p[4]) <= 10]

    print(f"\n=== SUMMARY (n={len(pairs)}) ===", flush=True)
    print(f"Pearson r (30m parcel-scale vs 300m neighbourhood-scale): {r:.3f}", flush=True)
    print(f"Parcels with >10pp divergence between scales: {len(big_divergence)} of {len(pairs)}", flush=True)
    print(f"Parcels still within 10pp (effectively still saturated together): {len(small_divergence)} of {len(pairs)}", flush=True)

    print("\n=== By location (median 300m reading, for context) ===", flush=True)
    by_loc = {}
    for p in pairs:
        by_loc.setdefault(p[0], []).append(p)
    for loc, rows in by_loc.items():
        vals_300 = sorted(p[3] for p in rows if p[3] is not None)
        med = vals_300[len(vals_300) // 2] if vals_300 else None
        print(f"  {loc} [{rows[0][1]}]: 30m={rows[0][2]}% (all parcels) | 300m median={med}%", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
