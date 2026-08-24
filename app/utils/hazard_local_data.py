from __future__ import annotations

from typing import Any, Dict, List, Optional

# Real, peer-reviewed sources for the local-ground-data methods below (RUSLE K-factor,
# SCS/NRCS Curve Number runoff, and the Nigerian gully-susceptibility factor weights) - shown to
# users alongside the existing GloFAS/terrain-proxy/RUSLE-index references already in
# hazard_common.py, following this codebase's established citation convention.
LOCAL_DATA_REFERENCES = [
    {
        "short": "Wischmeier & Smith (1978)",
        "citation": "Wischmeier, W.H., Smith, D.D. (1978). Predicting Rainfall Erosion Losses: A Guide to Conservation Planning. USDA Agriculture Handbook No. 537.",
        "url": "https://naldc.nal.usda.gov/download/CAT79706928/PDF",
    },
    {
        "short": "USDA NRCS (2004)",
        "citation": "USDA Natural Resources Conservation Service (2004). National Engineering Handbook, Part 630, Chapter 10: Estimation of Direct Runoff from Storm Rainfall (SCS/NRCS Curve Number method).",
        "url": "https://www.nrcs.usda.gov/sites/default/files/2022-08/Chapter%2010%20Estimation%20of%20Direct%20Runoff%20from%20Storm%20Rainfall.pdf",
    },
    {
        "short": "Okafor et al. (2023)",
        "citation": "Okafor et al. (2023). Geospatial factors driving gully erosion susceptibility in southeastern Nigeria - relative contribution of slope angle, plasticity index, angle of internal friction, cohesion, and population density. Natural Hazards.",
        "url": "https://link.springer.com/article/10.1007/s11069-023-05971-6",
    },
    {
        "short": "McCool et al. (1987)",
        "citation": "McCool, D.K., Brown, L.C., Foster, G.R., Mutchler, C.K., Meyer, L.D. (1987). Revised slope steepness factor for the Universal Soil Loss Equation. Transactions of the ASAE, 30(5), 1387-1396.",
        "url": "https://doi.org/10.13031/2013.30576",
    },
]

# NRCS TR-55 style runoff Curve Numbers by (site_type, hydrologic soil group). Values are the
# standard "fair condition" row for each cover type from NEH-4/TR-55 tables - a reasonable,
# citable default when the user hasn't surveyed cover condition in more detail.
_CURVE_NUMBERS: Dict[str, Dict[str, int]] = {
    "bare_soil": {"A": 77, "B": 86, "C": 91, "D": 94},
    "agricultural": {"A": 67, "B": 78, "C": 85, "D": 89},
    "residential_low_density": {"A": 51, "B": 68, "C": 79, "D": 84},
    "residential_high_density": {"A": 77, "B": 85, "C": 90, "D": 92},
    "commercial_paved": {"A": 89, "B": 92, "C": 94, "D": 95},
}
DEFAULT_SITE_TYPE = "residential_low_density"


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _mean_of(points: List[Dict[str, Any]], key: str) -> Optional[float]:
    values = []
    for p in points:
        v = p.get(key)
        if v is None:
            continue
        try:
            values.append(float(v))
        except (TypeError, ValueError):
            continue
    if not values:
        return None
    return sum(values) / len(values)


def compute_k_factor(
    silt_vfs_pct: float, clay_pct: float, organic_matter_pct: float,
    structure_code: float, permeability_code: float,
) -> float:
    """RUSLE/USLE soil-erodibility K-factor via the Wischmeier nomograph approximation
    (Wischmeier & Smith, 1978), converted from US customary units to SI (t.ha.h / (ha.MJ.mm))
    using the standard 0.1317 factor (Foster et al., 1981) so it combines directly with an SI
    R-factor. `structure_code` is the USDA soil-structure class (1=very fine granular ... 4=blocky/
    platy/massive) and `permeability_code` is the USDA profile-permeability class (1=rapid ...
    6=very slow) - both standard fields on a geotechnical/pedological soil report.
    """
    m = max(0.0, silt_vfs_pct) * max(0.0, 100.0 - clay_pct)
    om = max(0.0, min(organic_matter_pct, 11.9))  # nomograph is undefined above ~12% OM
    k_us = (
        2.1e-4 * (m ** 1.14) * (12.0 - om)
        + 3.25 * (structure_code - 2.0)
        + 2.5 * (permeability_code - 3.0)
    ) / 100.0
    k_us = max(0.0, k_us)
    return round(k_us * 0.1317, 4)


