"""Neighbourhood-scale imperviousness investigation - groundwork for the V2 architecture redesign.

DIAGNOSTIC ONLY. No production code touched.

Phase 2 (real-parcel diagnostic) confirmed 34/35 real building parcels read ~100% impervious at
Esri 10m resolution measured directly over the parcel - not a point-placement artifact, a genuine
saturation at parcel scale. The open question this script answers: does imperviousness measured
over a wider SURROUNDING buffer (the neighbourhood's actual runoff-contributing catchment, not the
one building pixel) discriminate a real flood corridor from a well-drained control at any radius?

Reuses the EXACT same 35 real building coordinates from the completed real-parcel diagnostic run
(copied verbatim from that run's output) so results are directly comparable - same buildings,
different measurement scale.

For efficiency, resolves which LULC asset/class-table succeeds ONCE per parcel (at the smallest
radius, via the actual production _fetch_impervious_fraction helper) and reuses that same image
for the larger radii at that parcel, combining all radii into one Earth Engine round trip per
parcel rather than one per radius.

Run: python scratch/test_impervious_buffer_scale.py
"""
import os
import statistics
import sys

sys.path.insert(0, os.path.abspath("."))
from dotenv import load_dotenv
load_dotenv()

import ee

from app.utils.gee_client import init_gee
from app.utils.hazard_lulc import LULC_ASSETS, _is_known_class_code, _load_landcover_source_image

RADII_M = [30, 60, 100, 150, 250, 400, 600]

