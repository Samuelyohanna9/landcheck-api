"""INVESTIGATION ONLY - not wired into any production code path. Does not change
compute_pluvial_risk, compute_flood_risk, or any weight/threshold. Answers two questions raised
after the historical-flood diagnostic study:

1. Does a real topographic-wetness/contributing-area signal (not just local flatness+depression)
   separate Bodija (control) from Ogunpa (flooded corridor) - same city, same regional rainfall?
2. Does Height Above Nearest Drainage (HAND) or MERIT Hydro's upstream drainage area flag Ogbaru
   (a real River Niger floodplain town with zero GloFAS coverage) as fluvially susceptible, where
   both existing engines currently miss it?

Datasets investigated (all confirmed present in the GEE catalog via live check below before use):
  - WWF/HydroSHEDS/15DIR + /15ACC (drainage direction + flow accumulation, ~450m/15 arc-sec) -
    already partially used by hazard_pluvial.py's local-drainage-distance component; here also
    used to build a genuine TWI = ln(contributing_area_m2 / tan(slope)) proxy.
  - MERIT/Hydro/v1_0_1 (elv, dir, upa, upg, wat bands, ~90m/3 arc-sec) - a finer, more
    hydrologically-conditioned alternative to HydroSHEDS, with upa = upstream drainage area (km2)
    and wat = a real mapped river-channel mask (not a flow-accumulation-threshold proxy).
  - users/gena/GlobalHAND/90m-global/hand-1000 - a COMMUNITY (non-Google-curated) asset hosted
    under a personal GEE namespace, CC-BY 4.0 licensed (commercial use OK with attribution) per
    the awesome-gee-community-catalog listing. Community/personal-namespace assets carry a real
    availability risk Google-curated catalog datasets don't (can be deleted/moved without notice)
    - flagged here, not treated as production-ready.

Run: python scratch/test_drainage_dataset_investigation.py
"""
import math
import os
import sys

sys.path.insert(0, os.path.abspath("."))
from dotenv import load_dotenv
load_dotenv()

import ee
from app.utils.gee_client import init_gee

SITES = [
    ("Lokoja (Niger-Benue confluence)", 7.8023, 6.7333, "flooded"),
    ("Port Harcourt (Niger Delta)", 4.8156, 7.0498, "flooded"),
    ("Maiduguri (Alau Dam failure area)", 11.8333, 13.1500, "flooded"),
    ("Makurdi (River Benue)", 7.7322, 8.5391, "flooded"),
    ("Yenagoa (Niger Delta)", 4.9247, 6.2642, "flooded"),
    ("Ogbaru (River Niger, Anambra)", 6.1667, 6.7500, "flooded"),
    ("Ibadan (Ogunpa River corridor)", 7.3775, 3.8880, "flooded"),
    ("Lagos (Ajegunle)", 6.4550, 3.3260, "flooded"),
    ("Mokwa (Niger State)", 9.2937, 5.0653, "flooded"),
    ("Numan (River Benue, Adamawa)", 9.4667, 12.0333, "flooded"),
    ("Hadejia (Jigawa)", 12.4500, 10.0333, "flooded"),
    ("Jos (Plateau highland)", 9.8965, 8.8583, "control"),
    ("Abuja - Asokoro/Maitama", 9.0479, 7.5289, "control"),
    ("Kano (old city core)", 12.0022, 8.5920, "control"),
    ("Enugu (Independence Layout)", 6.4413, 7.4988, "control"),
    ("Sokoto (city center)", 13.0059, 5.2476, "control"),
    ("Ibadan - Bodija GRA", 7.4270, 3.9080, "control"),
    ("Obudu Plateau", 6.7500, 9.3500, "control"),
    ("Mambilla Plateau", 6.9500, 11.3500, "control"),
]


def _verify_assets():
    print("=== Live dataset verification ===")
    checks = [
        ("WWF/HydroSHEDS/15DIR", "Image"),
        ("WWF/HydroSHEDS/15ACC", "Image"),
        ("MERIT/Hydro/v1_0_1", "Image"),
        ("users/gena/GlobalHAND/90m-global/hand-1000", "Image"),
    ]
    ok = {}
    for asset_id, kind in checks:
        try:
            img = ee.Image(asset_id)
            bands = img.bandNames().getInfo()
            print(f"  [OK] {asset_id}: bands={bands}")
            ok[asset_id] = True
        except Exception as exc:
            print(f"  [FAIL] {asset_id}: {exc!r}")
            ok[asset_id] = False
    return ok