def compute_gully_susceptibility_index(
    cohesion_kpa: Optional[float], friction_angle_deg: Optional[float],
    plasticity_index: Optional[float], slope_deg: float,
) -> Optional[float]:
    """A 0..1 gully-erosion susceptibility index weighted by the relative factor contributions
    reported for southeastern Nigeria gully sites (Okafor et al., 2023: slope ~20%, plasticity
    index ~23%, friction angle ~20%, cohesion ~18%, population density ~9% - renormalized to 100%
    across the four geotechnical/terrain factors this app can actually source, since population
    density isn't survey data). Each factor is normalized against a practical reference range for
    Nigerian lateritic/sandy soils rather than a universal geotechnical extreme.
    """
    if cohesion_kpa is None and friction_angle_deg is None and plasticity_index is None:
        return None
    cohesion_score = 1.0 - clamp01((cohesion_kpa if cohesion_kpa is not None else 25.0) / 50.0)
    friction_score = 1.0 - clamp01((friction_angle_deg if friction_angle_deg is not None else 30.0) / 40.0)
    pi_score = clamp01((plasticity_index if plasticity_index is not None else 15.0) / 40.0)
    slope_score = clamp01(slope_deg / 25.0)

    # Weights renormalized from Okafor et al. (2023)'s reported contributions (PI 23, friction 20,
    # cohesion 18, slope 20 -> sum 81) to sum to 1.0 across these four factors.
    w_pi, w_friction, w_cohesion, w_slope = 23 / 81, 20 / 81, 18 / 81, 20 / 81
    index = (
        pi_score * w_pi
        + friction_score * w_friction
        + cohesion_score * w_cohesion
        + slope_score * w_slope
    )
    return round(clamp01(index), 3)


def derive_hydrologic_soil_group(sand_pct: float, clay_pct: float) -> str:
    """A simplified USDA-texture-triangle-to-Hydrologic-Soil-Group mapping (A = high infiltration/
    sandy, D = low infiltration/clayey), used when the user gives a texture breakdown instead of
    directly stating a group letter.
    """
    if clay_pct >= 40:
        return "D"
    if clay_pct >= 27 or (clay_pct >= 20 and sand_pct < 45):
        return "C"
    if sand_pct >= 70 and clay_pct < 20:
        return "A" if sand_pct >= 85 and clay_pct < 10 else "B"
    return "B"


def compute_scs_runoff(
    hydrologic_soil_group: str, site_type: str, design_rainfall_mm: float, lambda_ratio: float = 0.2,
) -> Dict[str, float]:
    """SCS/NRCS Curve Number runoff estimate (USDA NEH-4/TR-55): S = (25400/CN) - 254 (mm),
    Ia = lambda*S, Q = (P-Ia)^2 / (P-Ia+S) for P > Ia else 0. lambda=0.2 is the NRCS-standard
    initial-abstraction ratio; SE-Nigeria-calibrated studies have found lambda closer to 0.24 for
    local basins, but 0.2 is used as the well-established, internationally documented default.
    """
    hsg = hydrologic_soil_group if hydrologic_soil_group in ("A", "B", "C", "D") else "B"
    site = site_type if site_type in _CURVE_NUMBERS else DEFAULT_SITE_TYPE
    cn = _CURVE_NUMBERS[site][hsg]
    s_mm = (25400.0 / cn) - 254.0
    ia_mm = lambda_ratio * s_mm
    p = max(0.0, float(design_rainfall_mm))
    if p > ia_mm:
        runoff_mm = ((p - ia_mm) ** 2) / (p - ia_mm + s_mm)
    else:
        runoff_mm = 0.0
    runoff_coefficient = (runoff_mm / p) if p > 0 else 0.0
    return {
        "curve_number": cn,
        "hydrologic_soil_group": hsg,
        "site_type": site,
        "potential_retention_mm": round(s_mm, 1),
        "initial_abstraction_mm": round(ia_mm, 1),
        "design_rainfall_mm": round(p, 1),
        "runoff_mm": round(runoff_mm, 1),
        "runoff_coefficient": round(clamp01(runoff_coefficient), 3),
    }


