"""Pluvial V3 R&D - signal diagnostic against HAD-FCDR25 (Lateef et al. 2025, Zenodo
10.5281/zenodo.15204588). NO production code touched. NO model trained/fitted - per the standing
"do not train or modify any model" rule, this computes the SAME kind of no-fit rank/correlation
diagnostic used in Stage A (Mann-Whitney-style AUC on raw feature values), not a fitted classifier.

HONEST FRAMING: Hadejia sits in the Hadejia-Nguru wetlands, a seasonal river-floodplain system.
This flood event is most likely predominantly FLUVIAL/seasonal-floodplain inundation, not urban
pluvial drainage failure - so this experiment does not validate the pluvial mechanism specifically.
It answers the broader, still load-bearing question: do TWI/relative-elevation/depression/drainage/
runoff/HAND carry ANY real discriminating signal for flood extent when the LABELS themselves are
good (independently derived via Sentinel-2 MNDWI accuracy-assessment points, not our own
self-derived SAR change detection)? A positive result here would be necessary-but-not-sufficient
evidence for the pluvial case; a negative result would suggest the problem is the predictors
themselves, not just label quality - directly the question the user posed.

Hadejia is ALREADY-USED development data (V1/V2). This is explicitly a diagnostic/development use,
never to be presented as a fresh validation event, per the standing exclusion-set rule.

Samples 60 points per year (30 flooded + 30 non-flooded, stratified from the 300 independent
Sentinel-2-MNDWI-labeled testing points per year) - smaller than the full 600 to keep GEE round
trips tractable, but far larger and better-labeled than anything Stage C could build (48 points,
3 shaky self-derived masks).

Run: python scratch/v3_hadfcdr25_signal_diagnostic.py
"""
import math
import os
import random
import sys

sys.path.insert(0, os.path.abspath("."))
from dotenv import load_dotenv
load_dotenv()

import ee
import geopandas as gpd
from app.utils.gee_client import init_gee
from app.utils.hazard_local_data import compute_scs_runoff, derive_hydrologic_soil_group, DEFAULT_SITE_TYPE
from app.utils.hazard_pluvial import _fetch_impervious_fraction

HADFCDR25_DIR = os.path.join(
    "C:/Users/User/AppData/Local/Temp/claude/c--Users-User-Desktop-project/989c6038-a552-4810-95c3-e5fa30239b8a/scratchpad/HAD-FCDR25/extracted"
)
POINTS_PER_CLASS_PER_YEAR = 30
RANDOM_SEED = 42

# Approximate event windows inferred from the dataset's own S1/PlanetScope scene filenames
# (S1_20200814...S1_20201025 for 2020; 2022 layer dated similarly in the wet season) - used only
# for the event_rain_mm feature (actual CHIRPS total over the window), not for label generation.
EVENT_WINDOWS = {2020: ("2020-08-01", "2020-10-31"), 2022: ("2022-08-01", "2022-10-31")}


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


def _rank_auc(pos, neg):
    pairs = concordant = ties = 0
    for p in pos:
        for n in neg:
            pairs += 1
            if p > n:
                concordant += 1
            elif p == n:
                ties += 1
    return (concordant + 0.5 * ties) / pairs if pairs else float("nan")


def _load_stratified_points(year):
    path = os.path.join(HADFCDR25_DIR, "Flood_Accuracy_Assessment", "Flood_Testing_Points", f"Flood_{year}_Random_Testing_Points.shp")
    gdf = gpd.read_file(path).to_crs(epsg=4326)
    flooded = gdf[gdf["MNDWI_Valu"] == 1]
    non_flooded = gdf[gdf["MNDWI_Valu"] == 0]
    rng = random.Random(RANDOM_SEED)
    flooded_sample = flooded.sample(n=min(POINTS_PER_CLASS_PER_YEAR, len(flooded)), random_state=RANDOM_SEED)
    non_flooded_sample = non_flooded.sample(n=min(POINTS_PER_CLASS_PER_YEAR, len(non_flooded)), random_state=RANDOM_SEED)
    points = []
    for _, row in flooded_sample.iterrows():
        points.append({"year": year, "lat": row.geometry.y, "lon": row.geometry.x, "label": 1})
    for _, row in non_flooded_sample.iterrows():
        points.append({"year": year, "lat": row.geometry.y, "lon": row.geometry.x, "label": 0})
    return points


