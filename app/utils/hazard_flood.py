from __future__ import annotations

import base64
import io
from typing import Any, Dict, Tuple

import ee
import requests

from app.utils.gee_client import init_gee


def compute_flood_risk(
    boundary_geojson: Dict[str, Any],
    show_raster: bool = False,
    return_period: int = 100,
) -> Tuple[float, str, Dict[str, float], bytes]:
    init_gee()

    geom = ee.Geometry(boundary_geojson)
    analysis_region = geom.buffer(1000)

    # JRC/CEMS GloFAS Flood Hazard depth (meters) for selected return period
    rp_band = f"RP{return_period}_depth"
    flood = ee.ImageCollection("JRC/CEMS_GLOFAS/FloodHazard/v2_1").mosaic().select(rp_band)
    depth = flood.rename("depth_m")

    # Components
    flow_acc = ee.Image("WWF/HydroSHEDS/15ACC").select("b1")
    rivers = flow_acc.gt(1000)
    river_dist = rivers.fastDistanceTransform(30).sqrt()
    distance_m = river_dist.multiply(flow_acc.projection().nominalScale()).rename("distance_m")
    valid_count = depth.reduceRegion(
        reducer=ee.Reducer.count(),
        geometry=geom,
        scale=90,
        maxPixels=1e9,
    ).get("depth_m")

    inundation = depth.gt(0).rename("inundation")
    mean_dist = distance_m.reduceRegion(
        reducer=ee.Reducer.mean(),
        geometry=geom,
        scale=463,
        maxPixels=1e9,
    ).get("distance_m")
    mean_depth = depth.reduceRegion(
        reducer=ee.Reducer.mean(),
        geometry=geom,
        scale=90,
        maxPixels=1e9,
    ).get("depth_m")
    max_depth = depth.reduceRegion(
        reducer=ee.Reducer.max(),
        geometry=geom,
        scale=90,
        maxPixels=1e9,
    ).get("depth_m")
    inundation_fraction = inundation.reduceRegion(
        reducer=ee.Reducer.mean(),
        geometry=geom,
        scale=90,
        maxPixels=1e9,
    ).get("inundation")

    has_data = ee.Number(valid_count).gt(0)
    mean_depth_val = ee.Number(ee.Algorithms.If(has_data, mean_depth, 0))
    max_depth_val = ee.Number(ee.Algorithms.If(has_data, max_depth, 0))
    inundation_val = ee.Number(ee.Algorithms.If(has_data, inundation_fraction, 0))
    mean_dist_val = ee.Number(ee.Algorithms.If(has_data, mean_dist, 0))

    # Normalize depth to 0..1 using 3m as a practical upper bound
    depth_score_val = mean_depth_val.divide(3).min(1).max(0)
    inundation_score_val = inundation_val.min(1).max(0)
    river_proximity_score_val = ee.Number(1).subtract(mean_dist_val.divide(5000)).min(1).max(0)

    risk_value = (
        depth_score_val.multiply(0.6)
        .add(inundation_score_val.multiply(0.25))
        .add(river_proximity_score_val.multiply(0.15))
        .getInfo()
    )

    breakdown = {
        "mean_depth_m": float(mean_depth_val.getInfo() or 0.0),
        "max_depth_m": float(max_depth_val.getInfo() or 0.0),
        "inundation_fraction": float(inundation_val.getInfo() or 0.0),
        "depth_score": float(depth_score_val.getInfo() or 0.0),
        "inundation_score": float(inundation_score_val.getInfo() or 0.0),
        "distance_to_river_m": float(mean_dist_val.getInfo() or 0.0),
        "river_proximity_score": float(river_proximity_score_val.getInfo() or 0.0),
        "data_available": bool(has_data.getInfo()),
    }

    if risk_value is None:
        risk_value = (
            breakdown["depth_score"] * 0.6
            + breakdown["inundation_score"] * 0.25
            + breakdown["river_proximity_score"] * 0.15
        )
    risk_value = float(risk_value)

    if breakdown["data_available"] is False:
        risk_class = "No Data"
        risk_value = 0.0
    elif risk_value < 0.3:
        risk_class = "Low"
    elif risk_value < 0.6:
        risk_class = "Moderate"
    else:
        risk_class = "High"

    palette = ["#e0f2fe", "#7dd3fc", "#0ea5e9", "#1d4ed8"]
    depth_for_vis = depth.unmask(0)
    hillshade = depth_for_vis.visualize(min=0, max=3, palette=["#0b1220", "#64748b", "#e2e8f0"])
    risk_vis = depth_for_vis.visualize(min=0.1, max=3, palette=palette, opacity=0.6)
    boundary = ee.Image().paint(geom, 1, 2).visualize(palette=["#0f172a"])

    class_colors = {
        "Low": "#22c55e",
        "Moderate": "#f59e0b",
        "High": "#ef4444",
        "No Data": "#94a3b8",
    }
    class_color = class_colors.get(risk_class, "#22c55e")
    class_fill = ee.Image().paint(geom, 1).visualize(palette=[class_color], opacity=0.25)
    class_outline = ee.Image().paint(geom, 1, 3).visualize(palette=[class_color])

    # Simple hatch pattern for plot class overlay (diagonal stripes)
    stripe_x = ee.Image.pixelCoordinates(analysis_region.projection()).select("x")
    hatch = stripe_x.mod(20).lt(2)
    hatch_overlay = hatch.updateMask(hatch).visualize(palette=[class_color], opacity=0.6)
    hatch_overlay = hatch_overlay.clip(geom)

    layers = [hillshade, class_fill, hatch_overlay, class_outline, boundary]
    if show_raster:
        layers.insert(1, risk_vis)

    overlay = ee.ImageCollection(layers).mosaic().clip(analysis_region)

    thumb_url = overlay.getThumbURL(
        {
            "region": analysis_region,
            "dimensions": 1024,
            "format": "png",
        }
    )

    resp = requests.get(thumb_url, timeout=30)
    resp.raise_for_status()
    return risk_value, risk_class, breakdown, resp.content


def overlay_to_data_url(png_bytes: bytes) -> str:
    encoded = base64.b64encode(png_bytes).decode("utf-8")
    return f"data:image/png;base64,{encoded}"


def overlay_to_bytes_io(png_bytes: bytes) -> io.BytesIO:
    return io.BytesIO(png_bytes)
