"""Real-parcel diagnostic pass - Phase 2 of the historical-flood investigation.

DIAGNOSTIC ONLY. Production code (hazard_pluvial.py, hazard_flood.py, weights, thresholds,
CHIRPS percentile, curve numbers, production APIs) remains completely untouched by this script.

Phase 1 used single hand-picked "city name" point coordinates. This phase answers whether the
~100%-impervious saturation seen there was a genuine land-cover characteristic or an artifact of
the point happening to land on a road/plaza. It anchors on REAL OSM building centroids (never the
building footprint itself as the sampling geometry - a building polygon is close to 100% built
almost by definition, which would only make the saturation question worse) near each diagnostic-
pair location, then reproduces compute_pluvial_risk()'s exact scoring formula (terrain*0.35 +
runoff*0.35 + impervious*0.30, same classify_risk() call) directly against each parcel.

This is a leaner reimplementation of the scoring math, not the full compute_pluvial_risk()
pipeline - deliberately skips map rendering, building-footprint fetch for the overlay, and GIS
export prep, none of which affect the score and all of which multiply live Earth Engine round
trips for no diagnostic benefit. The score formula itself is copied verbatim from
app/utils/hazard_pluvial.py so results are numerically identical to what production would produce
for the same boundary - verified against the phase-1 point results as a sanity check.

v2: the first version of this script called the FULL compute_pluvial_risk() (map rendering +
building fetch + GIS export prep included) for all 40 parcels and hung indefinitely after ~40
minutes with zero progress for the last 10 - almost certainly a single stuck Earth Engine network
call with no client-side timeout. This version (a) does far fewer GEE round trips per parcel, (b)
wraps every network-triggering call in a hard timeout via ThreadPoolExecutor so one bad call skips
that parcel instead of hanging the whole run, and (c) force-flushes output after every parcel so
progress is visible in real time instead of sitting in an unflushed buffer.

Run: python scratch/test_pluvial_parcel_diagnostic.py
"""
import math
import os
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

sys.path.insert(0, os.path.abspath("."))
from dotenv import load_dotenv
load_dotenv()

import ee
from sqlalchemy import text

from app.db import SessionLocal
from app.utils.gee_client import init_gee
from app.utils.hazard_common import classify_risk
from app.utils.hazard_local_data import compute_scs_runoff, derive_hydrologic_soil_group, summarize_local_soil_points
from app.utils.hazard_pluvial import _fetch_impervious_fraction, _site_type_from_impervious_fraction

PARCELS_PER_LOCATION = 5
PARCEL_HALF_WIDTH_DEG = 0.00027  # ~60m box - a realistic residential-plot scale
LOCAL_AREA_RADIUS_M = 300        # wider window for HAND/TWI distribution stats (need multiple pixels)
RIVER_DIST_CAP_M = 2000.0        # same cap hazard_pluvial.py applies to its own drainage-distance signal
CHIRPS_SCALE_M = 5500
SOIL_SCALE_M = 250
CALL_TIMEOUT_S = 75              # hard ceiling per network-triggering step; skip the parcel on expiry

_EXECUTOR = ThreadPoolExecutor(max_workers=1)


def _with_timeout(fn, *args, **kwargs):
    future = _EXECUTOR.submit(fn, *args, **kwargs)
    return future.result(timeout=CALL_TIMEOUT_S)


LOCATIONS = [
    ("Ogunpa River corridor (Ibadan)", 7.3775, 3.8880, "flooded", "Ogunpa vs Bodija"),
    ("Bodija GRA (Ibadan)", 7.4270, 3.9080, "control", "Ogunpa vs Bodija"),
    ("Makurdi (River Benue)", 7.7322, 8.5391, "flooded", "Makurdi vs Abuja"),
    ("Abuja - Asokoro/Maitama", 9.0479, 7.5289, "control", "Makurdi vs Abuja"),
    ("Ogbaru (River Niger, Anambra)", 6.1667, 6.7500, "flooded", "Ogbaru vs Obudu"),
    ("Obudu Plateau", 6.7500, 9.3500, "control", "Ogbaru vs Obudu"),
    ("Lokoja (Niger-Benue confluence)", 7.8023, 6.7333, "flooded", "Lokoja vs Jos"),
    ("Jos (Plateau highland)", 9.8965, 8.8583, "control", "Lokoja vs Jos"),
]


