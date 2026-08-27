"""Derives new Low/Moderate/High/Severe cutoffs for the Floodplain Susceptibility badge, calibrated
to the real distribution of the FROZEN V3.1 score across ordinary Nigerian ground - not a change to
the score itself (formula/weights/normalization constants are untouched, per the V3.1 permanent
record's governance), only to what percentage ranges get called "Low"/"Moderate"/"High"/"Severe".

Root problem (confirmed in scratch/floodplain_v31_calibration_check.py against 10 hand-picked
diverse points): the shared RISK_TIERS cutoffs (Low<25%, Moderate<50%, High<75%, Severe>=75%) were
built for a generic 0-1 score, but V3.1's min-max normalization range was set by a validation
sample's extremes (HAND up to 166m) that essentially never occur in ordinary Nigerian terrain - so
most ordinary sites land in the upper half of the 0-1 scale even though they are not, in reality,
unusually flood-prone relative to other Nigerian sites.

Sampling frame: one point per Nigerian state capital + Abuja (37 points) - a principled,
reproducible, geographically-comprehensive frame (spans coastal/deltaic, savanna, and highland
terrain in roughly the same proportion as the country itself), not a hand-picked sample chosen to
prove a point either way. Uses the exact same HAND/RelElev sampling radii as production
(hazard_floodplain.py: 300m for HAND, 1000m focal-mean for relative elevation), batched via
reduceRegions for one round trip instead of 37 sequential calls.
"""
from __future__ import annotations

import statistics

from dotenv import load_dotenv

load_dotenv()

import ee

from app.utils.gee_client import init_gee
from app.utils.hazard_floodplain import (
    _HAND_ORIENTED_MAX,
    _HAND_ORIENTED_MIN,
    _LOCAL_AREA_RADIUS_M,
    _MERIT_HYDRO_ASSET,
    _MERIT_SCALE_M,
    _RELATIVE_ELEV_RADIUS_M,
    _RELELEV_ORIENTED_MAX,
    _RELELEV_ORIENTED_MIN,
)

init_gee()

# name -> (lng, lat) - state capital (or FCT seat), one per Nigerian state.
STATE_CAPITALS = {
    "Abia (Umuahia)": (7.4951, 5.5252),
    "Adamawa (Yola)": (12.4986, 9.2035),
    "Akwa Ibom (Uyo)": (7.9257, 5.0377),
    "Anambra (Awka)": (7.0706, 6.2120),
    "Bauchi (Bauchi)": (9.8442, 10.3158),
    "Bayelsa (Yenagoa)": (6.2649, 4.9247),
    "Benue (Makurdi)": (8.5391, 7.7322),
    "Borno (Maiduguri)": (13.1571, 11.8333),
    "Cross River (Calabar)": (8.3417, 4.9500),
    "Delta (Asaba)": (6.7333, 6.2000),
    "Ebonyi (Abakaliki)": (8.1137, 6.3249),
    "Edo (Benin City)": (5.6037, 6.3350),
    "Ekiti (Ado-Ekiti)": (5.2200, 7.6211),
    "Enugu (Enugu)": (7.5139, 6.4413),
    "FCT (Abuja)": (7.4913, 9.0765),
    "Gombe (Gombe)": (11.1670, 10.2897),
    "Imo (Owerri)": (7.0257, 5.4840),
    "Jigawa (Dutse)": (9.3400, 11.7564),
    "Kaduna (Kaduna)": (7.4383, 10.5222),
    "Kano (Kano)": (8.5167, 12.0000),
    "Katsina (Katsina)": (7.6000, 12.9908),
    "Kebbi (Birnin Kebbi)": (4.1975, 12.4534),
    "Kogi (Lokoja)": (6.7333, 7.8023),
    "Kwara (Ilorin)": (4.5421, 8.4966),
    "Lagos (Ikeja)": (3.3515, 6.6018),
    "Nasarawa (Lafia)": (8.5167, 8.4833),
    "Niger (Minna)": (6.5569, 9.6139),
    "Ogun (Abeokuta)": (3.3487, 7.1475),
    "Ondo (Akure)": (5.1931, 7.2571),
    "Osun (Osogbo)": (4.5567, 7.7719),
    "Oyo (Ibadan)": (3.9000, 7.4167),
    "Plateau (Jos)": (8.8583, 9.8965),
    "Rivers (Port Harcourt)": (7.0134, 4.8156),
    "Sokoto (Sokoto)": (5.2339, 13.0059),
    "Taraba (Jalingo)": (11.3667, 8.9000),
    "Yobe (Damaturu)": (11.9660, 11.7470),
    "Zamfara (Gusau)": (6.6641, 12.1704),
}


