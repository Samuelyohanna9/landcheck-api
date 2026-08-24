from __future__ import annotations

import base64
import io
from typing import Any, Callable, Dict, List, Optional, Tuple

import ee
from sqlalchemy.orm import Session

from app.utils.elevation import fetch_dem_elevation_points
from app.utils.gee_client import init_gee
from app.utils.hazard_common import (
    FLOOD_REFERENCES_TERRAIN_PROXY,
    classify_risk,
    fetch_buildings_near,
    fetch_susceptibility_points,
)
from app.utils.hazard_local_data import (
    DEFAULT_SITE_TYPE,
    LOCAL_DATA_REFERENCES,
    compute_confidence_score,
    compute_scs_runoff,
    derive_hydrologic_soil_group,
    summarize_local_soil_points,
)
from app.utils.hazard_lulc import LULC_ASSETS, LULC_REFERENCES, _is_known_class_code, _load_landcover_source_image
from app.utils.hazard_map_renderer import render_pluvial_hazard_map

# Esri's "Built Area" class is code 7 in BOTH of hazard_lulc.py's class-code tables (the 2017-2025
# time series and the older single-year 2020 scheme) - one of the classes that kept the same value
# across Esri's later Grass+Scrub -> Rangeland merge (see hazard_map_renderer.py's
# LULC_CLASS_COLORS_TS/_2020 for the full reasoning). Safe to hardcode here regardless of which of
# the two assets ends up supplying the data.
_BUILT_AREA_CLASS_CODE = 7

# Extreme-rainfall design storm, gauge-calibrated satellite precipitation, 1981-present, ~5.5km
# resolution - long-established, stable GEE catalog dataset. DAILY (not PENTAD/5-day sums) because
# pluvial flooding is driven by a single extreme-rainfall day, not a multi-day accumulation.
_CHIRPS_DAILY_ASSET = "UCSB-CHG/CHIRPS/DAILY"
_CHIRPS_SCALE_M = 5500

# Global soil texture (surface/0cm depth) - verified live against this exact asset/band this
# session: band names are b0/b10/b30/b60/b100/b200 (depth layers), values are 0-100 integer
# percent (not 0-1 fraction).
_SOIL_SAND_ASSET = "OpenLandMap/SOL/SOL_SAND-WFRACTION_USDA-3A1A1A_M/v02"
_SOIL_CLAY_ASSET = "OpenLandMap/SOL/SOL_CLAY-WFRACTION_USDA-3A1A1A_M/v02"
_SOIL_BAND = "b0"
_SOIL_SCALE_M = 250

PLUVIAL_REFERENCES = [
    {
        "short": "Funk et al. (2015)",
        "citation": (
            "Funk, C., Peterson, P., Landsfeld, M., Pedreros, D., Verdin, J., Shukla, S., Husak, G., "
            "Rowland, J., Harrison, L., Hoell, A., Michaelsen, J. (2015). The climate hazards "
            "infrared precipitation with stations - a new environmental record for monitoring "
            "extremes. Scientific Data, 2, 150066."
        ),
        "url": "https://doi.org/10.1038/sdata.2015.66",
    },
    {
        "short": "Hengl et al. (2017)",
        "citation": (
            "Hengl, T., Mendes de Jesus, J., Heuvelink, G.B.M., et al. (2017). SoilGrids250m: "
            "Global gridded soil information based on machine learning. PLoS ONE, 12(2), e0169748."
        ),
        "url": "https://doi.org/10.1371/journal.pone.0169748",
    },
] + FLOOD_REFERENCES_TERRAIN_PROXY + LULC_REFERENCES


def _site_type_from_impervious_fraction(impervious_frac: float) -> str:
    """Auto-selects an SCS/NRCS site cover type from the Esri Built-Area fraction when the caller
    hasn't specified one - a real, satellite-derived proxy for how much of the surface can't
    infiltrate rainfall, standing in for a surveyor's own land-use judgment. Thresholds are a
    documented judgment call, not from a peer-reviewed source, mirroring how _CURVE_NUMBERS itself
    is presented in hazard_local_data.py.
    """
    if impervious_frac > 0.6:
        return "commercial_paved"
    if impervious_frac > 0.3:
        return "residential_high_density"
    if impervious_frac > 0.1:
        return "residential_low_density"
    return "agricultural"


