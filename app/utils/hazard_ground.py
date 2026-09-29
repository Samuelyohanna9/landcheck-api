from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

import ee
from sqlalchemy.orm import Session

from app.utils.gee_client import init_gee
from app.utils.hazard_common import GROUND_REFERENCES, classify_risk
from app.utils.hazard_local_data import (
    compute_confidence_score,
    derive_hydrologic_soil_group,
    summarize_local_soil_points,
)

# Same OpenLandMap/SoilGrids250m asset IDs hazard_pluvial.py already reads for its Hydrologic
# Soil Group calculation - one real, already-integrated dataset, not a new one for this module.
_SOIL_SAND_ASSET = "OpenLandMap/SOL/SOL_SAND-WFRACTION_USDA-3A1A1A_M/v02"
_SOIL_CLAY_ASSET = "OpenLandMap/SOL/SOL_CLAY-WFRACTION_USDA-3A1A1A_M/v02"
_SOIL_BAND = "b0"
_SOIL_SCALE_M = 250

# A = high infiltration/sandy (best natural drainage) ... D = low infiltration/clayey (poorest
# natural drainage, most prone to waterlogging and expansive-soil behaviour).
_HSG_DRAINAGE_SCORE = {"A": 0.10, "B": 0.40, "C": 0.70, "D": 1.00}

# Shown in both the JSON payload and the PDF, unconditionally - this screening must never be
# mistaken for an actual geotechnical soil test. See the module docstring for why.
GROUND_SCOPE_NOTE = (
    "This is a satellite-based drainage and waterlogging screening, not a soil test. It does not "
    "measure load-bearing capacity, water table depth, or subsurface soil layers. For any "
    "foundation design decision, commission a licensed geotechnical investigation "
    "(boreholes, SPT/CPT testing, and lab soil analysis)."
)


