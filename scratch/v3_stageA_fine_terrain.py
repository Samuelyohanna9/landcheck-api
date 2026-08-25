"""Pluvial V3 R&D - STAGE A: fine-terrain feasibility. NO production code touched, NO score
proposed. Reuses the 12 Phase 4 pluvial-set matched pairs (already development/diagnostic data,
never again usable as blind validation - this is exactly the R&D reuse that rule permits).

Computes RAW physical terrain metrics only - nothing is combined into a score:
  - slope (Copernicus GLO-30, 30m - already the production DEM, free/commercial-clear license)
  - local relative elevation / depression at THREE window sizes (50m, 100m, 300m) - tests whether a
    genuinely finer microtopography signal exists below production's current single 300m window
  - contributing-area proxies at two resolutions: WWF/HydroSHEDS 15ACC (~450m, current production
    input) vs MERIT Hydro upa (~90m, already used by the floodplain branch - finer, still native to
    GEE, no off-platform processing needed)
  - TWI = ln(contributing_area_m2 / tan(slope)) computed from BOTH contributing-area proxies, so the
    two can be compared directly

A real, load-bearing feasibility finding, not swept under the rug: true 30m D8/D-infinity flow
ACCUMULATION (as opposed to a coarser pre-computed contributing-area raster) has no native GEE
algorithm - GEE's map-reduce paradigm has no iterative pixel-to-pixel flow-routing primitive. A
genuine 30m accumulation would require off-platform hydrological processing (WhiteboxTools, TauDEM,
richdem) on downloaded GLO-30 tiles, not a `python -c` GEE script. This script does NOT attempt
that - it tests whether the two GEE-native contributing-area options (HydroSHEDS 15ACC, MERIT upa)
already available produce different, better-discriminating TWI than the coarser one currently used.

Run: python scratch/v3_stageA_fine_terrain.py
"""
import math
import os
import statistics
import sys

sys.path.insert(0, os.path.abspath("."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv()

import ee
from app.utils.gee_client import init_gee
from phase4_pluvial_locations import LOCATIONS

WINDOWS_M = [50, 100, 300]


def main() -> int:
    init_gee()
    dem = ee.ImageCollection("COPERNICUS/DEM/GLO30_2024_1").select("DEM").mosaic()
    slope_deg_img = ee.Terrain.slope(dem)
    slope_rad_img = slope_deg_img.multiply(math.pi / 180.0)

    hydrosheds_acc = ee.Image("WWF/HydroSHEDS/15ACC").select("b1")
    hydrosheds_cell_area = hydrosheds_acc.projection().nominalScale().pow(2)
    hydrosheds_contrib_area = hydrosheds_acc.multiply(hydrosheds_cell_area)

    merit_upa = ee.Image("MERIT/Hydro/v1_0_1").select("upa")  # already km2
    merit_contrib_area_m2 = merit_upa.multiply(1e6)

    twi_hydrosheds = hydrosheds_contrib_area.divide(slope_rad_img.tan().max(0.001)).log().rename("twi_hs")
    twi_merit = merit_contrib_area_m2.divide(slope_rad_img.tan().max(0.001)).log().rename("twi_merit")

    rows = []
    for loc in LOCATIONS:
        name, lat, lon, group = loc["name"], loc["lat"], loc["lon"], loc["group"]
        pt = ee.Geometry.Point([lon, lat])
        parcel = pt.buffer(30)  # a real parcel-scale point, not a wide local area

        combined = {"name": name, "group": group}
        try:
            base = ee.Dictionary({
                "slope_deg": slope_deg_img.reduceRegion(ee.Reducer.mean(), parcel, scale=30, maxPixels=1e9).get("slope"),
                "elev_m": dem.reduceRegion(ee.Reducer.mean(), parcel, scale=30, maxPixels=1e9).get("DEM"),
                "upa_km2": merit_upa.reduceRegion(ee.Reducer.max(), pt.buffer(90), scale=90, maxPixels=1e9, bestEffort=True).get("upa"),
                "hs_acc": hydrosheds_acc.reduceRegion(ee.Reducer.max(), pt.buffer(450), scale=450, maxPixels=1e9, bestEffort=True).get("b1"),
                "twi_hs": twi_hydrosheds.reduceRegion(ee.Reducer.mean(), pt.buffer(450), scale=450, maxPixels=1e9, bestEffort=True).get("twi_hs"),
                "twi_merit": twi_merit.reduceRegion(ee.Reducer.mean(), pt.buffer(90), scale=90, maxPixels=1e9, bestEffort=True).get("twi_merit"),
            }).getInfo()
        except Exception as exc:
            print(f"[ERROR] {name}: {exc!r}", flush=True)
            continue
        combined.update(base)

        for w in WINDOWS_M:
            focal_mean_img = dem.focal_mean(radius=w, units="meters")
            depression_img = focal_mean_img.subtract(dem)
            try:
                dep = depression_img.reduceRegion(ee.Reducer.mean(), parcel, scale=30, maxPixels=1e9).get("DEM").getInfo()
            except Exception:
                dep = None
            combined[f"depression_{w}m"] = dep

        rows.append(combined)
        print(
            f"{name} [{group}]: slope={combined.get('slope_deg')} elev={combined.get('elev_m')} "
            f"upa={combined.get('upa_km2')}km2 hs_acc={combined.get('hs_acc')} "
            f"twi_hs={combined.get('twi_hs')} twi_merit={combined.get('twi_merit')} "
            f"dep50={combined.get('depression_50m')} dep100={combined.get('depression_100m')} dep300={combined.get('depression_300m')}",
            flush=True,
        )

    print("\n=== PAIR COMPARISONS (matched same-city/climate pairs) ===", flush=True)
    by_name = {r["name"]: r for r in rows}
    pairs = [
        ("Lekki (Lagos State)", "Ikeja GRA (Lagos State)"),
        ("Kubwa (FCT, Abuja)", "Wuse 2 (FCT, Abuja)"),
        ("Benin City (Edo State)", "Akure (Ondo State)"),
        ("Uyo (Akwa Ibom State)", "Ado-Ekiti (Ekiti State)"),
        ("Calabar (Cross River State)", "Abakaliki (Ebonyi State)"),
        ("Ilorin (Kwara State)", "Offa (Kwara State)"),
    ]
    fields = ["slope_deg", "upa_km2", "hs_acc", "twi_hs", "twi_merit", "depression_50m", "depression_100m", "depression_300m"]
    for flooded_name, control_name in pairs:
        fr, cr = by_name.get(flooded_name), by_name.get(control_name)
        if not fr or not cr:
            continue
        print(f"\n  {flooded_name} vs {control_name}:", flush=True)
        for field in fields:
            print(f"    {field}: {fr.get(field)}  vs  {cr.get(field)}", flush=True)

    print("\n=== AGGREGATE: does each raw metric separate flooded from control across all 6 pairs? ===", flush=True)
    flooded_rows = [r for r in rows if r["group"] == "flooded"]
    control_rows = [r for r in rows if r["group"] == "control"]
    for field in fields:
        f_vals = [r[field] for r in flooded_rows if r.get(field) is not None]
        c_vals = [r[field] for r in control_rows if r.get(field) is not None]
        if not f_vals or not c_vals:
            print(f"  {field}: insufficient data")
            continue
        print(
            f"  {field}: flooded mean={statistics.mean(f_vals):.3f} median={statistics.median(f_vals):.3f} | "
            f"control mean={statistics.mean(c_vals):.3f} median={statistics.median(c_vals):.3f}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
