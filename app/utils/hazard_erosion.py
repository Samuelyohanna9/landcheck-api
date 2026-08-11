from __future__ import annotations

import base64
import io
from datetime import date, timedelta
from typing import Any, Dict, Tuple

import ee
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


def compute_erosion_risk(
    boundary_geojson: Dict[str, Any],
    show_raster: bool = False,
) -> Tuple[float, str, Dict[str, float], bytes]:
    """A pragmatic susceptibility index, not a full RUSLE soil-loss model: slope (from a global
    30m DEM) is the dominant driver of water erosion, vegetation cover (NDVI) protects soil from
    being stripped, and proximity to a natural drainage channel concentrates erosive flow. This
    mirrors compute_flood_risk's weighted-composite approach rather than pulling in rainfall
    erosivity/soil-erodibility datasets, which would need per-region calibration to be trustworthy.
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
    has_data = bool(ee.Number(slope_count).getInfo() or 0)

    mean_slope_val = 0.0
    max_slope_val = 0.0
    mean_ndvi_val = 0.3  # neutral fallback if no clear-sky imagery is available
    mean_dist_val = 5000.0

    if has_data:
        mean_slope_val = float(slope.reduceRegion(
            reducer=ee.Reducer.mean(), geometry=geom, scale=30, maxPixels=1e9,
        ).get("slope_deg").getInfo() or 0.0)
        max_slope_val = float(slope.reduceRegion(
            reducer=ee.Reducer.max(), geometry=geom, scale=30, maxPixels=1e9,
        ).get("slope_deg").getInfo() or 0.0)

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