def main() -> int:
    init_gee()
    dem = ee.ImageCollection("COPERNICUS/DEM/GLO30_2024_1").select("DEM").mosaic()
    slope_deg_img = ee.Terrain.slope(dem)
    slope_rad_img = slope_deg_img.multiply(math.pi / 180.0)
    hydrosheds_acc = ee.Image("WWF/HydroSHEDS/15ACC").select("b1")
    hydrosheds_cell_area = hydrosheds_acc.projection().nominalScale().pow(2)
    twi_hs_img = hydrosheds_acc.multiply(hydrosheds_cell_area).divide(slope_rad_img.tan().max(0.001)).log().rename("twi_hs")
    local_channels = hydrosheds_acc.gt(100)
    local_channel_dist_img = (
        local_channels.fastDistanceTransform(30).sqrt().multiply(hydrosheds_acc.projection().nominalScale()).rename("drain_dist_m")
    )
    hand_img = ee.Image("MERIT/Hydro/v1_0_1").select("hnd")

    all_samples = []
    for year in (2020, 2022):
        points = _load_stratified_points(year)
        print(f"\n=== Year {year}: {len(points)} points ({sum(1 for p in points if p['label']==1)} flooded) ===", flush=True)

        chirps_daily = ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY").select("precipitation")
        p99_image = chirps_daily.reduce(ee.Reducer.percentile([99])).rename("p99_daily_mm")
        start, end = EVENT_WINDOWS[year]
        event_rain_img = chirps_daily.filterDate(start, end).sum().rename("event_rain_mm")

        for i, pt_info in enumerate(points):
            lat, lon = pt_info["lat"], pt_info["lon"]
            pt = ee.Geometry.Point([lon, lat])
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
                print(f"  [ERROR] point {i} ({lat:.4f},{lon:.4f}): {exc!r}", flush=True)
                continue

            elev = combined.get("elev_m")
            focal300 = combined.get("focal_300_m")
            focal1000 = combined.get("focal_1000_m")
            depression_300m = (focal300 - elev) if (elev is not None and focal300 is not None) else None
            relative_elev_1000m = (elev - focal1000) if (elev is not None and focal1000 is not None) else None
            sand, clay = combined.get("sand_pct"), combined.get("clay_pct")
            hsg = derive_hydrologic_soil_group(sand, clay) if sand is not None and clay is not None else "B"
            event_rain = combined.get("event_rain_mm")
            scs = compute_scs_runoff(hsg, DEFAULT_SITE_TYPE, event_rain) if event_rain else None
            raw_drain_dist = combined.get("drain_dist_m")
            capped_drain_dist = min(float(raw_drain_dist), 2000.0) if raw_drain_dist is not None else None

            sample = {
                "year": year, "label": pt_info["label"],
                "twi_hs": combined.get("twi_hs"), "slope_deg": combined.get("slope_deg"),
                "depression_300m": depression_300m, "relative_elev_1000m": relative_elev_1000m,
                "chirps_p99_mm": combined.get("chirps_p99_mm"), "event_rain_mm": event_rain,
                "runoff_mm": scs["runoff_mm"] if scs else None,
                "runoff_coefficient": scs["runoff_coefficient"] if scs else None,
                "impervious_pct": round(impervious_frac * 100, 1) if impervious_frac is not None else None,
                "drain_dist_m": capped_drain_dist, "hand_m": combined.get("hand_m"),
            }
            all_samples.append(sample)
        print(f"  extracted {sum(1 for s in all_samples if s['year']==year)} of {len(points)} points", flush=True)

    print(f"\n{'='*90}\nTotal samples: {len(all_samples)}\n{'='*90}", flush=True)
    features = ["twi_hs", "slope_deg", "depression_300m", "relative_elev_1000m", "chirps_p99_mm",
                "event_rain_mm", "runoff_mm", "runoff_coefficient", "impervious_pct", "drain_dist_m", "hand_m"]
    flooded = [s for s in all_samples if s["label"] == 1]
    non_flooded = [s for s in all_samples if s["label"] == 0]
    print(f"Flooded n={len(flooded)}, Non-flooded n={len(non_flooded)}\n")

    print("Rank-AUC per feature (0.5=no signal, 1.0=perfect, no model fitted - raw value ranking only):")
    for feat in features:
        pos = [s[feat] for s in flooded if s.get(feat) is not None]
        neg = [s[feat] for s in non_flooded if s.get(feat) is not None]
        if len(pos) < 3 or len(neg) < 3:
            print(f"  {feat:22s}: insufficient data (pos={len(pos)}, neg={len(neg)})")
            continue
        auc = _rank_auc(pos, neg)
        print(f"  {feat:22s}: AUC={auc:.3f}  (n_pos={len(pos)}, n_neg={len(neg)}, "
              f"mean_pos={sum(pos)/len(pos):.2f}, mean_neg={sum(neg)/len(neg):.2f})")

    import json
    out_path = os.path.join(os.path.dirname(__file__), "v3_hadfcdr25_samples.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_samples, f, indent=2)
    print(f"\nSamples preserved: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
