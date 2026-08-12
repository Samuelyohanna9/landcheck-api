from __future__ import annotations

import base64
import io
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

import ee
from sqlalchemy.orm import Session

from app.utils.elevation import fetch_dem_elevation_points
from app.utils.gee_client import init_gee
from app.utils.hazard_common import classify_risk, fetch_buildings_near
from app.utils.hazard_map_renderer import render_flood_hazard_map


def _fetch_depth_points(depth_image: "ee.Image", region: "ee.Geometry", scale_m: int = 90) -> Optional[List[Dict[str, float]]]:
    """Samples the flood depth band as a {lng, lat, depth_m} point cloud over the analysis
    region, the same pixelLonLat + toList idiom used by elevation.py's DEM sampler - this is
    what lets the map show a real graduated depth surface instead of a single flat risk color.
    """
    try:
        sampled = depth_image.addBands(ee.Image.pixelLonLat())
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
        depths = info.get("depth_m") or []
        if len(lons) < 3 or len(lons) != len(lats) or len(lons) != len(depths):
            return None
        points = [
            {"lng": float(lo), "lat": float(la), "depth_m": float(d)}
            for lo, la, d in zip(lons, lats, depths)
            if d is not None
        ]
        return points if len(points) >= 3 else None
    except Exception:
        return None


def _fetch_susceptibility_points(image: "ee.Image", region: "ee.Geometry", scale_m: int = 90) -> Optional[List[Dict[str, float]]]:
    """Samples the local terrain-based flood susceptibility composite (0-100) as a
    {lng, lat, flood_susceptibility_pct} point cloud - the same pixelLonLat + toList idiom as
    _fetch_depth_points, used only for the fallback map when GloFAS has no modeled extent here.
    """
    try:
        sampled = image.multiply(100).rename("flood_susceptibility_pct").addBands(ee.Image.pixelLonLat())
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
        values = info.get("flood_susceptibility_pct") or []
        if len(lons) < 3 or len(lons) != len(lats) or len(lons) != len(values):
            return None
        points = [
            {"lng": float(lo), "lat": float(la), "flood_susceptibility_pct": float(v)}
            for lo, la, v in zip(lons, lats, values)
            if v is not None
        ]
        return points if len(points) >= 3 else None
    except Exception:
        return None