def _fetch_impervious_fraction(
    geom: "ee.Geometry", _resolved: Optional[Tuple["ee.Image", Any]] = None,
) -> Tuple[Optional[float], Optional["ee.Image"], Optional[Any]]:
    """Fraction (0-1) of `geom` classified as Esri "Built Area", reusing hazard_lulc.py's already
    live-verified asset/class-code pairing (LULC_ASSETS) rather than re-deriving it - the exact
    per-asset class-code mismatch that caused a real "93% Snow/Ice in Adamawa" bug earlier is the
    reason this doesn't just hardcode one asset ID here.

    `_resolved`, when given an (image, class_colors) pair from a previous call, skips straight to
    the histogram step instead of re-running the per-asset fallback loop - used so the parcel-scale
    and neighbourhood-scale reads below resolve the LULC asset only once per request, mirroring the
    efficiency pattern validated in scratch/test_impervious_scale_correlation.py. Returns
    (built_fraction, image, class_colors); (None, None, None) if no asset could be resolved.
    """
    candidates = [_resolved] if _resolved else _lulc_assets_with_images()
    for candidate_image, class_colors in candidates:
        try:
            hist = candidate_image.reduceRegion(
                reducer=ee.Reducer.frequencyHistogram(),
                geometry=geom, scale=10, maxPixels=int(1e9), bestEffort=True,
            ).get("landcover")
            raw_hist = hist.getInfo() or {}
        except Exception:
            continue

        # frequencyHistogram() under bestEffort=True can return fractional pixel-WEIGHT values
        # (e.g. 60.35, not an integer count) when the requested scale doesn't align perfectly with
        # the source grid - a real bug here previously truncated the denominator via int(v) while
        # leaving the numerator (built_pixels) as a raw float, producing a ratio slightly over 1.0
        # (confirmed live: 100.6% built-area at a real test point). Kept consistently as floats
        # throughout so the ratio can never mathematically exceed 1.0.
        raw_pixels = sum(float(v) for v in raw_hist.values())
        valid_hist = {k: v for k, v in raw_hist.items() if _is_known_class_code(k, class_colors)}
        valid_pixels = sum(float(v) for v in valid_hist.values())
        if raw_pixels and valid_pixels / raw_pixels < 0.5:
            continue
        if not valid_hist:
            continue

        built_pixels = sum(float(v) for k, v in valid_hist.items() if int(float(k)) == _BUILT_AREA_CLASS_CODE)
        return min(1.0, built_pixels / valid_pixels), candidate_image, class_colors
    return None, None, None


def _lulc_assets_with_images():
    for asset_id, class_colors in LULC_ASSETS:
        try:
            yield _load_landcover_source_image(asset_id), class_colors
        except Exception:
            continue


