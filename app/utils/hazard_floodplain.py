from __future__ import annotations

import base64
import io
from typing import Any, Callable, Dict, Optional, Tuple

import ee
from sqlalchemy.orm import Session

from app.utils.elevation import fetch_dem_elevation_points
from app.utils.gee_client import init_gee
from app.utils.hazard_common import classify_risk, fetch_buildings_near, fetch_susceptibility_points
from app.utils.hazard_local_data import compute_confidence_score
from app.utils.hazard_map_renderer import render_floodplain_hazard_map

# MERIT Hydro (Yamazaki et al. 2019) - a Google-curated, officially catalogued global hydrography
# product (no personal-namespace availability risk, unlike the community HAND asset originally
# considered) that ships its own Height Above Nearest Drainage band directly - live-verified this
# session: bands are elv/dir/wth/wat/upa/upg/hnd/viswth, ~90m (3 arc-second) resolution.
_MERIT_HYDRO_ASSET = "MERIT/Hydro/v1_0_1"
_MERIT_SCALE_M = 90
_LOCAL_AREA_RADIUS_M = 300  # wide enough for a real multi-pixel HAND distribution at 90m native
                            # resolution without diluting into unrelated terrain - the same radius
                            # validated in this session's real-parcel diagnostic.
_RIVER_DIST_CAP_M = 2000.0  # fastDistanceTransform only searches a bounded pixel radius; beyond
                             # that its output is a search-cutoff artifact, not a real measurement
                             # (confirmed live: an uncapped ~4,298km value at two diagnostic sites)
                             # - capped the same way hazard_pluvial.py caps its own drainage-
                             # distance signal.

# V3.1 FROZEN TERRAIN SUSCEPTIBILITY INDEX - supersedes the earlier single-variable, provisional
# "1 - HAND/25" transform (see scratch/V3_1_TERRAIN_INDEX_PERMANENT_RECORD.md for the full R&D
# history). V3.1 = 0.5*normalize(-HAND) + 0.5*normalize(-RelativeElevation_1000m), where both
# terms are negated (lower HAND / lower relative elevation = higher susceptibility) before a fixed
# min-max rescale. The formula, both terms, and the normalization constants below are FROZEN - they
# were derived once from 417 pooled samples across 13 independent UFO flood events and are not to be
# refit, retuned, or adjusted based on any single future result (that is precisely the discipline
# that produced two independent out-of-sample replications: median AUC 0.734 on UFO, 0.729 on
# Sen1Floods11 - see the permanent record). No weight in this formula was ever fit to a label.
#
# IMPORTANT SCOPE NOTE: this is a validated TERRAIN INUNDATION SUSCEPTIBILITY signal (low-lying
# terrain relative to its surroundings predicts observed flood extent across two independent global
# benchmarks). It is NOT a validated Nigerian urban-pluvial/drainage risk model - every attempt this
# session to obtain qualifying Nigerian pluvial ground truth failed at the data-qualification stage,
# before this index was ever tested against it (see scratch/V3_NIGERIA_SEARCH_CLOSURE.md). Do not
# extend the customer-facing framing beyond "floodplain/terrain susceptibility" on the strength of
# this deployment alone. The Pluvial/Rainfall branch's separate "Experimental" label is unaffected
# by this change and must not be upgraded until genuine Nigerian pluvial ground truth is obtained.
_HAND_ORIENTED_MIN, _HAND_ORIENTED_MAX = -165.94714902903826, -0.011056458756802398
_RELELEV_ORIENTED_MIN, _RELELEV_ORIENTED_MAX = -90.06438029231361, 76.09087863884835
_RELATIVE_ELEV_RADIUS_M = 1000.0

FLOODPLAIN_REFERENCES = [
    {
        "short": "Yamazaki et al. (2019)",
        "citation": (
            "Yamazaki, D., Ikeshima, D., Sosa, J., Bates, P.D., Allen, G.H., Pavelsky, T.M. (2019). "
            "MERIT Hydro: A high-resolution global hydrography map based on latest topography "
            "datasets. Water Resources Research, 55, 5053-5073."
        ),
        "url": "https://doi.org/10.1029/2019WR024873",
    },
    {
        "short": "Rennó et al. (2008)",
        "citation": (
            "Rennó, C.D., Nobre, A.D., Cuartas, L.A., Soares, J.V., Hodnett, M.G., Tomasella, J., "
            "Waterloo, M.J. (2008). HAND, a new terrain descriptor using SRTM-DEM: Mapping "
            "terra-firme rainforest environments in Amazonia. Remote Sensing of Environment, 112, "
            "3469-3481."
        ),
        "url": "https://doi.org/10.1016/j.rse.2008.03.018",
    },
]


