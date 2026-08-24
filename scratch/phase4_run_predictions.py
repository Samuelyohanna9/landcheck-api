"""Phase 4 PREDICTION LOCKING - runs the frozen, unmodified V2 combine function against BOTH
independent Phase 4 sets (fluvial, pluvial), writing separate predictions files for each so they
stay genuinely independent per instruction. No group/flooded-control label is written into either
predictions file - only what the model actually computed. Each file is hashed and timestamped
immediately after writing.

Does not modify any V2 file. Read-only calls into the frozen production compute functions.

Run: python scratch/phase4_run_predictions.py
"""
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath("."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv()

from app.db import SessionLocal
from app.routers.hazards import _compute_combined_flood
from phase4_fluvial_locations import LOCATIONS as FLUVIAL_LOCATIONS
from phase4_pluvial_locations import LOCATIONS as PLUVIAL_LOCATIONS

SITE_PARAMS = {"site_type": None, "design_rainfall_mm": None, "analysis_mode": "hybrid"}


def _square_boundary(lat: float, lon: float, half_width_deg: float = 0.00035) -> dict:
    d = half_width_deg
    return {
        "type": "Polygon",
        "coordinates": [[
            [lon - d, lat - d], [lon + d, lat - d], [lon + d, lat + d], [lon - d, lat + d], [lon - d, lat - d],
        ]],
    }


def _run_set(db, locations, label):
    predictions = []
    print(f"\n=== {label} ({len(locations)} locations) ===", flush=True)
    for loc in locations:
        name, lat, lon = loc["name"], loc["lat"], loc["lon"]
        boundary = _square_boundary(lat, lon)
        try:
            river_r, floodplain_r, pluvial_r, overall = _compute_combined_flood(
                db, boundary, False, 100, None, SITE_PARAMS,
            )
        except Exception as exc:
            print(f"[ERROR] {name}: {exc!r}", flush=True)
            predictions.append({"name": name, "lat": lat, "lon": lon, "error": repr(exc)})
            continue

        river_risk, river_class, river_b, _ = river_r
        floodplain_risk, floodplain_class, floodplain_b, _ = floodplain_r
        pluvial_risk, pluvial_class, pluvial_b, _ = pluvial_r

        record = {
            "name": name, "lat": lat, "lon": lon,
            "river": {
                "risk_value": round(river_risk, 4), "risk_class": river_class,
                "data_available": bool(river_b.get("data_available")),
                "mean_depth_m": river_b.get("mean_depth_m"), "max_depth_m": river_b.get("max_depth_m"),
                "inundation_fraction": river_b.get("inundation_fraction"),
                "distance_to_river_m": river_b.get("distance_to_river_m"),
            },
            "floodplain": {
                "risk_value": round(floodplain_risk, 4), "risk_class": floodplain_class,
                "data_available": bool(floodplain_b.get("data_available")),
                "hand_median_m": floodplain_b.get("hand_median_m"), "hand_p10_m": floodplain_b.get("hand_p10_m"),
                "distance_to_major_river_m": floodplain_b.get("distance_to_major_river_m"),
                "upstream_area_km2": floodplain_b.get("upstream_area_km2"),
            },
            "pluvial": {
                "risk_value": round(pluvial_risk, 4), "risk_class": pluvial_class,
                "terrain_score": pluvial_b.get("terrain_score"), "runoff_score": pluvial_b.get("runoff_score"),
                "design_rainfall_mm": pluvial_b.get("design_rainfall_mm"),
                "impervious_fraction_pct": pluvial_b.get("impervious_fraction_pct"),
                "neighborhood_impervious_fraction_pct": pluvial_b.get("neighborhood_impervious_fraction_pct"),
                "hydrologic_soil_group": pluvial_b.get("hydrologic_soil_group"),
            },
            "overall": {
                "risk_value": round(overall["risk_value"], 4), "risk_class": overall["risk_class"],
                "primary_driver": overall["primary_driver"],
                "data_available": bool(overall["data_available"]),
            },
        }
        predictions.append(record)
        print(
            f"{name}: overall={record['overall']['risk_value']:.3f}/{record['overall']['risk_class']} "
            f"(driver={record['overall']['primary_driver']}) | river={record['river']['risk_value']:.3f}"
            f"{'*no-coverage*' if not record['river']['data_available'] else ''} | "
            f"floodplain={record['floodplain']['risk_value']:.3f} | pluvial={record['pluvial']['risk_value']:.3f}",
            flush=True,
        )
    return predictions


def _write_and_hash(predictions, out_name):
    out_path = os.path.join(os.path.dirname(__file__), out_name)
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "v2_frozen": True,
        "predictions": predictions,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    with open(out_path, "rb") as f:
        sha256 = hashlib.sha256(f.read()).hexdigest()
    print(f"\n{out_name}: generated_at={payload['generated_at_utc']} SHA256={sha256} n={len(predictions)}", flush=True)


def main() -> int:
    db = SessionLocal()
    try:
        fluvial_predictions = _run_set(db, FLUVIAL_LOCATIONS, "FLUVIAL VALIDATION SET")
        pluvial_predictions = _run_set(db, PLUVIAL_LOCATIONS, "PLUVIAL VALIDATION SET")
    finally:
        db.close()

    _write_and_hash(fluvial_predictions, "phase4_fluvial_predictions.json")
    _write_and_hash(pluvial_predictions, "phase4_pluvial_predictions.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