def _normalize(oriented_value: float, lo: float, hi: float) -> float:
    scaled = (oriented_value - lo) / (hi - lo) if hi > lo else 0.5
    return max(0.0, min(1.0, scaled))


names = list(STATE_CAPITALS.keys())
hand_fc = ee.FeatureCollection([
    ee.Feature(ee.Geometry.Point(lng, lat).buffer(_LOCAL_AREA_RADIUS_M), {"name": name})
    for name, (lng, lat) in STATE_CAPITALS.items()
])
# ~plot-scale footprint for the elevation/relative-elevation reads, mirroring how a real (small)
# plot boundary would be sampled in production - not the 300m HAND radius.
elev_fc = ee.FeatureCollection([
    ee.Feature(ee.Geometry.Point(lng, lat).buffer(40), {"name": name})
    for name, (lng, lat) in STATE_CAPITALS.items()
])

merit = ee.Image(_MERIT_HYDRO_ASSET)
hand_img = merit.select("hnd")
dem = ee.ImageCollection("COPERNICUS/DEM/GLO30_2024_1").select("DEM").mosaic()
dem_focal_1000 = dem.focal_mean(radius=_RELATIVE_ELEV_RADIUS_M, units="meters")

print("Sampling HAND over", len(names), "state capitals (one batched call)...")
hand_result = hand_img.reduceRegions(collection=hand_fc, reducer=ee.Reducer.median(), scale=_MERIT_SCALE_M).getInfo()

print("Sampling elevation + 1000m focal-mean elevation (one batched call each)...")
elev_result = dem.reduceRegions(collection=elev_fc, reducer=ee.Reducer.mean(), scale=30).getInfo()
focal_result = dem_focal_1000.reduceRegions(collection=elev_fc, reducer=ee.Reducer.mean(), scale=30).getInfo()

hand_by_name = {f["properties"]["name"]: f["properties"].get("median") for f in hand_result["features"]}
elev_by_name = {f["properties"]["name"]: f["properties"].get("mean") for f in elev_result["features"]}
focal_by_name = {f["properties"]["name"]: f["properties"].get("mean") for f in focal_result["features"]}

rows = []
print(f"\n{'State capital':<26} {'HAND(m)':>8} {'RelElev(m)':>11} {'risk_value':>11}")
print("-" * 60)
for name in names:
    hand_median_m = hand_by_name.get(name)
    elev_m = elev_by_name.get(name)
    focal_1000_m = focal_by_name.get(name)
    if hand_median_m is None or elev_m is None or focal_1000_m is None:
        print(f"{name:<26} MISSING DATA")
        continue
    relative_elev_1000m = float(elev_m) - float(focal_1000_m)
    hand_term = _normalize(-float(hand_median_m), _HAND_ORIENTED_MIN, _HAND_ORIENTED_MAX)
    relelev_term = _normalize(-relative_elev_1000m, _RELELEV_ORIENTED_MIN, _RELELEV_ORIENTED_MAX)
    risk_value = 0.5 * hand_term + 0.5 * relelev_term
    print(f"{name:<26} {hand_median_m:>8.1f} {relative_elev_1000m:>11.2f} {risk_value:>11.3f}")
    rows.append(risk_value)

rows_sorted = sorted(rows)
print("-" * 60)
print(f"N = {len(rows)}")
print(f"Mean = {statistics.mean(rows):.3f}  Median = {statistics.median(rows):.3f}  Stdev = {statistics.pstdev(rows):.3f}")


def pct(p: float) -> float:
    idx = min(len(rows_sorted) - 1, max(0, round(p / 100 * (len(rows_sorted) - 1))))
    return rows_sorted[idx]


print("\nPercentiles of real V3.1 scores across Nigerian state capitals:")
for p in (10, 25, 40, 50, 60, 75, 90, 95):
    print(f"  p{p:>2}: {pct(p):.3f}")

print("\nCurrent shared RISK_TIERS cutoffs: Low<0.25, Moderate<0.50, High<0.75, Severe>=0.75")
print("How many state capitals fall in each CURRENT tier:")
tiers_current = {"Low": 0, "Moderate": 0, "High": 0, "Severe": 0}
for r in rows:
    if r < 0.25:
        tiers_current["Low"] += 1
    elif r < 0.50:
        tiers_current["Moderate"] += 1
    elif r < 0.75:
        tiers_current["High"] += 1
    else:
        tiers_current["Severe"] += 1
print(tiers_current)