def compute_ground_risk(
    db: Session,
    boundary_geojson: Dict[str, Any],
    show_raster: bool = False,
    local_elevation_points: Optional[List[Dict[str, float]]] = None,
    analysis_mode: str = "hybrid",
    progress_cb: Optional[Callable[[str, int], None]] = None,
) -> Tuple[float, str, Dict[str, Any], Optional[bytes]]:
    """"Ground & Drainage" screening: how likely this site's own natural drainage characteristics
    are to cause waterlogging, using two site-appropriate satellite signals - soil texture
    (drainage capacity, via Hydrologic Soil Group) and proximity to a natural drainage channel
    (a concentrated-flow-path proxy, the same HydroSHEDS computation hazard_erosion.py already
    does for the same reason).

    Deliberately NOT built on hazard_floodplain.py's HAND-based terrain index: that index's
    normalization constants are frozen and calibrated specifically for FLOOD-hazard
    discrimination (see hazard_floodplain.py's own docstring). Reusing them here for a different
    question - ground drainage, not flood risk - without equivalent recalibration would be
    exactly the kind of unjustified-precision problem this honest screening exists to avoid.

    This is explicitly NOT a soil test: it cannot and does not report load-bearing capacity,
    water table depth, or subsurface soil layers - see GROUND_SCOPE_NOTE, always included in the
    breakdown and surfaced in the UI/PDF, unconditionally.

    `db` and `show_raster` are accepted only to match every other hazard module's
    (db, boundary, show_raster, ...) call signature (see hazards.py's erosion_preview /
    flood_preview) - this module makes no database queries and has no raster overlay yet.
    """
    report = progress_cb or (lambda stage, pct: None)
    report("Connecting to Earth Engine...", 5)
    init_gee()

    geom = ee.Geometry(boundary_geojson)
    # Same 500m "near-site" buffer erosion uses - ground drainage is a site/near-site property,
    # not a distant-catchment one.
    analysis_region = geom.buffer(500)

    # --- Soil texture -> Hydrologic Soil Group (drainage capacity) -----------------------------
    report("Reading soil texture data...", 30)
    soil_summary = summarize_local_soil_points(local_elevation_points or []) or {}
    sand_pct: Optional[float] = soil_summary.get("sand_pct")
    clay_pct: Optional[float] = soil_summary.get("clay_pct")
    soil_source = "user_input" if (sand_pct is not None and clay_pct is not None) else "global_soil_texture"
    if sand_pct is None or clay_pct is None:
        try:
            soil_combined = ee.Dictionary({
                "sand_pct": ee.Image(_SOIL_SAND_ASSET).select(_SOIL_BAND).reduceRegion(
                    reducer=ee.Reducer.mean(), geometry=analysis_region, scale=_SOIL_SCALE_M, maxPixels=1e9,
                ).get(_SOIL_BAND),
                "clay_pct": ee.Image(_SOIL_CLAY_ASSET).select(_SOIL_BAND).reduceRegion(
                    reducer=ee.Reducer.mean(), geometry=analysis_region, scale=_SOIL_SCALE_M, maxPixels=1e9,
                ).get(_SOIL_BAND),
            }).getInfo()
            sand_pct = soil_combined.get("sand_pct")
            clay_pct = soil_combined.get("clay_pct")
        except Exception:
            sand_pct = clay_pct = None
    has_soil_data = sand_pct is not None and clay_pct is not None
    hydrologic_soil_group = derive_hydrologic_soil_group(sand_pct, clay_pct) if has_soil_data else "B"
    soil_drainage_score = _HSG_DRAINAGE_SCORE.get(hydrologic_soil_group, 0.4)

    # --- Distance to nearest natural drainage channel (HydroSHEDS flow accumulation) -----------
    report("Analyzing drainage network...", 60)
    mean_dist_val = 5000.0
    has_drainage_data = False
    try:
        flow_acc = ee.Image("WWF/HydroSHEDS/15ACC").select("b1")
        channels = flow_acc.gt(1000)
        channel_dist = channels.fastDistanceTransform(30).sqrt()
        distance_m = channel_dist.multiply(flow_acc.projection().nominalScale()).rename("distance_m")
        dist_result = distance_m.reduceRegion(
            reducer=ee.Reducer.mean(), geometry=geom, scale=463, maxPixels=1e9,
        ).get("distance_m").getInfo()
        if dist_result is not None:
            mean_dist_val = float(dist_result)
            has_drainage_data = True
    except Exception:
        pass
    drainage_proximity_score = max(0.0, min(1.0, 1.0 - (mean_dist_val / 500.0)))

    has_data = has_soil_data or has_drainage_data

    report("Scoring drainage risk...", 80)
    factor_weights = {"soil_drainage": 0.6, "drainage_proximity": 0.4}
    risk_value = (
        soil_drainage_score * factor_weights["soil_drainage"]
        + drainage_proximity_score * factor_weights["drainage_proximity"]
    )
    if not has_data:
        risk_value = 0.0

    factor_sources = {
        "soil_drainage": soil_source if has_soil_data else "not_available",
        "drainage_proximity": "satellite_hydrosheds" if has_drainage_data else "not_available",
    }

    try:
        plot_area_ha = float(geom.area(1).getInfo()) / 10000.0
    except Exception:
        plot_area_ha = 0.0
    local_soil_point_count = len([p for p in (local_elevation_points or []) if p.get("sand_pct") is not None])
    confidence = compute_confidence_score(
        factor_sources, factor_weights, local_point_count=local_soil_point_count, plot_area_ha=plot_area_ha,
    )

    breakdown: Dict[str, Any] = {
        "hydrologic_soil_group": hydrologic_soil_group,
        "sand_pct": round(float(sand_pct), 1) if sand_pct is not None else None,
        "clay_pct": round(float(clay_pct), 1) if clay_pct is not None else None,
        "distance_to_drainage_m": round(mean_dist_val, 1),
        "soil_drainage_score": round(soil_drainage_score, 3),
        "drainage_proximity_score": round(drainage_proximity_score, 3),
        "data_available": has_data,
        "soil_source": soil_source if has_soil_data else "unavailable",
        "analysis_mode": analysis_mode,
        "data_sources": factor_sources,
        "confidence": confidence,
        "scope_note": GROUND_SCOPE_NOTE,
        "_references": GROUND_REFERENCES,
    }

    risk_class, _class_color = classify_risk(risk_value, has_data)

    # No custom map overlay for this first pass (see hazard_map_renderer.py for how the other
    # hazard types build one, if this is worth adding later) - value_points/value_key are still
    # populated so a future GIS export has somewhere to put real point data.
    breakdown["_gis_export"] = {
        "boundary_geojson": boundary_geojson,
        "buildings_gdf": None,
        "value_points": None,
        "value_key": "ground_risk_pct",
    }
    breakdown["_interactive"] = None
    breakdown["buildings_total"] = 0
    breakdown["buildings_threatened"] = 0

    report("Finalizing report...", 95)
    return risk_value, risk_class, breakdown, None
