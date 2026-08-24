"""DIAGNOSTIC ONLY - does not change, tune, or optimize any weight, threshold, CHIRPS percentile,
or classification boundary in the production engine. Freeze remains in effect.

Reuses the exact same 19-site set from test_historical_flood_validation.py and dumps every raw
input, normalized component, and weighted contribution compute_pluvial_risk() produces, so the
false-positive (Jos/Abuja/Kano/Enugu/Bodija) and false-negative (Ogbaru) patterns found by that
study can be diagnosed component-by-component instead of guessed at.

Run: python scratch/test_pluvial_diagnostic.py
"""
import math
import os
import sys

sys.path.insert(0, os.path.abspath("."))
from dotenv import load_dotenv
load_dotenv()

from app.db import SessionLocal
from app.utils.hazard_pluvial import compute_pluvial_risk

# (name, lat, lon, group) - group is "flooded" or "control", matching the prior study exactly.
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

SITE_PARAMS = {"site_type": None, "design_rainfall_mm": None, "analysis_mode": "hybrid"}


def _square_boundary(lat: float, lon: float, half_width_deg: float = 0.00035) -> dict:
    d = half_width_deg
    return {
        "type": "Polygon",
        "coordinates": [[
            [lon - d, lat - d], [lon + d, lat - d], [lon + d, lat + d], [lon - d, lat + d], [lon - d, lat - d],
        ]],
    }


def _pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return float("nan")
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    denom = math.sqrt(vx * vy)
    return cov / denom if denom else float("nan")


def main() -> int:
    db = SessionLocal()
    rows = []
    try:
        for name, lat, lon, group in SITES:
            boundary = _square_boundary(lat, lon)
            risk_value, risk_class, b, _png = compute_pluvial_risk(
                db, boundary, local_elevation_points=None,
                site_type=SITE_PARAMS["site_type"], design_rainfall_mm=SITE_PARAMS["design_rainfall_mm"],
                analysis_mode=SITE_PARAMS["analysis_mode"],
            )
            scs = b.get("scs_runoff") or {}
            rows.append({
                "name": name, "group": group,
                "risk_value": risk_value, "risk_class": risk_class,
                "design_rainfall_mm": b.get("design_rainfall_mm"),
                "hsg": b.get("hydrologic_soil_group"),
                "curve_number": scs.get("curve_number"),
                "runoff_mm": scs.get("runoff_mm"),
                "runoff_coefficient": b.get("runoff_coefficient"),
                "impervious_pct": b.get("impervious_fraction_pct"),
                "terrain_slope_deg": b.get("terrain_slope_deg"),
                "terrain_depression_m": b.get("terrain_depression_m"),
                "distance_to_drainage_m": b.get("distance_to_drainage_m"),
                "terrain_score": b.get("terrain_score"),
                "runoff_score": b.get("runoff_score"),
                "impervious_score": b.get("impervious_score"),
                "susceptibility_pct": b.get("susceptibility_pct"),
                "site_type_used": b.get("site_type_used"),
            })
            terrain_contrib = round((b.get("terrain_score") or 0) * 0.35, 3)
            runoff_contrib = round((b.get("runoff_score") or 0) * 0.35, 3)
            impervious_contrib = round((b.get("impervious_score") or 0) * 0.30, 3)
            print(
                f"{name} [{group}]: FINAL={risk_value:.3f} ({risk_class}) | "
                f"terrain={b.get('terrain_score'):.3f} (contrib {terrain_contrib:+.3f}, slope={b.get('terrain_slope_deg')}deg, "
                f"depression={b.get('terrain_depression_m')}m, drain_dist={b.get('distance_to_drainage_m')}m) | "
                f"runoff={b.get('runoff_score'):.3f} (contrib {runoff_contrib:+.3f}, CN={scs.get('curve_number')}, "
                f"HSG={b.get('hydrologic_soil_group')}, rain_p99={b.get('design_rainfall_mm')}mm, runoff={scs.get('runoff_mm')}mm) | "
                f"impervious={b.get('impervious_score'):.3f} (contrib {impervious_contrib:+.3f}, "
                f"built={b.get('impervious_fraction_pct')}%, site_type={b.get('site_type_used')})"
            )
    finally:
        db.close()

    print("\n=== CORRELATIONS vs final pluvial risk_value (n={}) ===".format(len(rows)))
    risk_vals = [r["risk_value"] for r in rows]
    for field, label in [
        ("terrain_score", "terrain_score"),
        ("runoff_score", "runoff_score"),
        ("impervious_score", "impervious_score (built fraction)"),
        ("design_rainfall_mm", "CHIRPS P99 design rainfall"),
        ("terrain_depression_m", "terrain depression (m)"),
        ("terrain_slope_deg", "terrain slope (deg)"),
        ("distance_to_drainage_m", "distance to local drainage (m)"),
    ]:
        vals = [r[field] for r in rows if r[field] is not None]
        paired_risk = [r["risk_value"] for r in rows if r[field] is not None]
        r = _pearson(paired_risk, vals)
        print(f"  {label}: r = {r:+.3f}")

    print("\n=== NAMED PAIR COMPARISONS ===")
    by_name = {r["name"]: r for r in rows}
    pairs = [
        ("Abuja - Asokoro/Maitama", "Makurdi (River Benue)"),
        ("Ibadan - Bodija GRA", "Ibadan (Ogunpa River corridor)"),
        ("Jos (Plateau highland)", "Lokoja (Niger-Benue confluence)"),
        ("Ogbaru (River Niger, Anambra)", "Obudu Plateau"),
    ]
    for a, b_name in pairs:
        ra, rb = by_name[a], by_name[b_name]
        print(f"\n  {a} ({ra['risk_value']:.3f}) vs {b_name} ({rb['risk_value']:.3f}):")
        for field, label in [
            ("terrain_score", "terrain"), ("runoff_score", "runoff"), ("impervious_score", "impervious"),
            ("design_rainfall_mm", "rain_p99_mm"), ("impervious_pct", "built_%"),
            ("terrain_depression_m", "depression_m"), ("terrain_slope_deg", "slope_deg"),
            ("distance_to_drainage_m", "drain_dist_m"), ("curve_number", "CN"),
        ]:
            print(f"    {label}: {ra[field]}  vs  {rb[field]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
