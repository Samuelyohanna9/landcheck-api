from __future__ import annotations

import base64
import io
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import ee
import geopandas as gpd
import matplotlib.tri as mtri
import numpy as np
from sqlalchemy.orm import Session

from app.utils.elevation import fetch_dem_elevation_points
from app.utils.gee_client import init_gee
from app.utils.hazard_common import EROSION_REFERENCES, classify_risk, fetch_buildings_near
from app.utils.hazard_local_data import (
    LOCAL_DATA_REFERENCES,
    compute_confidence_score,
    compute_gully_susceptibility_index,
    compute_k_factor,
    summarize_local_soil_points,
)
from app.utils.hazard_map_renderer import render_erosion_hazard_map

# Same cloud-mask idiom used for the Green module's NDVI work (green_remote_monitoring.py) -
# QA60 bits 10/11 flag cloud and cirrus pixels.
def _mask_sentinel_clouds(image: ee.Image) -> ee.Image:
    qa = image.select("QA60")
    cloud_mask = qa.bitwiseAnd(1 << 10).eq(0)
    cirrus_mask = qa.bitwiseAnd(1 << 11).eq(0)
    return image.updateMask(cloud_mask.And(cirrus_mask)).copyProperties(image, image.propertyNames())


def _fetch_slope_points(dem: "ee.Image", region: "ee.Geometry", scale_m: int = 30) -> Optional[List[Dict[str, float]]]:
    """Samples ee.Terrain.slope(dem) as a {lng, lat, slope_deg} point cloud - the same
    pixelLonLat + toList idiom used for flood depth and DEM elevation sampling elsewhere in this
    module, so the erosion map can show a real graduated slope surface instead of a flat fill.
    """
    try:
        slope = ee.Terrain.slope(dem).rename("slope_deg")
        sampled = slope.addBands(ee.Image.pixelLonLat())
        reduced = sampled.reduceRegion(
            reducer=ee.Reducer.toList(),
            geometry=region,
            scale=scale_m,
            maxPixels=int(1e9),
            bestEffort=True,
        )
        info = reduced.getInfo() or {}
        lons = info.get("longitude") or []
        lats = info.get("latitude") or []
        slopes = info.get("slope_deg") or []
        if len(lons) < 3 or len(lons) != len(lats) or len(lons) != len(slopes):
            return None
        points = [
            {"lng": float(lo), "lat": float(la), "slope_deg": float(s)}
            for lo, la, s in zip(lons, lats, slopes)
            if s is not None
        ]
        return points if len(points) >= 3 else None
    except Exception:
        return None