def _find_real_building_centroids(db, lat, lon, n=PARCELS_PER_LOCATION):
    # Buffers the POINT in geography space (cheap, one computation), then compares back in native
    # geometry via ST_Intersects - same pattern hazard_common.py's fetch_buildings_near already
    # uses. Casting m.geom itself to ::geography per row (the first version of this query) can't
    # use the table's geometry GiST index and forces a sequential scan over all 17.7M rows - this
    # is exactly what caused the "hang" diagnosed just before this fix (it wasn't Earth Engine at
    # all, it was this query never returning within any reasonable time).
    for radius_m in (300, 800, 2000, 5000):
        rows = db.execute(
            text(
                """
                SELECT ST_X(ST_Centroid(m.geom)) AS lon, ST_Y(ST_Centroid(m.geom)) AS lat
                FROM multipolygons m
                WHERE m.building IS NOT NULL
                  AND ST_Intersects(
                      m.geom,
                      ST_Buffer(ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, :radius_m)::geometry
                  )
                ORDER BY random()
                LIMIT :n
                """
            ),
            {"lon": lon, "lat": lat, "radius_m": radius_m, "n": n},
        ).fetchall()
        if len(rows) >= min(3, n):
            return [(float(r.lat), float(r.lon)) for r in rows], radius_m
    return [], None


def _square_boundary(lat, lon, half_width_deg=PARCEL_HALF_WIDTH_DEG):
    d = half_width_deg
    return {
        "type": "Polygon",
        "coordinates": [[
            [lon - d, lat - d], [lon + d, lat - d], [lon + d, lat + d], [lon - d, lat + d], [lon - d, lat - d],
        ]],
    }


