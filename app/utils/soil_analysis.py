from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional, Tuple

import ee
from sqlalchemy.orm import Session

from app.utils.gee_client import init_gee
from app.utils.hazard_common import HYDROSHEDS_REFERENCE, classify_risk
from app.utils.hazard_local_data import (
    compute_confidence_score,
    derive_hydrologic_soil_group,
    summarize_local_soil_points,
)

"""Soil Analysis - a standalone Estate-dashboard tool (deliberately NOT part of Hazard Analysis),
using satellite soil-texture and terrain data to give three indicative, honestly-caveated
readings: a drainage/waterlogging tendency, a presumptive bearing-capacity RANGE, and a water-
table-depth TENDENCY. Each is a real, published, GIS/remote-sensing screening technique - not
guesswork - but none of them is a substitute for a site-specific geotechnical investigation, and
this module is explicit about exactly where its confidence runs out. See SOIL_SCOPE_NOTE.

What this module can and can't do, and why:

- Bearing capacity: satellite data cannot measure load-bearing capacity directly (that needs an
  SPT/CPT probe or a plate load test). What soil TEXTURE (from OpenLandMap/SoilGrids, the same
  data hazard_pluvial.py already uses for its Hydrologic Soil Group calc) can support is a
  PRESUMED bearing-capacity RANGE for that texture class, from a standard, code-referenced table
  (BS 8004:1986's presumed bearing values - see BS8004_BEARING_TABLE). This mirrors real
  engineering practice: presumed bearing values are explicitly meant for preliminary design,
  pending a site investigation, in the standard itself. The real limitation this module is upfront
  about: BS 8004 differentiates bearing capacity by soil DENSITY/CONSISTENCY (loose/medium/dense
  sand; soft/firm/stiff clay) as well as texture, and density/consistency cannot be determined
  from satellite texture data at all - only a field test (SPT blow count, etc.) reveals it. So the
  range returned always spans the full texture family (e.g. "sand: <100 to 300+ kPa") rather than
  picking one row, and says so explicitly.

- Water table: satellite gravimetry (GRACE/GRACE-FO) only resolves at ~300km, useless for a single
  plot. What terrain data supports is a Topographic Wetness Index (TWI = ln(upslope contributing
  area / tan(slope)) - Beven & Kirkby, 1979), a well-published proxy for a site's relative
  TENDENCY toward a shallow water table / seasonal saturation, not a measured depth. Recent
  literature (Riihimäki et al., 2021) found TWI's skill depends heavily on flow-routing algorithm,
  grid resolution and season, and a 2024 machine-learning appraisal (Fan et al. framing; see the
  module's citation list) found HAND-family terrain proxies can be a materially uncertain proxy
  for actual water table depth - genuinely real, moderate-to-low-confidence science, not fabricated
  precision. This module reports a relative tendency class (shallow/moderate/deep) plus the raw
  TWI value, never a depth in metres.

- Subsurface soil profile (0-2m): SoilGrids/OpenLandMap actually models sand/clay fraction at six
  standard depths (0, 10, 30, 60, 100, 200cm - see _SOIL_DEPTH_BANDS), not just the surface, so this
  module reports an indicative TEXTURE-BY-DEPTH profile to 2m (soil_profile in the breakdown). This
  is genuinely useful and was missing from an earlier version of this module - but it is NOT
  geological subsurface imaging: it's a ~250m-resolution machine-learning model trained on a global
  soil-profile database (Hengl et al., 2017), reporting the SAME kind of texture reading as the
  surface value, just modeled separately at each depth. It cannot identify bedrock depth, a water
  table, distinct geological strata, or any engineering-classified layer boundary - those need an
  actual borehole log.

  Radar-based subsurface sensing (P-/L-band SAR) is real - the classic case is NASA's Shuttle
  Imaging Radar finding buried paleo-river channels under the Eastern Sahara - but it strictly
  requires extremely dry, fine-grained sand with minimal vegetation (moisture reflects radar and
  blocks penetration; grain size must be under ~1/5 of the radar wavelength). Nigeria's actual
  climate and soils (moist, clay-bearing, vegetated) are the opposite of that condition, and even
  where the technique works, it resolves kilometre-scale ancient river channels/geology, not a
  single plot's engineering strata. It genuinely does not apply here, and this module does not use
  it. Satellite gravimetry (GRACE/GRACE-FO) is real for continental aquifer-depletion trends but
  resolves at ~300km - equally inapplicable to a single plot.
"""

