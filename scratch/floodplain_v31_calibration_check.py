"""Live diagnostic: is the deployed V3.1 floodplain score systematically skewed high across
ordinary (not obviously flood-prone) Nigerian locations, as reported ("always returns more than
70% in almost everywhere")?

Samples the EXACT frozen formula/constants from app/utils/hazard_floodplain.py at a geographically
diverse set of real points - including places that should read clearly LOW (highland/plateau towns
with no business scoring as flood-susceptible) - and prints hand_term / relelev_term / risk_value
for each, so the skew (if real) is visible in actual numbers rather than argued from theory.

Does NOT modify anything - a pure read-only measurement.
"""
from __future__ import annotations

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

# Deliberately diverse - not just Niger Delta/known-flood towns. Jos and Obudu are highland
# plateau/resort towns picked BECAUSE they should score clearly low if the calibration is sound.
POINTS = {
    "Jos Plateau (highland, ~1200m ASL)": (8.8583, 9.8965),
    "Obudu Plateau (highland resort)": (9.3986, 6.6161),
    "Abuja - Asokoro hills (upmarket, elevated)": (7.5322, 9.0430),
    "Kano city (flat northern plain, semi-arid)": (8.5167, 12.0000),
    "Ibadan - Bodija (typical inland urban)": (3.9000, 7.4167),
    "Lagos mainland - Yaba (low-lying, urban)": (3.3792, 6.5083),
    "Port Harcourt (Niger Delta, genuinely low-lying)": (7.0134, 4.8156),
    "Ogbaru (River Niger floodplain - prior test site)": (6.7889, 6.1500),
    "Kaduna city (mid-elevation plateau town)": (7.4383, 10.5222),
    "Enugu (hilly southeastern town)": (7.5139, 6.4413),
}


def _normalize(oriented_value: float, lo: float, hi: float) -> float:
    scaled = (oriented_value - lo) / (hi - lo) if hi > lo else 0.5
    return max(0.0, min(1.0, scaled))


merit = ee.Image(_MERIT_HYDRO_ASSET)
hand_img = merit.select("hnd")
dem = ee.ImageCollection("COPERNICUS/DEM/GLO30_2024_1").select("DEM").mosaic()
dem_focal_1000 = dem.focal_mean(radius=_RELATIVE_ELEV_RADIUS_M, units="meters")

print(f"{'Location':<48} {'HAND(m)':>8} {'RelElev(m)':>11} {'hand_term':>10} {'relelev_term':>13} {'risk_value':>11}")
print("-" * 105)

rows = []
for name, (lng, lat) in POINTS.items():
    pt = ee.Geometry.Point([lng, lat])
    local_area = pt.buffer(_LOCAL_AREA_RADIUS_M)
    combined = ee.Dictionary({
        "hand_median": hand_img.reduceRegion(ee.Reducer.median(), local_area, scale=_MERIT_SCALE_M, maxPixels=1e9, bestEffort=True).get("hnd"),
        "elev_m": dem.reduceRegion(ee.Reducer.mean(), pt, scale=30, maxPixels=1e9, bestEffort=True).get("DEM"),
        "focal_1000_m": dem_focal_1000.reduceRegion(ee.Reducer.mean(), pt, scale=30, maxPixels=1e9, bestEffort=True).get("DEM"),
    }).getInfo()

    hand_median_m = combined.get("hand_median")
    elev_m = combined.get("elev_m")
    focal_1000_m = combined.get("focal_1000_m")
    if hand_median_m is None or elev_m is None or focal_1000_m is None:
        print(f"{name:<48} MISSING DATA: {combined}")
        continue

    relative_elev_1000m = float(elev_m) - float(focal_1000_m)
    hand_term = _normalize(-float(hand_median_m), _HAND_ORIENTED_MIN, _HAND_ORIENTED_MAX)
    relelev_term = _normalize(-relative_elev_1000m, _RELELEV_ORIENTED_MIN, _RELELEV_ORIENTED_MAX)
    risk_value = 0.5 * hand_term + 0.5 * relelev_term

    print(f"{name:<48} {hand_median_m:>8.1f} {relative_elev_1000m:>11.2f} {hand_term:>10.3f} {relelev_term:>13.3f} {risk_value:>11.3f}")
    rows.append(risk_value)

if rows:
    print("-" * 105)
    print(f"Mean risk_value across {len(rows)} diverse points: {sum(rows)/len(rows):.3f}")
    print(f"Min: {min(rows):.3f}  Max: {max(rows):.3f}")
    print(f"Fraction scoring >= 0.70 (the 'always High' complaint threshold): {sum(1 for r in rows if r >= 0.70)}/{len(rows)}")
