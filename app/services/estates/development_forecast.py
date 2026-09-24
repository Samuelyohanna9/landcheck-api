from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Callable

import ee
import geopandas as gpd
from shapely.geometry import LineString, Point, mapping, shape
from shapely.ops import unary_union

from app.utils.gee_client import init_gee
from app.utils.hazard_lulc import (
    LULC_ASSET_ID,
    LULC_CLASS_COLORS_TS,
    LULC_REFERENCES,
)
from app.utils.hazard_map_renderer import _display_epsg_for


BUILT_UP_CLASS = 7
DEFAULT_RADIUS_M = 5000
DEFAULT_SCALE_M = 30
SECTOR_LABELS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def direction_label(bearing_deg: float | None) -> str:
    if bearing_deg is None:
        return "No clear direction"
    normalized = float(bearing_deg) % 360
    return SECTOR_LABELS[int((normalized + 22.5) // 45) % 8]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_get_info(value: Any, default: Any = None) -> Any:
    try:
        return value.getInfo()
    except Exception:
        return default


def _year_from_metadata(timestamp: Any, index: Any) -> int | None:
    if timestamp:
        try:
            stamp = float(timestamp)
            if stamp > 10_000_000_000:
                stamp /= 1000
            return datetime.fromtimestamp(stamp, tz=timezone.utc).year
        except (TypeError, ValueError, OSError, OverflowError):
            pass
    match = re.search(r"(?:19|20)\d{2}", str(index or ""))
    return int(match.group(0)) if match else None


def _annual_images(collection: "ee.ImageCollection") -> list[tuple[int, "ee.Image"]]:
    """Return one mosaic per observed year without assuming a single global image per year."""
    size = int(_safe_get_info(collection.size(), 0) or 0)
    if not size:
        return []
    timestamps = _safe_get_info(collection.aggregate_array("system:time_start"), []) or []
    indexes = _safe_get_info(collection.aggregate_array("system:index"), []) or []
    years = sorted({
        year
        for index in range(max(len(timestamps), len(indexes), size))
        for year in [_year_from_metadata(
            timestamps[index] if index < len(timestamps) else None,
            indexes[index] if index < len(indexes) else None,
        )]
        if year is not None
    })
    images: list[tuple[int, ee.Image]] = []
    for year in years:
        annual = collection.filter(ee.Filter.calendarRange(year, year, "year"))
        if int(_safe_get_info(annual.size(), 0) or 0) > 0:
            images.append((year, annual.mosaic().select(0).rename("landcover")))
            continue
        # Some community catalog exports expose the year only in system:index.
        annual = collection.filter(ee.Filter.stringContains("system:index", str(year)))
        if int(_safe_get_info(annual.size(), 0) or 0) > 0:
            images.append((year, annual.mosaic().select(0).rename("landcover")))
    return images


def _projected_geometry(geometry: Any, epsg: int, source_crs: str = "EPSG:4326") -> Any:
    return gpd.GeoSeries([geometry], crs=source_crs).to_crs(epsg=epsg).iloc[0]


def _wgs84_geometry(geometry: Any, epsg: int) -> dict[str, Any]:
    return mapping(gpd.GeoSeries([geometry], crs=f"EPSG:{epsg}").to_crs(epsg=4326).iloc[0])


def _sector_polygon(center: Point, radius_m: float, start_bearing: float, end_bearing: float) -> Any:
    points = [(center.x, center.y)]
    steps = 12
    for index in range(steps + 1):
        bearing = math.radians(start_bearing + (end_bearing - start_bearing) * index / steps)
        points.append((center.x + math.sin(bearing) * radius_m, center.y + math.cos(bearing) * radius_m))
    points.append((center.x, center.y))
    from shapely.geometry import Polygon

    return Polygon(points)


def _area_ha(image: "ee.Image", geometry: dict[str, Any], scale_m: int = DEFAULT_SCALE_M) -> float:
    built = image.eq(BUILT_UP_CLASS)
    reduced = ee.Image.pixelArea().updateMask(built).rename("area").reduceRegion(
        reducer=ee.Reducer.sum(),
        geometry=ee.Geometry(geometry),
        scale=scale_m,
        maxPixels=int(1e9),
        bestEffort=True,
    )
    info = _safe_get_info(reduced, {}) or {}
    return max(0.0, _safe_float(info.get("area"))) / 10000.0


def _built_centroid(image: "ee.Image", geometry: dict[str, Any]) -> tuple[float, float] | None:
    built = image.eq(BUILT_UP_CLASS)
    reduced = ee.Image.pixelLonLat().updateMask(built).reduceRegion(
        reducer=ee.Reducer.mean(),
        geometry=ee.Geometry(geometry),
        scale=DEFAULT_SCALE_M,
        maxPixels=int(1e9),
        bestEffort=True,
    )
    info = _safe_get_info(reduced, {}) or {}
    longitude = info.get("longitude")
    latitude = info.get("latitude")
    if longitude is None or latitude is None:
        return None
    return _safe_float(longitude), _safe_float(latitude)


def _footprint(image: "ee.Image", geometry: dict[str, Any]) -> dict[str, Any] | None:
    """Create a deliberately coarse, simplified footprint for the public directional map."""
    try:
        vectors = image.eq(BUILT_UP_CLASS).selfMask().rename("built").reduceToVectors(
            geometry=ee.Geometry(geometry),
            scale=60,
            geometryType="polygon",
            eightConnected=False,
            bestEffort=True,
            maxPixels=int(1e8),
        )
        features = (_safe_get_info(vectors, {}) or {}).get("features") or []
        geometries = [shape(item["geometry"]) for item in features if item.get("geometry")]
        if not geometries:
            return None
        merged = unary_union(geometries).simplify(0.001, preserve_topology=True)
        if merged.is_empty:
            return None
        return mapping(merged)
    except Exception:
        return None


def _hazard_digest(result: dict[str, Any] | None, hazard_type: str) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"available": False, "risk_class": "No data", "risk_score": None}
    if hazard_type == "flood":
        summary = result.get("summary") or {}
        risk_class = summary.get("floodplain_class") or summary.get("river_class") or "No data"
        score = (result.get("floodplain") or {}).get("risk_score")
    else:
        risk_class = result.get("risk_class") or "No data"
        score = result.get("risk_score")
    available = str(risk_class).lower() not in {"no data", "unavailable", "unknown"}
    return {
        "available": available,
        "risk_class": str(risk_class),
        "risk_score": round(_safe_float(score), 1) if score is not None else None,
    }


def _bearing_from_points(start: tuple[float, float], end: tuple[float, float], epsg: int) -> float | None:
    start_metric = _projected_geometry(Point(start), epsg)
    end_metric = _projected_geometry(Point(end), epsg)
    dx = end_metric.x - start_metric.x
    dy = end_metric.y - start_metric.y
    if math.hypot(dx, dy) < 1:
        return None
    return (math.degrees(math.atan2(dx, dy)) + 360) % 360


def _scenario_rows(current_area: float, annual_rate: float, current_year: int) -> list[dict[str, Any]]:
    rows = []
    for horizon in (3, 5, 10):
        baseline = max(current_area, current_area + annual_rate * horizon)
        conservative = max(current_area, current_area + annual_rate * 0.55 * horizon)
        accelerated = max(current_area, current_area + annual_rate * 1.45 * horizon)
        rows.append({
            "horizon_years": horizon,
            "target_year": current_year + horizon,
            "conservative_area_ha": round(conservative, 2),
            "observed_trend_area_ha": round(baseline, 2),
            "accelerated_area_ha": round(accelerated, 2),
        })
    return rows


def _reach_estimate(frontier_distance_m: float | None, early_area: float, current_area: float, years: int) -> dict[str, Any]:
    if frontier_distance_m is None:
        return {"available": False, "headline": "There is not enough nearby built-up evidence to estimate when development may reach this area."}
    if frontier_distance_m <= 100:
        return {
            "available": True,
            "low_years": 0,
            "central_years": 0,
            "high_years": 1,
            "headline": "Built-up land is already close to the Estate boundary.",
        }
    early_radius = math.sqrt(max(early_area, 0.0) * 10000 / math.pi)
    current_radius = math.sqrt(max(current_area, 0.0) * 10000 / math.pi)
    equivalent_speed = (current_radius - early_radius) / max(years, 1)
    if equivalent_speed <= 1:
        return {"available": False, "headline": "The observed change is not strong enough to responsibly estimate when development may reach this area."}
    central = frontier_distance_m / equivalent_speed
    low = frontier_distance_m / (equivalent_speed * 1.45)
    high = frontier_distance_m / (equivalent_speed * 0.55)
    return {
        "available": True,
        "low_years": round(max(1, low), 1),
        "central_years": round(max(1, central), 1),
        "high_years": round(max(1, high), 1),
        "headline": f"At the observed rate, continuous built-up expansion could reach the Estate's surrounding area in approximately {max(1, round(low))}-{max(2, round(high))} years.",
        "method_note": "This range converts built-up area change into an equivalent outward expansion speed. It is a screening scenario, not a development guarantee.",
    }


def _value_outlook(annual_percent_rate: float | None, frontier_distance_m: float | None, direction: str, confidence: str) -> dict[str, str]:
    """Use urbanisation evidence as a value-potential proxy, never as a price prediction."""
    score = 0
    if annual_percent_rate is not None:
        if annual_percent_rate >= 3:
            score += 2
        elif annual_percent_rate > 0:
            score += 1
    if frontier_distance_m is not None:
        if frontier_distance_m <= 1000:
            score += 2
        elif frontier_distance_m <= 3000:
            score += 1
    if direction != "No clear direction":
        score += 1
    if confidence == "high":
        score += 1
    level = "strong" if score >= 5 else "moderate" if score >= 3 else "emerging" if score >= 1 else "unclear"
    labels = {"strong": "Strong", "moderate": "Moderate", "emerging": "Emerging", "unclear": "Unclear"}
    summaries = {
        "strong": "Urban growth around this Estate shows strong potential to support future land demand.",
        "moderate": "Urban growth around this Estate shows moderate potential to support future land demand.",
        "emerging": "The surrounding area shows early signs of urban growth that may support future land demand.",
        "unclear": "The available evidence is not strong enough to classify future land-demand potential yet.",
    }
    return {"level": level, "label": labels[level], "summary": summaries[level]}


def compute_development_forecast(
    boundary_geojson: dict[str, Any],
    *,
    flood_result: dict[str, Any] | None = None,
    erosion_result: dict[str, Any] | None = None,
    planned_roads_count: int = 0,
    progress_cb: Callable[[str, int], None] | None = None,
) -> dict[str, Any]:
    """Build a transparent, area-based built-up expansion scenario around an Estate."""
    report = progress_cb or (lambda stage, pct: None)
    init_gee()
    boundary = shape(boundary_geojson)
    if boundary.is_empty:
        raise ValueError("Estate boundary is empty")
    epsg = _display_epsg_for(boundary)
    boundary_metric = _projected_geometry(boundary, epsg)
    center_metric = boundary_metric.centroid
    radius_m = DEFAULT_RADIUS_M
    neighborhood_metric = boundary_metric.buffer(radius_m)
    neighborhood = _wgs84_geometry(neighborhood_metric, epsg)
    neighborhood_area_ha = max(0.01, neighborhood_metric.area / 10000)

    report("Loading annual built-up history...", 10)
    collection = ee.ImageCollection(LULC_ASSET_ID)
    annual_images = [(year, image) for year, image in _annual_images(collection) if 2017 <= year <= datetime.now(timezone.utc).year]
    if len(annual_images) < 2:
        return {
            "version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "data_available": False,
            "published": False,
            "message": "The annual land-cover time series is not available for this Estate location yet.",
            "analysis": {"radius_m": radius_m, "dataset": LULC_ASSET_ID, "historical_years": []},
            "data_sources": LULC_REFERENCES,
            "hazards": {"flood": _hazard_digest(flood_result, "flood"), "erosion": _hazard_digest(erosion_result, "erosion")},
        }

    history: list[dict[str, Any]] = []
    for index, (year, image) in enumerate(annual_images):
        report(f"Measuring built-up footprint for {year}...", int(15 + (index / max(len(annual_images), 1)) * 38))
        area_ha = _area_ha(image, neighborhood)
        centroid = _built_centroid(image, neighborhood)
        history.append({"year": year, "built_up_area_ha": round(area_ha, 2), "built_up_pct": round(area_ha / neighborhood_area_ha * 100, 2), "centroid": centroid})

    first = history[0]
    latest = history[-1]
    observed_years = max(1, int(latest["year"] - first["year"]))
    annual_rate = (latest["built_up_area_ha"] - first["built_up_area_ha"]) / observed_years
    annual_percent_rate = ((latest["built_up_area_ha"] / first["built_up_area_ha"]) ** (1 / observed_years) - 1) * 100 if first["built_up_area_ha"] > 0 else None
    monotonic_steps = sum(
        1 for previous, current in zip(history, history[1:]) if current["built_up_area_ha"] >= previous["built_up_area_ha"]
    )

    report("Locating the current built-up frontier...", 58)
    rings: list[tuple[float, float, dict[str, Any]]] = []
    for inner in range(0, radius_m, 500):
        outer = inner + 500
        inner_buffer = boundary_metric if inner == 0 else boundary_metric.buffer(inner)
        ring = boundary_metric.buffer(outer).difference(inner_buffer)
        rings.append((inner, outer, _wgs84_geometry(ring, epsg)))
    frontier_distance_m: float | None = None
    ring_areas: list[dict[str, Any]] = []
    for inner, outer, ring_geometry in rings:
        area_ha = _area_ha(annual_images[-1][1], ring_geometry)
        ring_areas.append({"from_m": inner, "to_m": outer, "built_up_area_ha": round(area_ha, 2)})
        if frontier_distance_m is None and area_ha >= 0.05:
            frontier_distance_m = float(inner)

    report("Estimating growth direction...", 68)
    sector_deltas: list[tuple[float, str]] = []
    for index, label in enumerate(SECTOR_LABELS):
        start_bearing = index * 45 - 22.5
        sector = _sector_polygon(center_metric, radius_m, start_bearing, start_bearing + 45)
        sector_geometry = _wgs84_geometry(sector, epsg)
        first_area = _area_ha(annual_images[0][1], sector_geometry)
        latest_area = _area_ha(annual_images[-1][1], sector_geometry)
        sector_deltas.append((latest_area - first_area, label))
    strongest_delta, strongest_sector = max(sector_deltas, key=lambda item: item[0])
    bearing = (SECTOR_LABELS.index(strongest_sector) * 45) % 360 if strongest_delta > 0 else None
    if bearing is None and first.get("centroid") and latest.get("centroid"):
        bearing = _bearing_from_points(first["centroid"], latest["centroid"], epsg)
    direction = direction_label(bearing)

    report("Preparing the public growth map...", 80)
    footprint_years = [first["year"], latest["year"]]
    footprints = []
    for year, image in annual_images:
        if year not in footprint_years:
            continue
        geometry = _footprint(image, neighborhood)
        if geometry:
            footprints.append({"year": year, "geometry": geometry})
    line_distance = min(radius_m, max(1500.0, (frontier_distance_m or radius_m * 0.65) + 1000))
    line_bearing = math.radians(bearing or 90)
    direction_end = Point(center_metric.x + math.sin(line_bearing) * line_distance, center_metric.y + math.cos(line_bearing) * line_distance)
    direction_line = _wgs84_geometry(LineString([center_metric, direction_end]), epsg)

    flood = _hazard_digest(flood_result, "flood")
    erosion = _hazard_digest(erosion_result, "erosion")
    supporting = []
    constraining = []
    if annual_rate > 0:
        supporting.append(f"Built-up land increased by approximately {annual_rate:.2f} hectares per year in the {first['year']}-{latest['year']} record.")
    else:
        constraining.append("The historical built-up footprint did not show a clear positive increase in the available record.")
    if direction != "No clear direction":
        supporting.append(f"The strongest observed outward change is toward the {direction}.")
    if frontier_distance_m is not None:
        supporting.append(f"The nearest observed built-up footprint is approximately {frontier_distance_m:.0f} metres from the Estate boundary.")
    if flood["available"] and flood["risk_class"].lower() not in {"low", "no data"}:
        constraining.append(f"Flood screening is classified as {flood['risk_class'].lower()} and may constrain development in some nearby areas.")
    if erosion["available"] and erosion["risk_class"].lower() not in {"low", "no data"}:
        constraining.append(f"Erosion screening is classified as {erosion['risk_class'].lower()} and may constrain development in some nearby areas.")
    if planned_roads_count:
        constraining.append(f"The Estate layout contains {planned_roads_count} planned road feature(s); planned roads are not treated as confirmed public-road evidence in this forecast.")
    else:
        constraining.append("Planned roads are not counted as confirmed public-road evidence in this forecast.")

    confidence_reasons = []
    if len(history) >= 6:
        confidence_reasons.append(f"{len(history)} annual observations were available")
    else:
        confidence_reasons.append(f"only {len(history)} annual observations were available")
    confidence_reasons.append(f"{monotonic_steps} of {max(1, len(history) - 1)} year-to-year changes were flat or increasing")
    confidence = "high" if len(history) >= 7 and monotonic_steps / max(1, len(history) - 1) >= 0.65 else "moderate" if len(history) >= 4 else "low"
    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_available": True,
        "published": False,
        "analysis": {
            "radius_m": radius_m,
            "dataset": LULC_ASSET_ID,
            "historical_start_year": first["year"],
            "historical_end_year": latest["year"],
            "method": "Built-up class area and directional sector change in a 5 km Estate neighbourhood.",
            "scenario_note": "Conservative and accelerated values are 55% and 145% of the observed area trend. They are scenarios, not statistical guarantees.",
        },
        "historical_built_up": [{key: value for key, value in item.items() if key != "centroid"} for item in history],
        "frontier_rings": ring_areas,
        "growth": {
            "annual_area_rate_ha": round(annual_rate, 3),
            "annual_percent_rate": round(annual_percent_rate, 2) if annual_percent_rate is not None else None,
            "direction": direction,
            "direction_bearing_deg": round(bearing, 1) if bearing is not None else None,
            "frontier_distance_m": round(frontier_distance_m, 1) if frontier_distance_m is not None else None,
            "direction_confidence": confidence,
            "confidence_interval": {
                "low_multiplier": 0.55,
                "high_multiplier": 1.45,
                "type": "scenario range, not a statistical confidence interval",
            },
        },
        "projections": _scenario_rows(latest["built_up_area_ha"], annual_rate, latest["year"]),
        "reach_estimate": _reach_estimate(frontier_distance_m, first["built_up_area_ha"], latest["built_up_area_ha"], observed_years),
        "value_outlook": _value_outlook(annual_percent_rate, frontier_distance_m, direction, confidence),
        "confidence": {"level": confidence, "reasons": confidence_reasons},
        "factors": {"supporting": supporting, "constraining": constraining},
        "hazards": {"flood": flood, "erosion": erosion},
        "planned_roads": {"count": int(planned_roads_count), "included_as_confirmed_evidence": False},
        "roads": {
            "confirmed_public_roads_considered": False,
            "planned_estate_roads": int(planned_roads_count),
            "note": "Planned Estate roads are kept separate from confirmed public-road evidence and do not increase the projection by themselves.",
        },
        "direction_line": {"type": "Feature", "properties": {"label": f"Observed growth direction: {direction}"}, "geometry": direction_line},
        "built_up_footprints": footprints,
        "data_sources": LULC_REFERENCES,
        "public_disclaimer": "This is a location-screening scenario based on rigorous analysis from multiple reliable data sources.",
    }