def _score_parcel(lat, lon):
    """Reproduces compute_pluvial_risk()'s score/breakdown fields directly (verbatim formula),
    plus diagnostic-only HAND/TWI/upstream-area/capped-river-distance stats. No map rendering, no
    building fetch, no GIS export - none affect the score.
    """
    boundary = _square_boundary(lat, lon)
    geom = ee.Geometry(boundary)
    analysis_region = geom.buffer(1000)

    # --- Terrain susceptibility (verbatim from hazard_pluvial.py) -----------------------------
    dem_proxy = ee.ImageCollection("COPERNICUS/DEM/GLO30_2024_1").select("DEM").mosaic()
    slope_img = ee.Terrain.slope(dem_proxy).rename("slope_deg")
    local_mean_elev_img = dem_proxy.focal_mean(radius=300, units="meters")
    depression_img = local_mean_elev_img.subtract(dem_proxy).rename("depression_m")
    flow_acc = ee.Image("WWF/HydroSHEDS/15ACC").select("b1")
    local_channels = flow_acc.gt(100)
    local_channel_dist = local_channels.fastDistanceTransform(30).sqrt()
    local_drainage_dist_img = local_channel_dist.multiply(flow_acc.projection().nominalScale()).rename("local_drainage_m")
    flatness_score_img = ee.Image(1).subtract(slope_img.divide(15).min(1)).max(0)
    drainage_score_img = ee.Image(1).subtract(local_drainage_dist_img.divide(500).min(1)).max(0)
    depression_score_img = depression_img.divide(3).max(0).min(1)
    susceptibility_img = (
        depression_score_img.multiply(0.40).add(flatness_score_img.multiply(0.35)).add(drainage_score_img.multiply(0.25))
    ).rename("susceptibility")

    # --- NEW diagnostic-only signals, folded into the SAME combined dict as terrain to save a
    # round trip (HAND/TWI/upstream-area/river-distance never feed the score below).
    merit = ee.Image("MERIT/Hydro/v1_0_1")
    hand_img = merit.select("hnd")
    merit_upa = merit.select("upa")
    merit_wat = merit.select("wat")
    slope_rad = slope_img.multiply(math.pi / 180.0)
    cell_area_m2 = flow_acc.projection().nominalScale().pow(2)
    twi_img = flow_acc.multiply(cell_area_m2).divide(slope_rad.tan().max(0.001)).log().rename("twi")
    river_mask = merit_wat.gt(0)
    river_dist_m_img = (
        river_mask.fastDistanceTransform(30).sqrt().multiply(merit_wat.projection().nominalScale()).rename("river_dist_m")
    )
    local_area = ee.Geometry.Point([lon, lat]).buffer(LOCAL_AREA_RADIUS_M)
    pct_reducer = (
        ee.Reducer.median().combine(ee.Reducer.min(), sharedInputs=True).combine(ee.Reducer.percentile([10, 25]), sharedInputs=True)
    )

    def _fetch_terrain_and_diagnostics():
        combined = ee.Dictionary({
            "susceptibility": susceptibility_img.reduceRegion(ee.Reducer.mean(), geom, scale=30, maxPixels=1e9).get("susceptibility"),
            "mean_slope_deg": slope_img.reduceRegion(ee.Reducer.mean(), geom, scale=30, maxPixels=1e9).get("slope_deg"),
            "mean_depression_m": depression_img.reduceRegion(ee.Reducer.mean(), geom, scale=30, maxPixels=1e9).get("depression_m"),
            "distance_to_drainage_m": local_drainage_dist_img.reduceRegion(ee.Reducer.mean(), geom, scale=90, maxPixels=1e9).get("local_drainage_m"),
            "upa_km2": merit_upa.reduceRegion(ee.Reducer.mean(), local_area, scale=90, maxPixels=1e9, bestEffort=True).get("upa"),
            "river_dist_m": river_dist_m_img.reduceRegion(ee.Reducer.mean(), local_area, scale=90, maxPixels=1e9, bestEffort=True).get("river_dist_m"),
        }).getInfo()
        hand_stats = hand_img.reduceRegion(pct_reducer, local_area, scale=90, maxPixels=1e9, bestEffort=True).getInfo()
        twi_stats = twi_img.reduceRegion(pct_reducer, local_area, scale=450, maxPixels=1e9, bestEffort=True).getInfo()
        return combined, hand_stats, twi_stats

    def _fetch_chirps_and_soil():
        chirps_daily = ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY").select("precipitation")
        p99_image = chirps_daily.reduce(ee.Reducer.percentile([99])).rename("p99_daily_mm")
        combined = ee.Dictionary({
            "chirps_p99_mm": p99_image.reduceRegion(ee.Reducer.mean(), analysis_region, scale=CHIRPS_SCALE_M, maxPixels=1e9).get("p99_daily_mm"),
            "sand_pct": ee.Image("OpenLandMap/SOL/SOL_SAND-WFRACTION_USDA-3A1A1A_M/v02").select("b0").reduceRegion(ee.Reducer.mean(), analysis_region, scale=SOIL_SCALE_M, maxPixels=1e9).get("b0"),
            "clay_pct": ee.Image("OpenLandMap/SOL/SOL_CLAY-WFRACTION_USDA-3A1A1A_M/v02").select("b0").reduceRegion(ee.Reducer.mean(), analysis_region, scale=SOIL_SCALE_M, maxPixels=1e9).get("b0"),
        }).getInfo()
        return combined

    terrain_diag, hand_stats, twi_stats = _with_timeout(_fetch_terrain_and_diagnostics)
    rain_soil = _with_timeout(_fetch_chirps_and_soil)
    impervious_frac, _ = _with_timeout(_fetch_impervious_fraction, geom)

    impervious_available = impervious_frac is not None
    if not impervious_available:
        impervious_frac = 0.0
    resolved_site_type = _site_type_from_impervious_fraction(impervious_frac) if impervious_available else "residential_low_density"

    sand_pct, clay_pct = rain_soil.get("sand_pct"), rain_soil.get("clay_pct")
    hydrologic_soil_group = derive_hydrologic_soil_group(sand_pct, clay_pct) if sand_pct is not None and clay_pct is not None else "B"

    resolved_rainfall_mm = float(rain_soil["chirps_p99_mm"]) if rain_soil.get("chirps_p99_mm") is not None else None
    scs_runoff = None
    runoff_coefficient = 0.0
    if resolved_rainfall_mm:
        scs_runoff = compute_scs_runoff(hydrologic_soil_group, resolved_site_type, resolved_rainfall_mm)
        runoff_coefficient = scs_runoff["runoff_coefficient"]

    terrain_score = max(0.0, min(1.0, float(terrain_diag.get("susceptibility") or 0.0)))
    runoff_score = max(0.0, min(1.0, runoff_coefficient))
    impervious_score = max(0.0, min(1.0, impervious_frac))
    risk_value = max(0.0, min(1.0, terrain_score * 0.35 + runoff_score * 0.35 + impervious_score * 0.30))
    risk_class, _color = classify_risk(risk_value, True)

    raw_river_dist = terrain_diag.get("river_dist_m")
    capped_river_dist = min(float(raw_river_dist), RIVER_DIST_CAP_M) if raw_river_dist is not None else RIVER_DIST_CAP_M

    return {
        "risk_value": risk_value, "risk_class": risk_class,
        "design_rainfall_mm": round(resolved_rainfall_mm, 1) if resolved_rainfall_mm else None,
        "impervious_pct": round(impervious_frac * 100, 1),
        "site_type_used": resolved_site_type, "hsg": hydrologic_soil_group,
        "curve_number": (scs_runoff or {}).get("curve_number"),
        "runoff_mm": (scs_runoff or {}).get("runoff_mm"),
        "runoff_coefficient": round(runoff_coefficient, 3),
        "terrain_score": round(terrain_score, 3), "runoff_score": round(runoff_score, 3), "impervious_score": round(impervious_score, 3),
        "terrain_slope_deg": round(float(terrain_diag.get("mean_slope_deg") or 0.0), 2),
        "terrain_depression_m": round(float(terrain_diag.get("mean_depression_m") or 0.0), 2),
        "distance_to_drainage_m": round(min(float(terrain_diag.get("distance_to_drainage_m") or 2000.0), 2000.0), 1),
        "hand_median": hand_stats.get("hnd_median"), "hand_min": hand_stats.get("hnd_min"),
        "hand_p10": hand_stats.get("hnd_p10"), "hand_p25": hand_stats.get("hnd_p25"),
        "twi_median": twi_stats.get("twi_median"), "twi_min": twi_stats.get("twi_min"),
        "twi_p10": twi_stats.get("twi_p10"), "twi_p25": twi_stats.get("twi_p25"),
        "upa_km2": terrain_diag.get("upa_km2"), "river_dist_m_capped": round(capped_river_dist, 1),
    }