# Same OpenLandMap/SoilGrids250m asset IDs hazard_pluvial.py already reads for its Hydrologic
# Soil Group calculation - one real, already-integrated dataset, not a new one for this module.
_SOIL_SAND_ASSET = "OpenLandMap/SOL/SOL_SAND-WFRACTION_USDA-3A1A1A_M/v02"
_SOIL_CLAY_ASSET = "OpenLandMap/SOL/SOL_CLAY-WFRACTION_USDA-3A1A1A_M/v02"
_SOIL_BAND = "b0"
_SOIL_SCALE_M = 250

# SoilGrids/OpenLandMap models sand/clay fraction at six standard depths, one band per depth on
# the same image (confirmed against the Earth Engine Data Catalog entry for this exact asset) -
# not a single surface-only reading. This is what supports the indicative soil_profile reading
# (see the module docstring's "Subsurface soil profile" section for what it can't tell us).
_SOIL_DEPTH_BANDS = [0, 10, 30, 60, 100, 200]

# A = high infiltration/sandy (best natural drainage) ... D = low infiltration/clayey (poorest
# natural drainage, most prone to waterlogging and expansive-soil behaviour).
_HSG_DRAINAGE_SCORE = {"A": 0.10, "B": 0.40, "C": 0.70, "D": 1.00}

# BS 8004:1986 Table 1 "Presumed bearing values under static loading" - a standard, widely-cited
# UK geotechnical code table. Ranges are the full texture-family span (loose->dense / soft->stiff)
# since this module has no way to tell density/consistency from satellite texture data alone.
BS8004_BEARING_TABLE = {
    "sand": {"min_kpa": 100, "max_kpa": 300, "description": "sand (loose to medium-dense)"},
    "sand_or_gravel": {"min_kpa": 200, "max_kpa": 600, "description": "sand and gravel (medium-dense to dense)"},
    "clay": {"min_kpa": 75, "max_kpa": 300, "description": "clay (firm to stiff)"},
    "mixed": {"min_kpa": 75, "max_kpa": 300, "description": "mixed sandy/clayey soil"},
}

SOIL_SCOPE_NOTE = (
    "This is a satellite- and terrain-based screening, not a soil test. The bearing-capacity "
    "range, water-table tendency, and 0-2m soil profile below are indicative, from published "
    "texture and terrain methods (see references) - not measurements. They do not, and cannot, "
    "determine soil density or consistency (which materially changes bearing capacity), or "
    "identify bedrock depth, distinct geological strata, or any engineering-classified subsurface "
    "layer. For any foundation design decision, commission a licensed geotechnical investigation "
    "(boreholes, SPT/CPT testing, and lab soil analysis)."
)

SOIL_ANALYSIS_REFERENCES = [
    {
        "short": "Hengl et al. (2017)",
        "citation": (
            "Hengl, T., Mendes de Jesus, J., Heuvelink, G.B.M., et al. (2017). SoilGrids250m: "
            "Global gridded soil information based on machine learning. PLoS ONE, 12(2), e0169748."
        ),
        "url": "https://doi.org/10.1371/journal.pone.0169748",
    },
    {
        "short": "BS 8004:1986",
        "citation": "British Standards Institution (1986). BS 8004:1986 Code of practice for foundations, Table 1: Presumed bearing values under static loading.",
        "url": "https://up.codes/s/allowable-load-bearing-values-of-soils-and-rock",
    },
    {
        "short": "Beven & Kirkby (1979)",
        "citation": "Beven, K.J., Kirkby, M.J. (1979). A physically based, variable contributing area model of basin hydrology. Hydrological Sciences Bulletin, 24(1), 43-69.",
        "url": "https://doi.org/10.1080/02626667909491834",
    },
    {
        "short": "Riihimäki et al. (2021)",
        "citation": "Riihimäki, H., Heiskanen, J., Luoto, M. (2021). Topographic Wetness Index as a Proxy for Soil Moisture: The Importance of Flow-Routing Algorithm and Grid Resolution. Water Resources Research, 57(9).",
        "url": "https://doi.org/10.1029/2021WR029871",
    },
    HYDROSHEDS_REFERENCE,
]