def compute_pluvial_risk(
    db: Session,
    boundary_geojson: Dict[str, Any],
    local_elevation_points: Optional[List[Dict[str, float]]] = None,
    site_type: Optional[str] = None,
    design_rainfall_mm: Optional[float] = None,
    analysis_mode: str = "hybrid",
    progress_cb: Optional[Callable[[str, int], None]] = None,
) -> Tuple[float, str, Dict[str, Any], bytes]:
    """Surface-Water / Rainfall Flood Risk: an always-computed pluvial-flooding screen, independent
    of whether JRC/GloFAS has river-flood coverage at this location (see compute_flood_risk in
    hazard_flood.py for the river/fluvial engine - the two are combined by the caller, never
    averaged, since a severe pluvial risk must never be diluted by a low river risk or vice versa).

    Three automatic, satellite/DEM-only signals (none require a surveyor upload) combine with the
    existing terrain-ponding methodology already used by this app:
      - terrain susceptibility (depression + flatness + drainage proximity) - unchanged from the
        methodology that used to live inline in compute_flood_risk's GloFAS-fallback branch.
      - a runoff coefficient (SCS/NRCS Curve Number) fed by a CHIRPS-derived extreme-rainfall design
        storm and an OpenLandMap-derived Hydrologic Soil Group - both real global datasets, not
        placeholders, so this produces a genuine result on every plot rather than only when a
        surveyor happens to supply rainfall/soil figures.
      - impervious/built-up surface fraction from Esri's 10m Land Cover (reused from the Land Use
        Land Cover feature, hazard_lulc.py) - more paved/roofed surface means less infiltration and
        more/faster runoff.

    `site_type`/`design_rainfall_mm` remain available as explicit overrides (preserving the existing
    survey-based "local" hybrid capability) - when not supplied, both are derived automatically from
    the built-area fraction and CHIRPS respectively, so this engine never falls back to a
    placeholder the way compute_scs_runoff historically did when nothing was supplied.

    V2 ARCHITECTURAL CORRECTION (impervious double-counting): earlier, parcel-scale built fraction
    fed BOTH a standalone 30%-weighted `impervious_score` AND (via `_site_type_from_impervious_
    fraction`) the SCS curve number driving `runoff_score` - the same evidence counted twice, which
    live diagnostic testing traced as the dominant driver of the pluvial score (r=+0.899 with the
    final result, versus terrain's +0.275) and the reason real flood corridors and well-drained
    control neighbourhoods became statistically indistinguishable once real building parcels were
    tested (34 of 35 real buildings read ~100% built at parcel scale, including a genuinely
    low-density GRA). Widening the standalone component to a 300m neighbourhood-scale reading was
    tested as a fix and rejected: Pearson r=0.734 between parcel-scale and 300m-scale imperviousness
    across the 35 diagnostic parcels, with the 5 locations that mattered most reading within 0.1
    percentage points of each other at both scales - not a genuinely independent signal.

    Standalone `impervious_score` is therefore REMOVED. Parcel-scale built fraction keeps its one
    remaining job (site_type -> curve number -> runoff_score); the vacated weight is not given to a
    substitute variable, it is redistributed to terrain and runoff preserving their prior 1:1 ratio
    (0.35:0.35 -> 0.5:0.5). Both parcel-scale and a 300m neighbourhood-scale built-fraction reading
    are still surfaced in the breakdown for transparency - urban sealing is real evidence a reader
    may want to see - but neither independently moves risk_value anymore.
    """
    report = progress_cb or (lambda stage, pct: None)
    report("Connecting to Earth Engine...", 5)
    init_gee()

    geom = ee.Geometry(boundary_geojson)
    analysis_region = geom.buffer(1000)

    # --- Terrain susceptibility (depression + flatness + drainage proximity) -------------------
    # Extracted as-is from what used to be compute_flood_risk's GloFAS-fallback branch - already
    # working, validated methodology; only its role changes (one component of a real score now,
    # not a silent substitute for the whole thing).
    report("Analyzing terrain susceptibility...", 20)
    dem_proxy = ee.ImageCollection("COPERNICUS/DEM/GLO30_2024_1").select("DEM").mosaic()
    slope_img = ee.Terrain.slope(dem_proxy).rename("slope_deg")
    local_mean_elev_img = dem_proxy.focal_mean(radius=300, units="meters")
    depression_img = local_mean_elev_img.subtract(dem_proxy).rename("depression_m")

    flow_acc = ee.Image("WWF/HydroSHEDS/15ACC").select("b1")
    local_channels = flow_acc.gt(100)
    local_channel_dist = local_channels.fastDistanceTransform(30).sqrt()
    local_drainage_dist_img = local_channel_dist.multiply(flow_acc.projection().nominalScale()).rename("local_drainage_m")

    flatness_score_img = ee.Image(1).subtract(slope_img.divide(15).min(1)).max(0)
    drainage_score_img = ee.Image(1).subtract(local_drainage_dist_img.divide(500).min(1)).max(0)
    depression_score_img = depression_img.divide(3).max(0).min(1)
    susceptibility_img = (
        depression_score_img.multiply(0.40)
        .add(flatness_score_img.multiply(0.35))
        .add(drainage_score_img.multiply(0.25))
    ).rename("susceptibility")

    # --- Extreme rainfall (CHIRPS 99th-percentile daily, server-side, one round trip per plot) --
    report("Sampling extreme rainfall history...", 35)
    chirps_p99_mm: Optional[float] = None
    if not design_rainfall_mm:
        try:
            chirps_daily = ee.ImageCollection(_CHIRPS_DAILY_ASSET).select("precipitation")
            p99_image = chirps_daily.reduce(ee.Reducer.percentile([99])).rename("p99_daily_mm")
            # Sampled over analysis_region (geom buffered 1000m), not the raw plot boundary - a
            # real GEE quirk confirmed live this session: reduceRegion over a small polygon (a
            # typical plot, tens of metres across) against a coarse-scale dataset (CHIRPS is
            # ~5.5km/pixel) can return None even with bestEffort=True, apparently because the
            # polygon's rasterization at that scale can cover zero pixel centers - a Point or a
            # sufficiently large buffered region always samples reliably. This is harmless for
            # accuracy here: CHIRPS is already representing ground far coarser than "the plot vs.
            # the plot plus 1km" could ever distinguish.
            chirps_p99_mm = p99_image.reduceRegion(
                reducer=ee.Reducer.mean(), geometry=analysis_region, scale=_CHIRPS_SCALE_M, maxPixels=1e9,
            ).get("p99_daily_mm").getInfo()
        except Exception:
            chirps_p99_mm = None

    # --- Global soil texture -> Hydrologic Soil Group, unless the user supplied their own --------
    report("Reading soil infiltration data...", 45)
    soil_summary = summarize_local_soil_points(local_elevation_points or []) or {}
    sand_pct: Optional[float] = soil_summary.get("sand_pct")
    clay_pct: Optional[float] = soil_summary.get("clay_pct")
    soil_source = "user_input" if (sand_pct is not None and clay_pct is not None) else "global_soil_texture"
    if sand_pct is None or clay_pct is None:
        try:
            # Same small-polygon-vs-coarse-scale None-return risk as CHIRPS above (OpenLandMap is
            # ~250m/pixel, still coarse relative to a typical plot) - sampled over analysis_region
            # for the same reason.
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
    hydrologic_soil_group = derive_hydrologic_soil_group(sand_pct, clay_pct) if sand_pct is not None and clay_pct is not None else "B"

    # --- Impervious/built-up surface fraction, reusing the Esri LULC asset -----------------------
    # Parcel-scale drives site_type/CN below (its one remaining job). A second, wider read over
    # the same 300m "neighbourhood" radius used elsewhere in this module is transparency-only
    # context (surfaced in the breakdown, never scored) - reuses the already-resolved asset/class-
    # table from the parcel-scale call rather than re-running the fallback loop a second time.
    report("Checking built-up surface coverage...", 55)
    impervious_frac, landcover_image, landcover_class_colors = _fetch_impervious_fraction(geom)
    impervious_available = impervious_frac is not None
    if not impervious_available:
        impervious_frac = 0.0

    neighborhood_impervious_frac: Optional[float] = None
    if landcover_image is not None:
        # 300m - the exact radius empirically tested in scratch/test_impervious_scale_correlation.py
        # (Pearson r=0.734 vs parcel-scale) - NOT the same as analysis_region above (1000m, used for
        # CHIRPS/soil sampling reasons unrelated to this reading).
        neighborhood_region = geom.buffer(300)
        neighborhood_impervious_frac, _, _ = _fetch_impervious_fraction(
            neighborhood_region, _resolved=(landcover_image, landcover_class_colors),
        )

    site_type_source = "user_input"
    resolved_site_type = site_type
    if not resolved_site_type:
        resolved_site_type = _site_type_from_impervious_fraction(impervious_frac) if impervious_available else DEFAULT_SITE_TYPE
        site_type_source = "auto_lulc" if impervious_available else "default"

    resolved_rainfall_mm = float(design_rainfall_mm) if design_rainfall_mm else (
        float(chirps_p99_mm) if chirps_p99_mm is not None else None
    )
    rainfall_source = "user_input" if design_rainfall_mm else "chirps_rainfall"

    scs_runoff = None
    runoff_coefficient = 0.0
    if resolved_rainfall_mm:
        scs_runoff = compute_scs_runoff(hydrologic_soil_group, resolved_site_type, resolved_rainfall_mm)
        runoff_coefficient = scs_runoff["runoff_coefficient"]

    # --- Combine terrain + CHIRPS/soil-fed runoff + impervious fraction into one score, one round
    # trip for the terrain-image scalars (matching compute_flood_risk's single-getInfo() pattern).
    report("Computing surface-water susceptibility...", 65)
    terrain_combined = ee.Dictionary({
        "susceptibility": susceptibility_img.reduceRegion(
            reducer=ee.Reducer.mean(), geometry=geom, scale=30, maxPixels=1e9
        ).get("susceptibility"),
        "mean_slope_deg": slope_img.reduceRegion(
            reducer=ee.Reducer.mean(), geometry=geom, scale=30, maxPixels=1e9
        ).get("slope_deg"),
        "mean_depression_m": depression_img.reduceRegion(
            reducer=ee.Reducer.mean(), geometry=geom, scale=30, maxPixels=1e9
        ).get("depression_m"),
        "distance_to_drainage_m": local_drainage_dist_img.reduceRegion(
            reducer=ee.Reducer.mean(), geometry=geom, scale=90, maxPixels=1e9
        ).get("local_drainage_m"),
    }).getInfo()

    terrain_score = max(0.0, min(1.0, float(terrain_combined.get("susceptibility") or 0.0)))
    runoff_score = max(0.0, min(1.0, runoff_coefficient))

    data_available = True
    # No standalone impervious_score - see the V2 architectural-correction note in this function's
    # docstring. Terrain/runoff renormalized to preserve their prior 1:1 ratio (0.35:0.35 -> 0.5:0.5)
    # arithmetically, not fit against any validation labels.
    risk_value = terrain_score * 0.5 + runoff_score * 0.5
    risk_value = max(0.0, min(1.0, risk_value))
    risk_class, class_color = classify_risk(risk_value, data_available)

    raw_drainage_dist = terrain_combined.get("distance_to_drainage_m")
    # fastDistanceTransform only searches a ~30-pixel radius (~14km at this raster's native
    # resolution) - beyond that its output is an artifact of the search cutoff, not a real
    # measurement, so it's capped here rather than displayed as false precision.
    capped_drainage_dist = min(float(raw_drainage_dist), 2000.0) if raw_drainage_dist is not None else 2000.0
    terrain_slope_deg = round(float(terrain_combined.get("mean_slope_deg") or 0.0), 2)
    terrain_depression_m = round(float(terrain_combined.get("mean_depression_m") or 0.0), 2)

    breakdown: Dict[str, Any] = {
        "data_available": data_available,
        "susceptibility_pct": round(terrain_score * 100, 1),
        "design_rainfall_mm": round(resolved_rainfall_mm, 1) if resolved_rainfall_mm else None,
        "runoff_coefficient": round(runoff_coefficient, 3),
        # Both impervious readings below are transparency-only context - neither independently
        # moves risk_value. impervious_fraction_pct (parcel scale) still drives site_type_used/CN.
        "impervious_fraction_pct": round(impervious_frac * 100, 1),
        "neighborhood_impervious_fraction_pct": (
            round(neighborhood_impervious_frac * 100, 1) if neighborhood_impervious_frac is not None else None
        ),
        "terrain_score": round(terrain_score, 3),
        "runoff_score": round(runoff_score, 3),
        "terrain_slope_deg": terrain_slope_deg,
        "terrain_depression_m": terrain_depression_m,
        "distance_to_drainage_m": round(capped_drainage_dist, 1),
        "scs_runoff": scs_runoff,
        "hydrologic_soil_group": hydrologic_soil_group,
        "site_type_used": resolved_site_type,
        "site_type_source": site_type_source,
        "flood_data_source": "pluvial_engine",
        "data_sources": {
            "terrain": "global_dem",
            "runoff": rainfall_source,
            "soil": soil_source,
            "impervious": "esri_lulc_impervious" if impervious_available else "not_available",
        },
        "_references": PLUVIAL_REFERENCES + (LOCAL_DATA_REFERENCES if soil_source == "user_input" or design_rainfall_mm else []),
    }

    factor_weights = {"terrain": 0.5, "runoff": 0.5}
    try:
        plot_area_ha = float(geom.area(1).getInfo()) / 10000.0
    except Exception:
        plot_area_ha = 0.0
    confidence = compute_confidence_score(
        breakdown["data_sources"],
        factor_weights,
        local_point_count=len([
            p for p in (local_elevation_points or [])
            if isinstance(p, dict) and (p.get("sand_pct") is not None or p.get("elevation_m") is not None)
        ]),
        plot_area_ha=plot_area_ha,
    )
    breakdown["confidence"] = confidence
    breakdown["analysis_mode"] = analysis_mode

    # Real susceptibility surface (for the graduated map) and real building footprints (flagged
    # against susceptibility, not depth) - fetched together, same pattern as the river engine.
    report("Rendering surface-water susceptibility map...", 80)
    surface_points = fetch_susceptibility_points(susceptibility_img, analysis_region)
    contour_points = fetch_dem_elevation_points(boundary_geojson, buffer_m=1000)
    buildings = fetch_buildings_near(db, boundary_geojson, 600)

    png_bytes, map_stats = render_pluvial_hazard_map(
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
        "value_key": map_stats.get("value_key", "flood_susceptibility_pct"),
    }
    breakdown["_interactive"] = map_stats.get("interactive")

    report("Finalizing report...", 95)
    return risk_value, risk_class, breakdown, png_bytes


def pluvial_overlay_to_data_url(png_bytes: bytes) -> str:
    encoded = base64.b64encode(png_bytes).decode("utf-8")
    return f"data:image/png;base64,{encoded}"


def pluvial_overlay_to_bytes_io(png_bytes: bytes) -> io.BytesIO:
    return io.BytesIO(png_bytes)