def compute_floodplain_risk(
    db: Session, boundary_geojson: Dict[str, Any], progress_cb: Optional[Callable[[str, int], None]] = None,
) -> Tuple[float, str, Dict[str, Any], bytes]:
    """Floodplain Susceptibility: how close this site sits to the regional drainage/river network in
    elevation terms, independent of whether JRC/GloFAS has a routed river-flood simulation here (see
    compute_flood_risk in hazard_flood.py) and independent of local rainfall-runoff ponding (see
    compute_pluvial_risk in hazard_pluvial.py). This is the fallback signal for exactly the gap the
    other two engines can't cover: a real river-floodplain location with no GloFAS coverage and no
    local terrain depression to trigger the pluvial engine (a real diagnostic case this session -
    Ogbaru, a River Niger floodplain town, read Low on the pluvial engine and had zero GloFAS
    coverage, yet its median Height Above Nearest Drainage across real building parcels was ~2m).

    Score is the frozen V3.1 terrain index: 0.5*normalize(-HAND) + 0.5*normalize(-RelativeElevation
    at 1000m) - see the module-level comment above for the full frozen-formula rationale and its
    two independent out-of-sample replications. MERIT Hydro's own channel-mask distance and upstream
    contributing-area were separately tested as candidate score inputs and found unreliable for
    small urban streams (going the wrong direction on a real diagnostic pair), so they're surfaced
    in the breakdown as supporting context only, never part of risk_value - same treatment as before.
    """
    report = progress_cb or (lambda stage, pct: None)
    report("Connecting to Earth Engine...", 10)
    init_gee()

    geom = ee.Geometry(boundary_geojson)
    local_area = geom.buffer(_LOCAL_AREA_RADIUS_M)

    merit = ee.Image(_MERIT_HYDRO_ASSET)
    hand_img = merit.select("hnd")
    merit_upa = merit.select("upa")
    merit_wat = merit.select("wat")
    river_mask = merit_wat.gt(0)
    river_dist_m_img = (
        river_mask.fastDistanceTransform(30).sqrt().multiply(merit_wat.projection().nominalScale()).rename("river_dist_m")
    )

    # V3.1's second term - point elevation minus the 1000m-radius focal-mean elevation, sampled at
    # the same location as everything else here. Same asset/scale as the frozen R&D methodology.
    dem = ee.ImageCollection("COPERNICUS/DEM/GLO30_2024_1").select("DEM").mosaic()
    dem_focal_1000 = dem.focal_mean(radius=_RELATIVE_ELEV_RADIUS_M, units="meters")

    report("Sampling elevation above nearest drainage...", 40)
    pct_reducer = (
        ee.Reducer.median().combine(ee.Reducer.min(), sharedInputs=True).combine(ee.Reducer.percentile([10]), sharedInputs=True)
    )
    combined = ee.Dictionary({
        "upa_km2": merit_upa.reduceRegion(ee.Reducer.mean(), local_area, scale=_MERIT_SCALE_M, maxPixels=1e9, bestEffort=True).get("upa"),
        "river_dist_m": river_dist_m_img.reduceRegion(ee.Reducer.mean(), local_area, scale=_MERIT_SCALE_M, maxPixels=1e9, bestEffort=True).get("river_dist_m"),
        "elev_m": dem.reduceRegion(ee.Reducer.mean(), geom, scale=30, maxPixels=1e9, bestEffort=True).get("DEM"),
        "focal_1000_m": dem_focal_1000.reduceRegion(ee.Reducer.mean(), geom, scale=30, maxPixels=1e9, bestEffort=True).get("DEM"),
    }).getInfo()
    hand_stats = hand_img.reduceRegion(pct_reducer, local_area, scale=_MERIT_SCALE_M, maxPixels=1e9, bestEffort=True).getInfo()

    hand_median_m = hand_stats.get("hnd_median")
    hand_min_m = hand_stats.get("hnd_min")
    hand_p10_m = hand_stats.get("hnd_p10")
    elev_m = combined.get("elev_m")
    focal_1000_m = combined.get("focal_1000_m")
    relative_elev_1000m = (
        float(elev_m) - float(focal_1000_m) if elev_m is not None and focal_1000_m is not None else None
    )
    data_available = hand_median_m is not None  # MERIT Hydro has complete global coverage - this
                                                  # should be True for virtually every real request;
                                                  # False only on a genuine EE failure, never on a
                                                  # legitimate "no data here" the way GloFAS can be.

    def _normalize(oriented_value: float, lo: float, hi: float) -> float:
        scaled = (oriented_value - lo) / (hi - lo) if hi > lo else 0.5
        return max(0.0, min(1.0, scaled))  # clip - see permanent record's clipping-rule note

    if data_available:
        hand_term = _normalize(-float(hand_median_m), _HAND_ORIENTED_MIN, _HAND_ORIENTED_MAX)
        if relative_elev_1000m is not None:
            relelev_term = _normalize(-relative_elev_1000m, _RELELEV_ORIENTED_MIN, _RELELEV_ORIENTED_MAX)
            risk_value = 0.5 * hand_term + 0.5 * relelev_term
        else:
            # Copernicus DEM has near-complete coverage too, but if this specific read fails, fall
            # back to the HAND-only term rather than failing the whole engine - still the frozen
            # formula's own HAND component, not a different constant.
            risk_value = hand_term
    else:
        risk_value = 0.0
    risk_class, class_color = classify_risk(risk_value, data_available)

    raw_river_dist = combined.get("river_dist_m")
    capped_river_dist = min(float(raw_river_dist), _RIVER_DIST_CAP_M) if raw_river_dist is not None else _RIVER_DIST_CAP_M
    upa_km2 = combined.get("upa_km2")

    breakdown: Dict[str, Any] = {
        "data_available": data_available,
        "hand_median_m": round(float(hand_median_m), 1) if hand_median_m is not None else None,
        "hand_min_m": round(float(hand_min_m), 1) if hand_min_m is not None else None,
        "hand_p10_m": round(float(hand_p10_m), 1) if hand_p10_m is not None else None,
        "relative_elevation_1000m_m": round(relative_elev_1000m, 1) if relative_elev_1000m is not None else None,
        "distance_to_major_river_m": round(capped_river_dist, 1),
        "upstream_area_km2": round(float(upa_km2), 2) if upa_km2 is not None else None,
        "flood_data_source": "hand_merit_hydro_v3_1",
        "score_method": "v3_1_frozen_terrain_index",
        "data_sources": {"floodplain": "merit_hydro_hand_plus_copernicus_dem_relative_elevation"},
        "_references": FLOODPLAIN_REFERENCES,
    }

    confidence = compute_confidence_score(
        breakdown["data_sources"], {"floodplain": 1.0},
    )
    breakdown["confidence"] = confidence

    # Converts the frozen V3.1 index to the same 0-1 susceptibility-image convention every other
    # engine's map sampling expects, using the SAME formula/constants as risk_value above (kept
    # visually consistent, never a second, different mapping).
    hand_term_img = (
        hand_img.multiply(-1).subtract(_HAND_ORIENTED_MIN).divide(_HAND_ORIENTED_MAX - _HAND_ORIENTED_MIN).max(0).min(1)
    )
    relative_elev_img = dem.subtract(dem_focal_1000).rename("relative_elev_1000m")
    relelev_term_img = (
        relative_elev_img.multiply(-1)
        .subtract(_RELELEV_ORIENTED_MIN)
        .divide(_RELELEV_ORIENTED_MAX - _RELELEV_ORIENTED_MIN)
        .max(0)
        .min(1)
    )
    susceptibility_img = hand_term_img.multiply(0.5).add(relelev_term_img.multiply(0.5))
    surface_points = fetch_susceptibility_points(susceptibility_img, local_area)
    contour_points = fetch_dem_elevation_points(boundary_geojson, buffer_m=1000)

    report("Rendering floodplain susceptibility map...", 75)
    buildings = fetch_buildings_near(db, boundary_geojson, 600)
    png_bytes, map_stats = render_floodplain_hazard_map(
        boundary_geojson=boundary_geojson,
        susceptibility_points=surface_points,
        contour_points=contour_points,
        buildings=buildings,
        risk_class=risk_class,
        class_color=class_color,
        buffer_m=1000,
    )
    breakdown["buildings_total"] = map_stats.get("buildings_total", 0)
    breakdown["buildings_threatened"] = map_stats.get("buildings_threatened", 0)
    breakdown["_gis_export"] = {
        "boundary_geojson": boundary_geojson,
        "buildings_gdf": map_stats.get("buildings_gdf"),
        "value_points": map_stats.get("value_points"),
        "value_key": map_stats.get("value_key", "floodplain_susceptibility_pct"),
    }
    breakdown["_interactive"] = map_stats.get("interactive")

    report("Finalizing report...", 95)
    return risk_value, risk_class, breakdown, png_bytes


def floodplain_overlay_to_data_url(png_bytes: bytes) -> str:
    encoded = base64.b64encode(png_bytes).decode("utf-8")
    return f"data:image/png;base64,{encoded}"


def floodplain_overlay_to_bytes_io(png_bytes: bytes) -> io.BytesIO:
    return io.BytesIO(png_bytes)