def _compute_local_slope_from_points(points: List[Dict[str, float]]) -> Optional[Tuple[float, float]]:
    """Builds a TIN (triangulated surface) directly from the surveyor's own elevation points and
    measures the slope of every triangle - a global 30m DEM only spans a handful of pixels across
    a typical plot, so a surveyor's own points (even a modest handful) resolve local terrain far
    more accurately. Returns (area-weighted mean slope deg, max slope deg), or None if there
    aren't enough usable points to triangulate.
    """
    if not points or len(points) < 3:
        return None
    try:
        lons = [float(p["lng"]) for p in points]
        lats = [float(p["lat"]) for p in points]
        elevations = [float(p["elevation_m"]) for p in points]
    except (KeyError, TypeError, ValueError):
        return None

    try:
        gdf = gpd.GeoDataFrame(
            {"elevation_m": elevations},
            geometry=gpd.points_from_xy(lons, lats),
            crs="EPSG:4326",
        )
        centroid = gdf.geometry.unary_union.centroid
        utm_zone = int((centroid.x + 180) / 6) + 1
        epsg = 32600 + utm_zone if centroid.y >= 0 else 32700 + utm_zone
        projected = gdf.to_crs(epsg=epsg)
        xs = projected.geometry.x.to_numpy()
        ys = projected.geometry.y.to_numpy()
        zs = projected["elevation_m"].to_numpy()
        triang = mtri.Triangulation(xs, ys)
    except Exception:
        return None

    # Horizontal (map-view) footprint area per triangle, used both as the mean-slope weight and
    # to discard slivers: a triangle whose 2D footprint is nearly zero (e.g. from near-collinear
    # points, common at the edges of a small/regular point set) can report a near-vertical slope
    # purely from floating-point noise, even though the real terrain is gentle - that's a
    # triangulation artifact, not a real cliff, so it must not be allowed to dominate max_slope.
    horizontal_areas = []
    for tri_indices in triang.triangles:
        x0, y0 = xs[tri_indices[0]], ys[tri_indices[0]]
        x1, y1 = xs[tri_indices[1]], ys[tri_indices[1]]
        x2, y2 = xs[tri_indices[2]], ys[tri_indices[2]]
        horizontal_areas.append(abs((x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)) / 2.0)

    if not horizontal_areas:
        return None
    min_area = max(float(np.median(horizontal_areas)) * 0.05, 1e-6)

    slopes: List[float] = []
    weights: List[float] = []
    for tri_indices, horizontal_area in zip(triang.triangles, horizontal_areas):
        if horizontal_area < min_area:
            continue
        p0 = np.array([xs[tri_indices[0]], ys[tri_indices[0]], zs[tri_indices[0]]])
        p1 = np.array([xs[tri_indices[1]], ys[tri_indices[1]], zs[tri_indices[1]]])
        p2 = np.array([xs[tri_indices[2]], ys[tri_indices[2]], zs[tri_indices[2]]])
        normal = np.cross(p1 - p0, p2 - p0)
        normal_len = float(np.linalg.norm(normal))
        if normal_len < 1e-9:
            continue
        cos_angle = max(-1.0, min(1.0, abs(normal[2]) / normal_len))
        slopes.append(math.degrees(math.acos(cos_angle)))
        weights.append(horizontal_area)

    if not slopes:
        return None
    slopes_arr = np.array(slopes)
    weights_arr = np.array(weights)
    mean_slope = float(np.average(slopes_arr, weights=weights_arr)) if weights_arr.sum() > 0 else float(np.mean(slopes_arr))
    return mean_slope, float(np.max(slopes_arr))