def main() -> int:
    db = SessionLocal()
    init_gee()
    all_parcels = []
    try:
        for name, lat, lon, group, pair_label in LOCATIONS:
            centroids, used_radius = _find_real_building_centroids(db, lat, lon)
            if not centroids:
                print(f"[WARN] {name}: no real buildings found nearby even at 5km - skipping", flush=True)
                continue
            print(f"\n=== {name} [{group}] - {len(centroids)} real buildings found within {used_radius}m ===", flush=True)
            for i, (plat, plon) in enumerate(centroids):
                try:
                    row = _score_parcel(plat, plon)
                except FutureTimeoutError:
                    print(f"  [TIMEOUT] parcel {i+1} ({plat:.5f},{plon:.5f}): exceeded {CALL_TIMEOUT_S}s - skipped", flush=True)
                    continue
                except Exception as exc:
                    print(f"  [ERROR] parcel {i+1} ({plat:.5f},{plon:.5f}): {exc!r}", flush=True)
                    continue
                row.update({"location": name, "group": group, "pair_label": pair_label, "parcel_idx": i + 1, "lat": plat, "lon": plon})
                all_parcels.append(row)
                print(
                    f"  parcel {i+1} ({plat:.5f},{plon:.5f}): risk={row['risk_value']:.3f}/{row['risk_class']} | "
                    f"impervious={row['impervious_pct']}% | terrain={row['terrain_score']:.3f} | "
                    f"HAND(median/min/p10)={row['hand_median']},{row['hand_min']},{row['hand_p10']} | "
                    f"TWI(median/p10)={row['twi_median']},{row['twi_p10']} | river_dist_capped={row['river_dist_m_capped']}m",
                    flush=True,
                )
    finally:
        db.close()

    if not all_parcels:
        print("No parcels computed - aborting summary.", flush=True)
        return 1

    print("\n\n=== PER-LOCATION MEDIANS (n parcels per location) ===", flush=True)
    by_location = {}
    for row in all_parcels:
        by_location.setdefault(row["location"], []).append(row)

    loc_summary = {}
    for loc, rows in by_location.items():
        def med(field):
            vals = [r[field] for r in rows if r[field] is not None]
            return round(statistics.median(vals), 3) if vals else None
        def rng(field):
            vals = [r[field] for r in rows if r[field] is not None]
            return (round(min(vals), 3), round(max(vals), 3)) if vals else (None, None)

        summary = {
            "n": len(rows), "group": rows[0]["group"], "pair_label": rows[0]["pair_label"],
            "risk_value_median": med("risk_value"), "risk_value_range": rng("risk_value"),
            "impervious_pct_median": med("impervious_pct"), "impervious_pct_range": rng("impervious_pct"),
            "terrain_score_median": med("terrain_score"),
            "hand_median_of_medians": med("hand_median"), "hand_min_of_mins": rng("hand_min")[0],
            "twi_median_of_medians": med("twi_median"),
        }
        loc_summary[loc] = summary
        print(
            f"  {loc} [{summary['group']}] (n={summary['n']}): risk={summary['risk_value_median']} "
            f"(range {summary['risk_value_range']}) | impervious%={summary['impervious_pct_median']} "
            f"(range {summary['impervious_pct_range']}) | terrain={summary['terrain_score_median']} | "
            f"HAND_median={summary['hand_median_of_medians']}m (min-of-mins {summary['hand_min_of_mins']}m) | "
            f"TWI_median={summary['twi_median_of_medians']}",
            flush=True,
        )

    PHASE1_POINT = {
        "Ogunpa River corridor (Ibadan)": {"impervious_pct": 100.0, "risk_value": 0.586},
        "Bodija GRA (Ibadan)": {"impervious_pct": 100.0, "risk_value": 0.583},
        "Makurdi (River Benue)": {"impervious_pct": 100.0, "risk_value": 0.653},
        "Abuja - Asokoro/Maitama": {"impervious_pct": 100.0, "risk_value": 0.716},
        "Ogbaru (River Niger, Anambra)": {"impervious_pct": 3.6, "risk_value": 0.215},
        "Obudu Plateau": {"impervious_pct": 0.0, "risk_value": 0.164},
        "Lokoja (Niger-Benue confluence)": {"impervious_pct": 100.0, "risk_value": 0.615},
        "Jos (Plateau highland)": {"impervious_pct": 100.0, "risk_value": 0.718},
    }
    print("\n=== BEFORE (single hand-picked point) vs AFTER (median of real-building parcels) ===", flush=True)
    for loc, summary in loc_summary.items():
        before = PHASE1_POINT.get(loc, {})
        print(
            f"  {loc}: impervious% before={before.get('impervious_pct')} -> after_median={summary['impervious_pct_median']} "
            f"(range {summary['impervious_pct_range']}) | risk before={before.get('risk_value')} -> "
            f"after_median={summary['risk_value_median']} (range {summary['risk_value_range']})",
            flush=True,
        )

    print("\n=== PAIR COMPARISONS (parcel medians) ===", flush=True)
    pairs = [
        ("Ogunpa River corridor (Ibadan)", "Bodija GRA (Ibadan)"),
        ("Makurdi (River Benue)", "Abuja - Asokoro/Maitama"),
        ("Ogbaru (River Niger, Anambra)", "Obudu Plateau"),
        ("Lokoja (Niger-Benue confluence)", "Jos (Plateau highland)"),
    ]
    for flooded_loc, control_loc in pairs:
        fs, cs = loc_summary.get(flooded_loc), loc_summary.get(control_loc)
        if not fs or not cs:
            continue
        print(f"\n  {flooded_loc} vs {control_loc}:", flush=True)
        print(f"    risk_value:        {fs['risk_value_median']}  vs  {cs['risk_value_median']}", flush=True)
        print(f"    impervious_pct:    {fs['impervious_pct_median']}  vs  {cs['impervious_pct_median']}", flush=True)
        print(f"    terrain_score:     {fs['terrain_score_median']}  vs  {cs['terrain_score_median']}", flush=True)
        print(f"    HAND_median_m:     {fs['hand_median_of_medians']}  vs  {cs['hand_median_of_medians']}", flush=True)
        print(f"    TWI_median:        {fs['twi_median_of_medians']}  vs  {cs['twi_median_of_medians']}", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
