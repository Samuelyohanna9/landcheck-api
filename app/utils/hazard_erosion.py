from __future__ import annotations

import base64
import io
import math
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import ee
import geopandas as gpd
import matplotlib.tri as mtri
import numpy as np
import requests

from app.utils.gee_client import init_gee
from app.utils.hazard_common import classify_risk

# Same cloud-mask idiom used for the Green module's NDVI work (green_remote_monitoring.py) -
# QA60 bits 10/11 flag cloud and cirrus pixels.
def _mask_sentinel_clouds(image: ee.Image) -> ee.Image:
    qa = image.select("QA60")
    cloud_mask = qa.bitwiseAnd(1 << 10).eq(0)
    cirrus_mask = qa.bitwiseAnd(1 << 11).eq(0)
    return image.updateMask(cloud_mask.And(cirrus_mask)).copyProperties(image, image.propertyNames())


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
    boundary_geojson: Dict[str, Any],
    show_raster: bool = False,
    local_elevation_points: Optional[List[Dict[str, float]]] = None,
) -> Tuple[float, str, Dict[str, float], bytes]:
    """A pragmatic susceptibility index, not a full RUSLE soil-loss model: slope is the dominant
    driver of water erosion, vegetation cover (NDVI) protects soil from being stripped, and
    proximity to a natural drainage channel concentrates erosive flow. This mirrors
    compute_flood_risk's weighted-composite approach rather than pulling in rainfall
    erosivity/soil-erodibility datasets, which would need per-region calibration to be trustworthy.

    When the caller supplies the surveyor's own elevation points, slope is computed directly from
    those (see _compute_local_slope_from_points) instead of the global 30m DEM - noticeably more
    accurate for a single plot. Vegetation and drainage still come from satellite data either way,
    since a handful of elevation points can't tell us anything about ground cover or drainage.
    """
    init_gee()

    geom = ee.Geometry(boundary_geojson)
    # Erosion susceptibility is driven by on-site/near-site terrain and cover, not a distant
    # river catchment, so a tighter buffer than flood's 1000m is appropriate here.
    analysis_region = geom.buffer(500)

    dem = ee.ImageCollection("COPERNICUS/DEM/GLO30_2024_1").select("DEM").mosaic()
    slope = ee.Terrain.slope(dem).rename("slope_deg")

    slope_count = slope.reduceRegion(
        reducer=ee.Reducer.count(), geometry=geom, scale=30, maxPixels=1e9,
    ).get("slope_deg")
    has_dem_data = bool(ee.Number(slope_count).getInfo() or 0)

    local_slope = _compute_local_slope_from_points(local_elevation_points or [])
    slope_source = "unavailable"
    mean_slope_val = 0.0
    max_slope_val = 0.0
    mean_ndvi_val = 0.3  # neutral fallback if no clear-sky imagery is available
    mean_dist_val = 5000.0

    if local_slope is not None:
        mean_slope_val, max_slope_val = local_slope
        slope_source = "local_survey"
    elif has_dem_data:
        mean_slope_val = float(slope.reduceRegion(
            reducer=ee.Reducer.mean(), geometry=geom, scale=30, maxPixels=1e9,
        ).get("slope_deg").getInfo() or 0.0)
        max_slope_val = float(slope.reduceRegion(
            reducer=ee.Reducer.max(), geometry=geom, scale=30, maxPixels=1e9,
        ).get("slope_deg").getInfo() or 0.0)
        slope_source = "global_dem"

    has_data = bool(local_slope is not None or has_dem_data)

    if has_dem_data:
        # Vegetation and drainage concentration still need satellite coverage regardless of
        # where the slope number came from.
        end = date.today()
        start = end - timedelta(days=180)
        collection = (
            ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
            .filterBounds(geom)
            .filterDate(start.isoformat(), end.isoformat())
            .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 80))
            .map(_mask_sentinel_clouds)
        )
        image_count = int(collection.size().getInfo() or 0)
        if image_count > 0:
            composite = collection.median()
            ndvi = composite.normalizedDifference(["B8", "B4"]).rename("ndvi")
            sampled_ndvi = ndvi.reduceRegion(
                reducer=ee.Reducer.mean(), geometry=geom, scale=10, maxPixels=1e9,
            ).get("ndvi").getInfo()
            if sampled_ndvi is not None:
                mean_ndvi_val = float(sampled_ndvi)

        flow_acc = ee.Image("WWF/HydroSHEDS/15ACC").select("b1")
        channels = flow_acc.gt(1000)
        channel_dist = channels.fastDistanceTransform(30).sqrt()
        distance_m = channel_dist.multiply(flow_acc.projection().nominalScale()).rename("distance_m")
        sampled_dist = distance_m.reduceRegion(
            reducer=ee.Reducer.mean(), geometry=geom, scale=463, maxPixels=1e9,
        ).get("distance_m").getInfo()
        if sampled_dist is not None:
            mean_dist_val = float(sampled_dist)

    # Normalize each factor to 0..1, where 1 = maximum contribution to erosion risk.
    slope_score = max(0.0, min(1.0, mean_slope_val / 25.0))
    vegetation_score = max(0.0, min(1.0, 1.0 - (mean_ndvi_val / 0.6)))
    drainage_score = max(0.0, min(1.0, 1.0 - (mean_dist_val / 500.0)))

    risk_value = (slope_score * 0.5) + (vegetation_score * 0.3) + (drainage_score * 0.2)
    if not has_data:
        risk_value = 0.0

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
    }

    risk_class, class_color = classify_risk(risk_value, has_data)

    hillshade = ee.Terrain.hillshade(dem).visualize(min=0, max=255, palette=["#0b1220", "#64748b", "#e2e8f0"])
    slope_vis = slope.visualize(min=0, max=30, palette=["#fef9c3", "#f97316", "#7c2d12"], opacity=0.6)
    boundary = ee.Image().paint(geom, 1, 2).visualize(palette=["#0f172a"])

    class_fill = ee.Image().paint(geom, 1).visualize(palette=[class_color], opacity=0.25)
    class_outline = ee.Image().paint(geom, 1, 3).visualize(palette=[class_color])

    # Diagonal hatch overlay for the plot class, matching compute_flood_risk's treatment so both
    # hazard maps read as one consistent product family.
    stripe_x = ee.Image.pixelCoordinates(analysis_region.projection()).select("x")
    hatch = stripe_x.mod(20).lt(2)
    hatch_overlay = hatch.updateMask(hatch).visualize(palette=[class_color], opacity=0.6)
    hatch_overlay = hatch_overlay.clip(geom)

    layers = [hillshade, class_fill, hatch_overlay, class_outline, boundary]
    if show_raster:
        layers.insert(1, slope_vis)

    overlay = ee.ImageCollection(layers).mosaic().clip(analysis_region)
    thumb_url = overlay.getThumbURL({
        "region": analysis_region,
        "dimensions": 1024,
        "format": "png",
    })

    resp = requests.get(thumb_url, timeout=30)
    resp.raise_for_status()
    return risk_value, risk_class, breakdown, resp.content


def overlay_to_data_url(png_bytes: bytes) -> str:
    encoded = base64.b64encode(png_bytes).decode("utf-8")
    return f"data:image/png;base64,{encoded}"


def overlay_to_bytes_io(png_bytes: bytes) -> io.BytesIO:
    return io.BytesIO(png_bytes)