# (location, group, lat, lon) - copied verbatim from the completed real-parcel diagnostic run.
PARCELS = [
    ("Ogunpa (Ibadan)", "flooded", 7.37720, 3.88912),
    ("Ogunpa (Ibadan)", "flooded", 7.37769, 3.89013),
    ("Ogunpa (Ibadan)", "flooded", 7.37718, 3.88698),
    ("Ogunpa (Ibadan)", "flooded", 7.37925, 3.88601),
    ("Ogunpa (Ibadan)", "flooded", 7.37835, 3.88768),
    ("Bodija GRA (Ibadan)", "control", 7.42479, 3.90894),
    ("Bodija GRA (Ibadan)", "control", 7.42714, 3.90553),
    ("Bodija GRA (Ibadan)", "control", 7.42660, 3.90940),
    ("Bodija GRA (Ibadan)", "control", 7.42803, 3.90738),
    ("Bodija GRA (Ibadan)", "control", 7.42901, 3.90821),
    ("Makurdi", "flooded", 7.73417, 8.54071),
    ("Makurdi", "flooded", 7.73395, 8.54082),
    ("Makurdi", "flooded", 7.73076, 8.53926),
    ("Makurdi", "flooded", 7.72984, 8.54004),
    ("Makurdi", "flooded", 7.72979, 8.53987),
    ("Abuja Asokoro/Maitama", "control", 9.04828, 7.52819),
    ("Abuja Asokoro/Maitama", "control", 9.04881, 7.52974),
    ("Abuja Asokoro/Maitama", "control", 9.04535, 7.52837),
    ("Abuja Asokoro/Maitama", "control", 9.04605, 7.52891),
    ("Abuja Asokoro/Maitama", "control", 9.04822, 7.52785),
    ("Ogbaru", "flooded", 6.16127, 6.75475),
    ("Ogbaru", "flooded", 6.16087, 6.74740),
    ("Ogbaru", "flooded", 6.16070, 6.74596),
    ("Ogbaru", "flooded", 6.16702, 6.74283),
    ("Ogbaru", "flooded", 6.16820, 6.74516),
    ("Lokoja", "flooded", 7.80008, 6.73485),
    ("Lokoja", "flooded", 7.80148, 6.73494),
    ("Lokoja", "flooded", 7.80301, 6.73456),
    ("Lokoja", "flooded", 7.79972, 6.73410),
    ("Lokoja", "flooded", 7.80276, 6.73150),
    ("Jos", "control", 9.89665, 8.85903),
    ("Jos", "control", 9.89812, 8.85812),
    ("Jos", "control", 9.89641, 8.85844),
    ("Jos", "control", 9.89803, 8.86001),
    ("Jos", "control", 9.89834, 8.85875),
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
    """Mirrors hazard_pluvial.py's _fetch_impervious_fraction fallback loop exactly, but keeps
    (image, class_colors) paired together instead of discarding class_colors - needed here since
    we reuse the SAME resolved image across multiple radii and must validate class codes against
    the matching table each time.
    """
    for asset_id, class_colors in LULC_ASSETS:
        try:
            candidate_image = _load_landcover_source_image(asset_id)
            frac = _fraction_at_scale(candidate_image, class_colors, base_geom)
        except Exception:
            continue
        if frac is not None:
            return candidate_image, class_colors, frac
    return None, None, None


def main() -> int:
    init_gee()
    rows = []
    for loc, group, lat, lon in PARCELS:
        base_geom = ee.Geometry.Point([lon, lat]).buffer(RADII_M[0])
        image, class_colors, frac_30 = _resolve_asset(base_geom)
        if image is None:
            print(f"[WARN] {loc} ({lat:.5f},{lon:.5f}): no LULC asset resolved - skipping", flush=True)
            continue
        results = {RADII_M[0]: frac_30}
        for r in RADII_M[1:]:
            geom = ee.Geometry.Point([lon, lat]).buffer(r)
            try:
                results[r] = _fraction_at_scale(image, class_colors, geom)
            except Exception as exc:
                print(f"  [ERROR] {loc} r={r}m: {exc!r}", flush=True)
                results[r] = None
        row = {"location": loc, "group": group, "lat": lat, "lon": lon, **{f"r{r}": results.get(r) for r in RADII_M}}
        rows.append(row)
        vals_str = " | ".join(f"{r}m={results.get(r)*100:.1f}%" if results.get(r) is not None else f"{r}m=None" for r in RADII_M)
        print(f"{loc} [{group}] ({lat:.5f},{lon:.5f}): {vals_str}", flush=True)

    if not rows:
        print("No parcels computed.", flush=True)
        return 1

    print("\n=== PER-LOCATION MEDIANS BY RADIUS ===", flush=True)
    by_loc = {}
    for row in rows:
        by_loc.setdefault(row["location"], []).append(row)
    loc_medians = {}
    for loc, group_rows in by_loc.items():
        medians = {}
        for r in RADII_M:
            vals = [row[f"r{r}"] for row in group_rows if row[f"r{r}"] is not None]
            medians[r] = round(statistics.median(vals) * 100, 1) if vals else None
        loc_medians[loc] = {"group": group_rows[0]["group"], **medians}
        vals_str = " | ".join(f"{r}m={medians[r]}%" for r in RADII_M)
        print(f"  {loc} [{group_rows[0]['group']}]: {vals_str}", flush=True)

    print("\n=== PAIR SEPARATION BY RADIUS (flooded_median - control_median, percentage points) ===", flush=True)
    pairs = [
        ("Ogunpa (Ibadan)", "Bodija GRA (Ibadan)"),
        ("Makurdi", "Abuja Asokoro/Maitama"),
        ("Lokoja", "Jos"),
    ]
    for flooded_loc, control_loc in pairs:
        fm, cm = loc_medians.get(flooded_loc), loc_medians.get(control_loc)
        if not fm or not cm:
            continue
        print(f"\n  {flooded_loc} vs {control_loc}:", flush=True)
        for r in RADII_M:
            f, c = fm.get(r), cm.get(r)
            if f is None or c is None:
                print(f"    {r}m: missing data", flush=True)
                continue
            print(f"    {r}m: {f}% vs {c}%  (gap = {f - c:+.1f}pp)", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