# Relative trust in each kind of input, used only to weight the confidence score below - not a
# claim about real-world accuracy (there's no ground-truth flood/erosion dataset to validate
# against), just an honest ranking of how direct each data source is: a surveyor's own reading of
# this specific site outranks a global 30m model of it, which outranks a purpose-built regional/
# global hazard model, which outranks a generic satellite proxy standing in for something it
# wasn't designed to measure.
SOURCE_QUALITY = {
    "local_survey": 0.95,
    "user_input": 0.90,
    "glofas": 0.85,
    "satellite_hydrosheds": 0.65,
    "local_terrain_proxy": 0.65,
    "chirps_rainfall": 0.65,  # gauge-calibrated satellite rainfall, same tier as HydroSHEDS/the
                              # terrain proxy - a purpose-built, widely-validated regional dataset,
                              # not a generic stand-in for something it wasn't designed to measure.
    "merit_hydro_hand": 0.65,  # MERIT Hydro (Yamazaki et al. 2019) - a purpose-built, peer-reviewed
                                # global hydrography product, same tier as CHIRPS/HydroSHEDS.
    "esri_lulc_impervious": 0.60,  # AI-classified land cover, same tier as satellite_ndvi below -
                                    # a real satellite classification, but a proxy for "impervious
                                    # surface" rather than a purpose-built imperviousness product.
    "satellite_ndvi": 0.60,
    "global_dem": 0.55,
    "global_soil_texture": 0.55,  # same tier as global_dem - a coarse global model of the site,
                                   # not a purpose-built regional hazard model or a local survey.
    "not_available": 0.0,
}


def compute_confidence_score(
    factor_sources: Dict[str, str], factor_weights: Dict[str, float],
    local_point_count: int = 0, plot_area_ha: float = 0.0,
) -> Dict[str, Any]:
    """A transparent "input data confidence" score (0-100) - NOT a claim of accuracy against
    reality, since no measured flood-depth/erosion-rate dataset exists to validate against. It
    reflects how direct and well-sampled the inputs actually feeding risk_value are: a weighted
    average of each contributing factor's source quality (see SOURCE_QUALITY), where any factor
    sourced from the surveyor's own point survey is additionally discounted by point density
    relative to plot area - a single point anywhere in a 5-hectare plot shouldn't earn the same
    confidence as a well-distributed 20-point survey of the same site.
    """
    total_weight = sum(w for w in factor_weights.values() if w > 0) or 1.0
    points_per_ha = (local_point_count / plot_area_ha) if plot_area_ha > 0 else 0.0
    # Even a single point is informative (better than nothing), so density never zeroes the
    # quality out entirely - it scales between a 40% floor (sparse/unclear coverage) and 100%
    # (>=4 points per hectare, a reasonably dense site survey).
    density_factor = min(1.0, 0.4 + 0.6 * min(points_per_ha / 4.0, 1.0)) if local_point_count > 0 else 0.4

    weighted_quality = 0.0
    for factor, weight in factor_weights.items():
        if weight <= 0:
            continue
        source = factor_sources.get(factor, "not_available")
        quality = SOURCE_QUALITY.get(source, 0.5)
        if source == "local_survey":
            quality *= density_factor
        weighted_quality += weight * quality

    score = round(clamp01(weighted_quality / total_weight) * 100)
    if score >= 80:
        tier = "Very High"
    elif score >= 60:
        tier = "High"
    elif score >= 40:
        tier = "Moderate"
    else:
        tier = "Low"

    notes = []
    if local_point_count > 0 and plot_area_ha > 0:
        notes.append(f"{local_point_count} local point(s) across {round(plot_area_ha, 2)} ha ({round(points_per_ha, 1)}/ha)")

    return {
        "score": int(score),
        "tier": tier,
        "factor_sources": factor_sources,
        "notes": notes,
    }


def summarize_local_soil_points(points: List[Dict[str, Any]]) -> Optional[Dict[str, float]]:
    """Averages whichever optional geotechnical/soil fields are actually present across the
    surveyor's uploaded points (not every field needs to be filled on every point). Returns None
    if none of the recognized fields have any usable values at all.
    """
    fields = (
        "silt_vfs_pct", "clay_pct", "sand_pct", "organic_matter_pct",
        "soil_structure_code", "soil_permeability_code",
        "cohesion_kpa", "friction_angle_deg", "plasticity_index",
    )
    result: Dict[str, float] = {}
    for field in fields:
        mean_val = _mean_of(points, field)
        if mean_val is not None:
            result[field] = mean_val
    return result or None