def compute_erosion_risk(
    db: Session,
    boundary_geojson: Dict[str, Any],
    show_raster: bool = False,
    local_elevation_points: Optional[List[Dict[str, float]]] = None,
    analysis_mode: str = "hybrid",
    progress_cb: Optional[Callable[[str, int], None]] = None,
) -> Tuple[float, str, Dict[str, float], bytes]:
    """`analysis_mode` selects one of three distinct pipelines:
    - "satellite": ignores any uploaded local_elevation_points entirely - pure satellite/DEM, the
      same result this function produced before local-data support existed.
    - "local": trusts the surveyor's own data more heavily wherever it's present (a bigger share
      of risk_value goes to the gully-susceptibility factor when geotechnical data was supplied)
      and explicitly reports which required factors had no local equivalent and fell back to
      satellite/DEM (breakdown["local_data_gaps"]).
    - "hybrid" (default, and the same behavior as before this parameter existed): every factor
      blends in local data wherever present and falls back to satellite/DEM automatically and
      silently wherever it's missing, at fixed weights.

    A pragmatic susceptibility index, not a full RUSLE soil-loss model: slope is the dominant
    driver of water erosion, vegetation cover (NDVI) protects soil from being stripped, and
    proximity to a natural drainage channel concentrates erosive flow. This mirrors
    compute_flood_risk's weighted-composite approach rather than pulling in rainfall
    erosivity/soil-erodibility datasets, which would need per-region calibration to be trustworthy.

    When the caller supplies the surveyor's own elevation points, slope is computed directly from
    those (see _compute_local_slope_from_points) instead of the global 30m DEM - noticeably more
    accurate for a single plot. Vegetation and drainage still come from satellite data either way,
    since a handful of elevation points can't tell us anything about ground cover or drainage.

    `local_elevation_points` entries may also carry optional geotechnical/soil fields (cohesion_kpa,
    friction_angle_deg, plasticity_index, silt_vfs_pct, clay_pct, organic_matter_pct,
    soil_structure_code, soil_permeability_code) from an uploaded geotechnical survey. When present,
    these blend a Nigeria-calibrated gully-susceptibility factor into risk_value (see
    hazard_local_data.compute_gully_susceptibility_index) and, if a texture/organic-matter/
    structure/permeability reading is complete, add an informational RUSLE K-factor (soil
    erodibility) to the breakdown. An absolute soil-loss figure (t/ha/yr) would additionally need a
    rainfall-erosivity R-factor, which needs either a sub-daily rainfall-intensity record or a
    region-calibrated regression on monthly rainfall (Arnoldus, 1980) - neither of which this app
    can currently source reliably, so only K-factor is exposed rather than risk an unreliable
    absolute number.
    """
    report = progress_cb or (lambda stage, pct: None)
    report("Connecting to Earth Engine...", 5)
    init_gee()

    analysis_mode = analysis_mode if analysis_mode in ("satellite", "local", "hybrid") else "hybrid"
    if analysis_mode == "satellite":
        local_elevation_points = None

    geom = ee.Geometry(boundary_geojson)
    # Erosion susceptibility is driven by on-site/near-site terrain and cover, not a distant
    # river catchment, so a tighter buffer than flood's 1000m is appropriate here.
    analysis_region = geom.buffer(500)

    dem = ee.ImageCollection("COPERNICUS/DEM/GLO30_2024_1").select("DEM").mosaic()
    slope = ee.Terrain.slope(dem).rename("slope_deg")

    report("Checking elevation data coverage...", 15)
    slope_count = slope.reduceRegion(
        reducer=ee.Reducer.count(), geometry=geom, scale=30, maxPixels=1e9,
    ).get("slope_deg")
    has_dem_data = bool(ee.Number(slope_count).getInfo() or 0)

    # Only points that actually carry an elevation reading feed the local-slope TIN - a mixed
    # upload (some points with elevation, some soil-only) must not silently disable local slope
    # just because one entry has no "elevation_m" key.
    elevation_only_points = [p for p in (local_elevation_points or []) if p.get("elevation_m") is not None]
    local_slope = _compute_local_slope_from_points(elevation_only_points)
    slope_source = "unavailable"
    mean_slope_val = 0.0
    max_slope_val = 0.0
    mean_ndvi_val = 0.3  # neutral fallback if no clear-sky imagery is available
    mean_dist_val = 5000.0

    if local_slope is not None:
        mean_slope_val, max_slope_val = local_slope
        slope_source = "local_survey"
    elif has_dem_data:
        # One combined round-trip instead of two separate .getInfo() calls.
        slope_stats = ee.Dictionary({
            "mean": slope.reduceRegion(reducer=ee.Reducer.mean(), geometry=geom, scale=30, maxPixels=1e9).get("slope_deg"),
            "max": slope.reduceRegion(reducer=ee.Reducer.max(), geometry=geom, scale=30, maxPixels=1e9).get("slope_deg"),
        }).getInfo()
        mean_slope_val = float(slope_stats.get("mean") or 0.0)
        max_slope_val = float(slope_stats.get("max") or 0.0)
        slope_source = "global_dem"

    has_data = bool(local_slope is not None or has_dem_data)

    report("Computing slope from terrain data...", 30)
    if has_dem_data:
        # Vegetation and drainage concentration still need satellite coverage regardless of
        # where the slope number came from.
        report("Analyzing vegetation and drainage...", 45)
        end = date.today()
        start = end - timedelta(days=180)
        collection = (
            ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
            .filterBounds(geom)
            .filterDate(start.isoformat(), end.isoformat())
            .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 80))
            .map(_mask_sentinel_clouds)
        )
        flow_acc = ee.Image("WWF/HydroSHEDS/15ACC").select("b1")
        channels = flow_acc.gt(1000)
        channel_dist = channels.fastDistanceTransform(30).sqrt()
        distance_m = channel_dist.multiply(flow_acc.projection().nominalScale()).rename("distance_m")

        # Same one-round-trip approach: NDVI's image_count gate and the drainage-distance mean
        # don't depend on each other, so fetch both in a single combined call.
        veg_drainage = ee.Dictionary({
            "image_count": collection.size(),
            "mean_dist": distance_m.reduceRegion(reducer=ee.Reducer.mean(), geometry=geom, scale=463, maxPixels=1e9).get("distance_m"),
        }).getInfo()
        if veg_drainage.get("mean_dist") is not None:
            mean_dist_val = float(veg_drainage["mean_dist"])
        if int(veg_drainage.get("image_count") or 0) > 0:
            composite = collection.median()
            ndvi = composite.normalizedDifference(["B8", "B4"]).rename("ndvi")
            sampled_ndvi = ndvi.reduceRegion(
                reducer=ee.Reducer.mean(), geometry=geom, scale=10, maxPixels=1e9,
            ).get("ndvi").getInfo()
            if sampled_ndvi is not None:
                mean_ndvi_val = float(sampled_ndvi)

    # Normalize each factor to 0..1, where 1 = maximum contribution to erosion risk.
    slope_score = max(0.0, min(1.0, mean_slope_val / 25.0))
    vegetation_score = max(0.0, min(1.0, 1.0 - (mean_ndvi_val / 0.6)))
    drainage_score = max(0.0, min(1.0, 1.0 - (mean_dist_val / 500.0)))

    # An uploaded geotechnical survey (cohesion/friction angle/plasticity index) lets us fold in a
    # Nigeria-calibrated gully-susceptibility factor. When present, it takes a real share of the
    # composite (reweighted below) rather than just being tacked on informationally, since it's a
    # genuine independent risk driver the satellite-only composite can't see.
    soil_summary = summarize_local_soil_points(local_elevation_points or []) or {}
    gully_index = compute_gully_susceptibility_index(
        soil_summary.get("cohesion_kpa"),
        soil_summary.get("friction_angle_deg"),
        soil_summary.get("plasticity_index"),
        mean_slope_val,
    )

    if gully_index is not None:
        # "local" mode trusts the surveyor's own geotechnical reading more than the fixed hybrid
        # split - it's meant to run primarily off the uploaded data, not treat it as one input
        # among several weighted the same way regardless of mode.
        factor_weights = (
            {"slope": 0.30, "vegetation": 0.20, "drainage": 0.10, "gully": 0.40}
            if analysis_mode == "local"
            else {"slope": 0.40, "vegetation": 0.25, "drainage": 0.15, "gully": 0.20}
        )
    else:
        factor_weights = {"slope": 0.5, "vegetation": 0.3, "drainage": 0.2}

    risk_value = (
        slope_score * factor_weights["slope"]
        + vegetation_score * factor_weights["vegetation"]
        + drainage_score * factor_weights["drainage"]
        + (gully_index * factor_weights["gully"] if gully_index is not None else 0.0)
    )
    if not has_data:
        risk_value = 0.0

    # K-factor (RUSLE soil erodibility, informational) - only computable when the survey gives a
    # complete soil-texture/organic-matter/structure/permeability reading; otherwise omitted rather
    # than guessed. Deliberately not extended into an absolute soil-loss (t/ha/yr) figure - that
    # would additionally need a rainfall-erosivity R-factor this app has no reliable way to source.
    k_factor = None
    texture_fields = ("silt_vfs_pct", "clay_pct", "organic_matter_pct", "soil_structure_code", "soil_permeability_code")
    if all(f in soil_summary for f in texture_fields):
        k_factor = compute_k_factor(
            soil_summary["silt_vfs_pct"], soil_summary["clay_pct"], soil_summary["organic_matter_pct"],
            soil_summary["soil_structure_code"], soil_summary["soil_permeability_code"],
        )

    # Transparency + confidence: exactly which source fed each factor in risk_value, and a
    # 0-100 "input data confidence" score derived from that mix (see compute_confidence_score's
    # docstring for what it does and doesn't claim).
    factor_sources = {
        "slope": slope_source if slope_source != "unavailable" else "not_available",
        "vegetation": "satellite_ndvi" if has_dem_data else "not_available",
        "drainage": "satellite_hydrosheds" if has_dem_data else "not_available",
    }
    if gully_index is not None:
        factor_sources["gully"] = "user_input"

    try:
        plot_area_ha = float(geom.area(1).getInfo()) / 10000.0
    except Exception:
        plot_area_ha = 0.0
    confidence = compute_confidence_score(
        factor_sources, factor_weights, local_point_count=len(elevation_only_points), plot_area_ha=plot_area_ha,
    )

    local_data_gaps: List[str] = []
    if analysis_mode == "local":
        if factor_sources["slope"] != "local_survey":
            local_data_gaps.append("Slope — no local elevation survey provided, used the global 30m DEM instead.")
        if gully_index is None:
            local_data_gaps.append("Gully susceptibility — no geotechnical survey (cohesion/friction angle/plasticity) provided.")
        local_data_gaps.append("Vegetation cover — always estimated from satellite imagery; there's no local equivalent to collect.")
        local_data_gaps.append("Drainage proximity — always estimated from the satellite-derived stream network.")

    breakdown = {
        "mean_slope_deg": round(mean_slope_val, 2),
        "max_slope_deg": round(max_slope_val, 2),
        "mean_ndvi": round(mean_ndvi_val, 3),
        "distance_to_drainage_m": round(mean_dist_val, 1),
        "slope_score": round(slope_score, 3),
        "vegetation_score": round(vegetation_score, 3),
        "drainage_score": round(drainage_score, 3),
        "data_available": has_data,
        "slope_source": slope_source,
        "local_soil_data_available": bool(soil_summary),
        "gully_susceptibility_index": gully_index,
        "k_factor": k_factor,
        "analysis_mode": analysis_mode,
        "data_sources": factor_sources,
        "confidence": confidence,
        "local_data_gaps": local_data_gaps,
        "_references": EROSION_REFERENCES + LOCAL_DATA_REFERENCES if soil_summary else EROSION_REFERENCES,
    }

    risk_class, class_color = classify_risk(risk_value, has_data)

    # Real graduated slope surface + real buildings, mirroring compute_flood_risk's map upgrade.
    # When slope came from the surveyor's own points, the map is built from that same local TIN
    # (per-triangle facets) rather than the coarser global DEM point cloud. These three fetches
    # are independent (two Earth Engine calls plus one Postgres query) - run them concurrently
    # rather than serially, for the same reason as compute_flood_risk.
    report("Locating nearby buildings...", 65)
    with ThreadPoolExecutor(max_workers=3) as pool:
        slope_points_future = pool.submit(
            lambda: None if local_slope is not None else (_fetch_slope_points(dem, analysis_region) if has_dem_data else None)
        )
        contour_future = pool.submit(
            lambda: elevation_only_points if local_slope is not None else fetch_dem_elevation_points(boundary_geojson, buffer_m=500)
        )
        buildings_future = pool.submit(fetch_buildings_near, db, boundary_geojson, 500)
        dem_slope_points = slope_points_future.result()
        contour_points = contour_future.result()
        buildings = buildings_future.result()

    report("Rendering hazard map...", 85)
    png_bytes, map_stats = render_erosion_hazard_map(
        boundary_geojson=boundary_geojson,
        local_elevation_points=elevation_only_points if local_slope is not None else None,
        dem_slope_points=dem_slope_points,
        contour_points=contour_points,
        buildings=buildings,
        risk_class=risk_class,
        class_color=class_color,
        buffer_m=500,
    )
    breakdown["buildings_total"] = map_stats.get("buildings_total", 0)
    breakdown["buildings_threatened"] = map_stats.get("buildings_threatened", 0)
    breakdown["_gis_export"] = {
        "boundary_geojson": boundary_geojson,
        "buildings_gdf": map_stats.get("buildings_gdf"),
        "value_points": map_stats.get("value_points"),
        "value_key": map_stats.get("value_key", "slope_deg"),
    }
    breakdown["_interactive"] = map_stats.get("interactive")

    report("Finalizing report...", 95)
    return risk_value, risk_class, breakdown, png_bytes


def overlay_to_data_url(png_bytes: bytes) -> str:
    encoded = base64.b64encode(png_bytes).decode("utf-8")
    return f"data:image/png;base64,{encoded}"


def overlay_to_bytes_io(png_bytes: bytes) -> io.BytesIO:
    return io.BytesIO(png_bytes)