def compute_flood_risk(
    db: Session,
    boundary_geojson: Dict[str, Any],
    show_raster: bool = False,
    return_period: int = 100,
    local_elevation_points: Optional[List[Dict[str, float]]] = None,
    progress_cb: Optional[Callable[[str, int], None]] = None,
) -> Tuple[float, str, Dict[str, float], bytes]:
    report = progress_cb or (lambda stage, pct: None)
    report("Connecting to Earth Engine...", 5)
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

    risk_value_ee = (
        depth_score_val.multiply(0.6)
        .add(inundation_score_val.multiply(0.25))
        .add(river_proximity_score_val.multiply(0.15))
    )

    # A single combined round-trip instead of 8 separate .getInfo() calls - each one is its own
    # network request to Earth Engine, and on a slower link those add up fast enough to blow past
    # a client-side request timeout even though every individual call is fine on its own.
    report("Downloading satellite flood data...", 25)
    combined = ee.Dictionary({
        "risk_value": risk_value_ee,
        "mean_depth_m": mean_depth_val,
        "max_depth_m": max_depth_val,
        "inundation_fraction": inundation_val,
        "depth_score": depth_score_val,
        "inundation_score": inundation_score_val,
        "distance_to_river_m": mean_dist_val,
        "river_proximity_score": river_proximity_score_val,
        "data_available": has_data,
    }).getInfo()

    breakdown = {
        "mean_depth_m": float(combined.get("mean_depth_m") or 0.0),
        "max_depth_m": float(combined.get("max_depth_m") or 0.0),
        "inundation_fraction": float(combined.get("inundation_fraction") or 0.0),
        "depth_score": float(combined.get("depth_score") or 0.0),
        "inundation_score": float(combined.get("inundation_score") or 0.0),
        "distance_to_river_m": float(combined.get("distance_to_river_m") or 0.0),
        "river_proximity_score": float(combined.get("river_proximity_score") or 0.0),
        "data_available": bool(combined.get("data_available")),
    }

    risk_value = combined.get("risk_value")
    if risk_value is None:
        risk_value = (
            breakdown["depth_score"] * 0.6
            + breakdown["inundation_score"] * 0.25
            + breakdown["river_proximity_score"] * 0.15
        )
    risk_value = float(risk_value)
    if breakdown["data_available"] is False:
        risk_value = 0.0

    risk_class, class_color = classify_risk(risk_value, breakdown["data_available"])
    breakdown["flood_data_source"] = "glofas"
    susceptibility_img = None

    # GloFAS only carries depth pixels within its modeled river-flood extent - a plot far from any
    # major river channel legitimately has zero coverage there, which used to just surface as
    # "No Data". Rather than leaving the user with nothing, fall back to a local terrain-based
    # ponding/pluvial susceptibility estimate: flatter, lower-lying ground near a drainage line is
    # more prone to standing water even when no river ever reaches it. This is a genuinely
    # different methodology from GloFAS's river-routing simulation, so it's always labeled as an
    # estimate (both in the API response and baked into the map itself), never presented as if it
    # were GloFAS data.
    if not breakdown["data_available"]:
        report("No river flood extent modeled here — estimating local terrain-based susceptibility...", 35)
        try:
            dem_proxy = ee.ImageCollection("COPERNICUS/DEM/GLO30_2024_1").select("DEM").mosaic()
            slope_img = ee.Terrain.slope(dem_proxy).rename("slope_deg")
            local_mean_elev_img = dem_proxy.focal_mean(radius=300, units="meters")
            depression_img = local_mean_elev_img.subtract(dem_proxy).rename("depression_m")

            # Each factor normalized 0..1: flat + low-lying + close to a drainage line all raise
            # ponding susceptibility, weighted by how directly each one drives standing water.
            flatness_score_img = ee.Image(1).subtract(slope_img.divide(15).min(1)).max(0)
            drainage_score_img = ee.Image(1).subtract(distance_m.divide(1500).min(1)).max(0)
            depression_score_img = depression_img.divide(3).max(0).min(1)
            susceptibility_img = (
                depression_score_img.multiply(0.40)
                .add(flatness_score_img.multiply(0.35))
                .add(drainage_score_img.multiply(0.25))
            ).rename("susceptibility")

            proxy_combined = ee.Dictionary({
                "susceptibility": susceptibility_img.reduceRegion(
                    reducer=ee.Reducer.mean(), geometry=geom, scale=30, maxPixels=1e9
                ).get("susceptibility"),
                "mean_slope_deg": slope_img.reduceRegion(
                    reducer=ee.Reducer.mean(), geometry=geom, scale=30, maxPixels=1e9
                ).get("slope_deg"),
                "mean_depression_m": depression_img.reduceRegion(
                    reducer=ee.Reducer.mean(), geometry=geom, scale=30, maxPixels=1e9
                ).get("depression_m"),
                "distance_to_drainage_m": mean_dist,
            }).getInfo()

            susceptibility_val = proxy_combined.get("susceptibility")
            if susceptibility_val is not None:
                risk_value = max(0.0, min(1.0, float(susceptibility_val)))
                risk_class, class_color = classify_risk(risk_value, True)
                breakdown["data_available"] = True
                breakdown["flood_data_source"] = "local_terrain_proxy"
                breakdown["distance_to_river_m"] = round(float(proxy_combined.get("distance_to_drainage_m") or 0.0), 1)
                breakdown["terrain_slope_deg"] = round(float(proxy_combined.get("mean_slope_deg") or 0.0), 2)
                breakdown["terrain_depression_m"] = round(float(proxy_combined.get("mean_depression_m") or 0.0), 2)
                # Same normalization as the per-pixel score images, applied to the plot-mean scalars -
                # lets the frontend show component bars for the proxy score the same way it already
                # does for GloFAS's depth/inundation/river-proximity breakdown.
                breakdown["terrain_flatness_score"] = round(max(0.0, min(1.0, 1 - (breakdown["terrain_slope_deg"] / 15))), 3)
                breakdown["terrain_drainage_score"] = round(max(0.0, min(1.0, 1 - (breakdown["distance_to_river_m"] / 1500))), 3)
                breakdown["terrain_depression_score"] = round(max(0.0, min(1.0, breakdown["terrain_depression_m"] / 3)), 3)
            else:
                susceptibility_img = None
        except Exception:
            susceptibility_img = None

    # Informational only - deliberately NOT folded into risk_value/risk_class. A single relative-
    # elevation comparison isn't rigorous enough to justify re-weighting a screening score, but a
    # site sitting notably below its surroundings is a genuine, well-established ponding/drainage
    # risk signal worth surfacing to a surveyor who supplied their own elevation data.
    breakdown["local_elevation_used"] = False
    breakdown["relative_elevation_m"] = None
    valid_points = [
        p for p in (local_elevation_points or [])
        if isinstance(p, dict) and p.get("elevation_m") is not None
    ]
    if valid_points:
        try:
            local_mean_elevation = sum(float(p["elevation_m"]) for p in valid_points) / len(valid_points)
            dem = ee.ImageCollection("COPERNICUS/DEM/GLO30_2024_1").select("DEM").mosaic()
            regional_mean = dem.reduceRegion(
                reducer=ee.Reducer.mean(), geometry=analysis_region, scale=30, maxPixels=1e9,
            ).get("DEM").getInfo()
            if regional_mean is not None:
                breakdown["local_elevation_used"] = True
                breakdown["local_mean_elevation_m"] = round(local_mean_elevation, 2)
                breakdown["regional_mean_elevation_m"] = round(float(regional_mean), 2)
                breakdown["relative_elevation_m"] = round(local_mean_elevation - float(regional_mean), 2)
        except Exception:
            pass

    # Real depth surface (for the graduated map) and real building footprints (to flag which
    # specific structures sit in the flood zone) - both fetched only when there's flood coverage
    # here at all, since there's nothing meaningful to overlay otherwise.
    # unmask(0) turns the "no flood here" NoData pixels into explicit 0.0 depth points - without
    # this, reduceRegion's toList reducer silently drops dry pixels entirely, leaving nothing to
    # anchor the triangulated surface to "dry ground" and letting wet-area depths bleed outward
    # across the whole buffer instead of tapering off at the real flood extent.
    #
    # These three fetches are independent of each other (two separate Earth Engine calls plus one
    # Postgres query) - run them concurrently rather than one after another, since each is I/O
    # bound and waiting on them serially is what was pushing total request time past the client's
    # timeout.
    flood_data_source = breakdown.get("flood_data_source", "glofas")
    report("Analyzing terrain and locating nearby buildings...", 50)
    with ThreadPoolExecutor(max_workers=3) as pool:
        if flood_data_source == "local_terrain_proxy" and susceptibility_img is not None:
            surface_future = pool.submit(_fetch_susceptibility_points, susceptibility_img, analysis_region)
        else:
            surface_future = pool.submit(
                lambda: _fetch_depth_points(depth.unmask(0), analysis_region) if flood_data_source == "glofas" else None
            )
        contour_future = pool.submit(
            lambda: local_elevation_points if valid_points else fetch_dem_elevation_points(boundary_geojson, buffer_m=1000)
        )
        buildings_future = pool.submit(fetch_buildings_near, db, boundary_geojson, 600)
        surface_points = surface_future.result()
        contour_points = contour_future.result()
        buildings = buildings_future.result()

    report("Rendering hazard map...", 80)
    png_bytes, map_stats = render_flood_hazard_map(
        boundary_geojson=boundary_geojson,
        depth_points=surface_points if flood_data_source == "glofas" else None,
        susceptibility_points=surface_points if flood_data_source == "local_terrain_proxy" else None,
        contour_points=contour_points,
        buildings=buildings,
        risk_class=risk_class,
        class_color=class_color,
        return_period=return_period,
        buffer_m=1000,
        value_source=flood_data_source if flood_data_source == "local_terrain_proxy" else "glofas",
    )
    breakdown["buildings_total"] = map_stats.get("buildings_total", 0)
    breakdown["buildings_threatened"] = map_stats.get("buildings_threatened", 0)
    # Not part of the JSON response (the router only whitelists specific breakdown fields into
    # its response body) - carried here purely so the GIS export endpoint can reuse the exact
    # same buildings/points the map was drawn from, instead of re-fetching and risking drift.
    breakdown["_gis_export"] = {
        "boundary_geojson": boundary_geojson,
        "buildings_gdf": map_stats.get("buildings_gdf"),
        "value_points": map_stats.get("value_points"),
        "value_key": map_stats.get("value_key", "depth_m"),
    }
    # Pixel<->geo mapping metadata for the frontend's hover/click "identify" tool - lets it turn a
    # mouse position on the rendered PNG back into a real depth value, not just a pretty picture.
    breakdown["_interactive"] = map_stats.get("interactive")

    report("Finalizing report...", 95)
    return risk_value, risk_class, breakdown, png_bytes


def overlay_to_data_url(png_bytes: bytes) -> str:
    encoded = base64.b64encode(png_bytes).decode("utf-8")
    return f"data:image/png;base64,{encoded}"


def overlay_to_bytes_io(png_bytes: bytes) -> io.BytesIO:
    return io.BytesIO(png_bytes)