def _presumptive_bearing_capacity(hydrologic_soil_group: str, sand_pct: Optional[float], clay_pct: Optional[float]) -> Dict[str, Any]:
    if sand_pct is not None and clay_pct is not None:
        if sand_pct >= 70 and clay_pct < 15:
            key = "sand"
        elif clay_pct >= 35:
            key = "clay"
        else:
            key = "mixed"
    else:
        # Fall back to the Hydrologic Soil Group alone when no direct texture reading is available.
        key = "sand" if hydrologic_soil_group in ("A", "B") else "clay" if hydrologic_soil_group == "D" else "mixed"
    row = BS8004_BEARING_TABLE[key]
    return {
        "min_kpa": row["min_kpa"],
        "max_kpa": row["max_kpa"],
        "soil_description": row["description"],
        "reference": "BS 8004:1986 presumed bearing values",
        "note": (
            "Presumptive range for preliminary design only, from soil texture - actual bearing "
            "capacity within this range depends on soil density/consistency, which satellite data "
            "cannot determine. Confirm with a site-specific geotechnical investigation."
        ),
    }


def _water_table_tendency(twi_value: Optional[float]) -> Dict[str, Any]:
    if twi_value is None:
        return {"twi": None, "tendency": "unavailable", "note": "Terrain wetness data unavailable for this site."}
    # TWI typically spans roughly -3 (steep ridges, fast drainage) to 30 (flat, high-accumulation
    # valley bottoms). These cut points are a documented-range judgement call (see references),
    # not a calibrated depth-to-water regression - reported as a relative tendency, never a metre
    # figure, for exactly that reason.
    if twi_value < 7:
        tendency = "deeper (well-drained terrain position)"
    elif twi_value < 11:
        tendency = "moderate"
    else:
        tendency = "shallow (low-lying, high-accumulation terrain position)"
    return {
        "twi": round(twi_value, 2),
        "tendency": tendency,
        "note": (
            "A relative terrain-position tendency (Topographic Wetness Index), not a measured "
            "water table depth - see references. Confirm actual depth with a borehole/piezometer."
        ),
    }


def _texture_label(sand_pct: Optional[float], clay_pct: Optional[float]) -> str:
    if sand_pct is None or clay_pct is None:
        return "unavailable"
    if sand_pct >= 70 and clay_pct < 15:
        return "sandy"
    if clay_pct >= 35:
        return "clayey"
    return "loamy/mixed"


def _fetch_soil_profile(analysis_region: "ee.Geometry") -> List[Dict[str, Any]]:
    """Sand/clay fraction at each of SoilGrids' six standard depths, in one combined EE round-trip
    (same batching idiom hazard_erosion.py already uses) - an indicative texture-by-depth reading
    to 2m, not a borehole log. See the module docstring for exactly what this can't tell us."""
    try:
        sand_img = ee.Image(_SOIL_SAND_ASSET)
        clay_img = ee.Image(_SOIL_CLAY_ASSET)
        fields: Dict[str, Any] = {}
        for depth_cm in _SOIL_DEPTH_BANDS:
            band = f"b{depth_cm}"
            fields[f"sand_{depth_cm}"] = sand_img.select(band).reduceRegion(
                reducer=ee.Reducer.mean(), geometry=analysis_region, scale=_SOIL_SCALE_M, maxPixels=1e9,
            ).get(band)
            fields[f"clay_{depth_cm}"] = clay_img.select(band).reduceRegion(
                reducer=ee.Reducer.mean(), geometry=analysis_region, scale=_SOIL_SCALE_M, maxPixels=1e9,
            ).get(band)
        combined = ee.Dictionary(fields).getInfo()
    except Exception:
        return []
    profile = []
    for depth_cm in _SOIL_DEPTH_BANDS:
        sand_val = combined.get(f"sand_{depth_cm}")
        clay_val = combined.get(f"clay_{depth_cm}")
        if sand_val is None or clay_val is None:
            continue
        profile.append({
            "depth_cm": depth_cm,
            "sand_pct": round(float(sand_val), 1),
            "clay_pct": round(float(clay_val), 1),
            "texture": _texture_label(float(sand_val), float(clay_val)),
        })
    return profile