def main() -> int:
    init_gee()
    availability = _verify_assets()
    if not availability.get("MERIT/Hydro/v1_0_1") or not availability.get("users/gena/GlobalHAND/90m-global/hand-1000"):
        print("\nOne or more datasets unavailable - stopping before wasting further calls.")
        return 1

    merit = ee.Image("MERIT/Hydro/v1_0_1")
    merit_upa = merit.select("upa")  # upstream drainage area, km^2
    merit_wat = merit.select("wat")  # river channel mask
    hand_img = ee.Image("users/gena/GlobalHAND/90m-global/hand-1000")
    hand_band = hand_img.bandNames().getInfo()[0]

    flow_acc = ee.Image("WWF/HydroSHEDS/15ACC").select("b1")
    dem = ee.ImageCollection("COPERNICUS/DEM/GLO30_2024_1").select("DEM").mosaic()
    slope_deg = ee.Terrain.slope(dem)
    slope_rad = slope_deg.multiply(math.pi / 180.0)
    cell_area_m2 = flow_acc.projection().nominalScale().pow(2)
    contributing_area_m2 = flow_acc.multiply(cell_area_m2)
    # TWI = ln(As / tan(beta)); tan(beta) floored to avoid log(inf) on perfectly flat pixels.
    twi_img = contributing_area_m2.divide(slope_rad.tan().max(0.001)).log().rename("twi")

    # Distance to a REAL mapped river channel (MERIT wat mask), not a flow-accumulation threshold -
    # a genuinely different signal from hazard_pluvial.py's existing local-drainage-distance.
    river_mask = merit_wat.gt(0)
    river_dist_px = river_mask.fastDistanceTransform(100)
    river_dist_m = river_dist_px.sqrt().multiply(merit_wat.projection().nominalScale()).rename("river_dist_m")

    print("\n=== Per-site drainage/convergence signals ===")
    rows = []
    for name, lat, lon, group in SITES:
        pt = ee.Geometry.Point([lon, lat])
        region = pt.buffer(500)
        try:
            combined = ee.Dictionary({
                "twi": twi_img.reduceRegion(ee.Reducer.mean(), region, scale=450, maxPixels=1e9, bestEffort=True).get("twi"),
                "upa_km2": merit_upa.reduceRegion(ee.Reducer.mean(), region, scale=90, maxPixels=1e9, bestEffort=True).get("upa"),
                "hand_m": hand_img.reduceRegion(ee.Reducer.mean(), region, scale=90, maxPixels=1e9, bestEffort=True).get(hand_band),
                "river_dist_m": river_dist_m.reduceRegion(ee.Reducer.mean(), region, scale=90, maxPixels=1e9, bestEffort=True).get("river_dist_m"),
            }).getInfo()
        except Exception as exc:
            print(f"  [ERROR] {name}: {exc!r}")
            continue
        row = {"name": name, "group": group, **combined}
        rows.append(row)
        twi = combined.get("twi")
        upa = combined.get("upa_km2")
        hand = combined.get("hand_m")
        rdist = combined.get("river_dist_m")
        print(
            f"  {name} [{group}]: TWI={twi:.2f}" if twi is not None else f"  {name} [{group}]: TWI=None",
            f"| upstream_area={upa:.1f}km2" if upa is not None else "| upstream_area=None",
            f"| HAND={hand:.1f}m" if hand is not None else "| HAND=None",
            f"| river_dist={rdist:.0f}m" if rdist is not None else "| river_dist=None",
        )

    print("\n=== NAMED PAIR COMPARISONS (drainage/convergence signals) ===")
    by_name = {r["name"]: r for r in rows}
    pairs = [
        ("Abuja - Asokoro/Maitama", "Makurdi (River Benue)"),
        ("Ibadan - Bodija GRA", "Ibadan (Ogunpa River corridor)"),
        ("Jos (Plateau highland)", "Lokoja (Niger-Benue confluence)"),
        ("Ogbaru (River Niger, Anambra)", "Obudu Plateau"),
    ]
    for a, b in pairs:
        if a not in by_name or b not in by_name:
            print(f"  (skipping {a} vs {b} - missing data)")
            continue
        ra, rb = by_name[a], by_name[b]
        print(f"\n  {a} vs {b}:")
        for field, label in [("twi", "TWI"), ("upa_km2", "upstream_area_km2"), ("hand_m", "HAND_m"), ("river_dist_m", "river_dist_m")]:
            print(f"    {label}: {ra.get(field)}  vs  {rb.get(field)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