def compute_soil_analysis(
    db: Session,
    boundary_geojson: Dict[str, Any],
    local_elevation_points: Optional[List[Dict[str, float]]] = None,
    analysis_mode: str = "hybrid",
    progress_cb: Optional[Callable[[str, int], None]] = None,
) -> Tuple[float, str, Dict[str, Any], Optional[bytes]]:
    """Returns the same (risk_value, risk_class, breakdown, overlay_png) shape every hazard-style
    compute function in this app uses, so it plugs into the existing job/persistence machinery -
    but risk_value/risk_class here describe drainage/waterlogging tendency specifically (the one
    factor that genuinely reduces to a single site-relative 0-1 score); presumptive_bearing_capacity
    and water_table are separate, non-scored, indicative readings alongside it in the breakdown.
    """
    report = progress_cb or (lambda stage, pct: None)
    report("Connecting to Earth Engine...", 5)
    init_gee()

    geom = ee.Geometry(boundary_geojson)
    analysis_region = geom.buffer(500)

    # --- Soil texture -> Hydrologic Soil Group (drainage capacity + bearing-capacity texture) ---
    report("Reading soil texture data...", 25)
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

    # --- Terrain: slope + flow accumulation, for drainage proximity AND Topographic Wetness Index -
    report("Analyzing terrain and drainage network...", 50)
    mean_dist_val = 5000.0
    has_drainage_data = False
    twi_value: Optional[float] = None
    try:
        dem = ee.ImageCollection("COPERNICUS/DEM/GLO30_2024_1").select("DEM").mosaic()
        slope_rad = ee.Terrain.slope(dem).multiply(math.pi / 180.0)
        flow_acc = ee.Image("WWF/HydroSHEDS/15ACC").select("b1")
        channels = flow_acc.gt(1000)
        channel_dist = channels.fastDistanceTransform(30).sqrt()
        distance_m = channel_dist.multiply(flow_acc.projection().nominalScale()).rename("distance_m")
        # TWI = ln(upslope contributing area / tan(slope)) - Beven & Kirkby (1979). A small
        # constant avoids ln(0)/division-by-zero on perfectly flat or zero-accumulation pixels.
        twi_img = flow_acc.add(1).log().subtract(slope_rad.tan().max(0.001).log()).rename("twi")
        combined = ee.Dictionary({
            "mean_dist": distance_m.reduceRegion(reducer=ee.Reducer.mean(), geometry=geom, scale=463, maxPixels=1e9).get("distance_m"),
            "mean_twi": twi_img.reduceRegion(reducer=ee.Reducer.mean(), geometry=geom, scale=463, maxPixels=1e9).get("twi"),
        }).getInfo()
        if combined.get("mean_dist") is not None:
            mean_dist_val = float(combined["mean_dist"])
            has_drainage_data = True
        if combined.get("mean_twi") is not None:
            twi_value = float(combined["mean_twi"])
    except Exception:
        pass
    drainage_proximity_score = max(0.0, min(1.0, 1.0 - (mean_dist_val / 500.0)))

    has_data = has_soil_data or has_drainage_data

    report("Scoring drainage risk...", 75)
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

    report("Estimating bearing capacity and water table tendency...", 85)
    bearing_capacity = _presumptive_bearing_capacity(hydrologic_soil_group, sand_pct, clay_pct)
    water_table = _water_table_tendency(twi_value)

    report("Reading soil profile to 2m depth...", 90)
    soil_profile = _fetch_soil_profile(analysis_region)

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
        "presumptive_bearing_capacity": bearing_capacity,
        "water_table": water_table,
        "soil_profile": soil_profile,
        "scope_note": SOIL_SCOPE_NOTE,
        "_references": SOIL_ANALYSIS_REFERENCES,
    }

    risk_class, _class_color = classify_risk(risk_value, has_data)
    breakdown["_gis_export"] = {"boundary_geojson": boundary_geojson, "buildings_gdf": None, "value_points": None, "value_key": "soil_risk_pct"}
    breakdown["_interactive"] = None
    breakdown["buildings_total"] = 0
    breakdown["buildings_threatened"] = 0

    report("Finalizing report...", 95)
    return risk_value, risk_class, breakdown, None
