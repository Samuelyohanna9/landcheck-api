from __future__ import annotations

"""Generate reviewable Estate concept layouts from an approved boundary.

The generator is intentionally deterministic and conservative. It produces a metric, orthogonal
concept plan with access corridors and public/open-space reserves; it is not a substitute for a
licensed planner, surveyor, engineering design or planning-authority approval.
"""

import math
from typing import Any

from pyproj import Geod, Transformer
from shapely.geometry import LineString, Polygon, box, mapping
from shapely.ops import transform as shapely_transform, unary_union

from app.routers.plots import _metric_epsg_for_wgs84_polygon
from app.schemas.estates import EstateLayoutCriteria


def _json_geometry(geometry: Any) -> dict[str, Any]:
    return {"type": geometry.geom_type, "coordinates": _coordinates_to_json(mapping(geometry)["coordinates"])}


def _coordinates_to_json(value: Any) -> Any:
    if isinstance(value, (tuple, list)):
        return [_coordinates_to_json(item) for item in value]
    return value


def _largest_polygon(geometry: Any) -> Polygon | None:
    if geometry is None or geometry.is_empty:
        return None
    if geometry.geom_type == "Polygon":
        return geometry
    if geometry.geom_type == "MultiPolygon":
        return max(geometry.geoms, key=lambda item: item.area, default=None)
    return None


def _clean_polygon(geometry: Any) -> Polygon | None:
    candidate = _largest_polygon(geometry)
    if candidate is None:
        return None
    if not candidate.is_valid:
        candidate = _largest_polygon(candidate.buffer(0))
    return candidate if candidate is not None and not candidate.is_empty and candidate.is_valid else None


def _safe_grid_count(length_m: float, ideal_m: float, road_width_m: float) -> int:
    if length_m <= 0 or ideal_m <= 0:
        return 1
    count = max(1, round(length_m / max(ideal_m + road_width_m, 1.0)))
    while count > 1 and (length_m - (count - 1) * road_width_m) / count < ideal_m * 0.55:
        count -= 1
    return count


def _line_parts(geometry: Any) -> list[Any]:
    if geometry.is_empty:
        return []
    if geometry.geom_type == "LineString":
        return [geometry]
    if geometry.geom_type == "MultiLineString":
        return list(geometry.geoms)
    if geometry.geom_type == "GeometryCollection":
        return [item for item in geometry.geoms if item.geom_type in {"LineString", "MultiLineString"}]
    return []


def generate_estate_layout(boundary_wgs84: Polygon, criteria: EstateLayoutCriteria) -> dict[str, Any]:
    """Return generated plot and infrastructure candidates in WGS84 GeoJSON form."""

    if boundary_wgs84.geom_type != "Polygon" or boundary_wgs84.is_empty or not boundary_wgs84.is_valid:
        raise ValueError("Estate boundary must be a valid Polygon")

    metric_epsg = _metric_epsg_for_wgs84_polygon(boundary_wgs84)
    forward = Transformer.from_crs("EPSG:4326", f"EPSG:{metric_epsg}", always_xy=True).transform
    backward = Transformer.from_crs(f"EPSG:{metric_epsg}", "EPSG:4326", always_xy=True).transform
    metric_boundary = _clean_polygon(shapely_transform(forward, boundary_wgs84))
    if metric_boundary is None:
        raise ValueError("Estate boundary could not be converted to a planning CRS")

    outer_reserve_m = max(float(criteria.edge_reserve_m), 0.0)
    planning_area = _clean_polygon(metric_boundary.buffer(-outer_reserve_m)) if outer_reserve_m else metric_boundary
    if planning_area is None or planning_area.area <= criteria.target_plot_area_sqm:
        raise ValueError("Estate boundary is too small for the selected planning assumptions")

    min_dimension = min(planning_area.bounds[2] - planning_area.bounds[0], planning_area.bounds[3] - planning_area.bounds[1])
    if min_dimension < criteria.road_width_m * 2:
        raise ValueError("Estate boundary is too narrow for the selected road width")

    rotated = shapely_transform(
        lambda x, y, z=None: (
            x * math.cos(math.radians(criteria.orientation_deg)) + y * math.sin(math.radians(criteria.orientation_deg)),
            -x * math.sin(math.radians(criteria.orientation_deg)) + y * math.cos(math.radians(criteria.orientation_deg)),
        ),
        planning_area,
    )
    min_x, min_y, max_x, max_y = rotated.bounds
    width = max_x - min_x
    height = max_y - min_y
    ideal_width = float(criteria.frontage_m or math.sqrt(criteria.target_plot_area_sqm * 1.25))
    ideal_height = float(criteria.target_plot_area_sqm / ideal_width)
    columns = _safe_grid_count(width, ideal_width, criteria.road_width_m)
    rows = _safe_grid_count(height, ideal_height, criteria.road_width_m)
    available_width = width - max(0, columns - 1) * criteria.road_width_m
    available_height = height - max(0, rows - 1) * criteria.road_width_m
    cell_width = available_width / columns
    cell_height = available_height / rows
    if cell_width <= 0 or cell_height <= 0:
        raise ValueError("The selected road width leaves no room for plots")

    cells: list[tuple[int, int, Polygon]] = []
    for row_index in range(rows):
        y0 = min_y + row_index * (cell_height + criteria.road_width_m)
        for column_index in range(columns):
            x0 = min_x + column_index * (cell_width + criteria.road_width_m)
            clipped = _clean_polygon(box(x0, y0, x0 + cell_width, y0 + cell_height).intersection(rotated))
            if clipped is not None and clipped.area >= max(criteria.target_plot_area_sqm * 0.35, 25):
                cells.append((row_index, column_index, clipped))

    if not cells:
        raise ValueError("No usable plots fit inside the boundary with these assumptions")

    open_space_cells: set[tuple[int, int]] = set()
    if criteria.include_open_space and criteria.open_space_percent > 0:
        center_x = (columns - 1) / 2
        center_y = (rows - 1) / 2
        ordered = sorted(cells, key=lambda item: (item[1] - center_x) ** 2 + (item[0] - center_y) ** 2)
        target_area = planning_area.area * criteria.open_space_percent / 100
        reserved_area = 0.0
        for row_index, column_index, cell in ordered[: max(0, len(ordered) - 1)]:
            open_space_cells.add((row_index, column_index))
            reserved_area += cell.area
            if reserved_area >= target_area:
                break

    plot_candidates: list[dict[str, Any]] = []
    open_space_geometries: list[Polygon] = []
    for row_index, column_index, cell in cells:
        if (row_index, column_index) in open_space_cells:
            open_space_geometries.append(cell)
            continue
        oriented = shapely_transform(
            lambda x, y, z=None: (
                x * math.cos(math.radians(criteria.orientation_deg)) - y * math.sin(math.radians(criteria.orientation_deg)),
                x * math.sin(math.radians(criteria.orientation_deg)) + y * math.cos(math.radians(criteria.orientation_deg)),
            ),
            cell,
        )
        plot_wgs84 = _clean_polygon(shapely_transform(backward, oriented))
        if plot_wgs84 is None:
            continue
        geod = Geod(ellps="WGS84")
        area_sqm, _ = geod.geometry_area_perimeter(plot_wgs84)
        plot_candidates.append(
            {
                "plot_number": f"{criteria.plot_prefix}-{len(plot_candidates) + 1:03d}",
                "block_label": chr(65 + (row_index % 26)),
                "geometry": _json_geometry(plot_wgs84),
                "area_sqm": round(abs(float(area_sqm)), 2),
                "valid": True,
                "issues": [],
            }
        )
        if len(plot_candidates) >= criteria.max_plots:
            break

    feature_candidates: list[dict[str, Any]] = []
    if criteria.include_roads:
        road_lines: list[Any] = []
        for column_index in range(1, columns):
            x = min_x + column_index * cell_width + (column_index - 0.5) * criteria.road_width_m
            road_lines.extend(_line_parts(rotated.intersection(LineString([(x, min_y), (x, max_y)]))))
        for row_index in range(1, rows):
            y = min_y + row_index * cell_height + (row_index - 0.5) * criteria.road_width_m
            road_lines.extend(_line_parts(rotated.intersection(LineString([(min_x, y), (max_x, y)]))))
        for index, road in enumerate(road_lines, 1):
            oriented = shapely_transform(
                lambda x, y, z=None: (
                    x * math.cos(math.radians(criteria.orientation_deg)) - y * math.sin(math.radians(criteria.orientation_deg)),
                    x * math.sin(math.radians(criteria.orientation_deg)) + y * math.cos(math.radians(criteria.orientation_deg)),
                ),
                road,
            )
            if not oriented.is_empty and oriented.length > 1:
                feature_candidates.append({"feature_type": "road", "name": f"Road {index}", "geometry": _json_geometry(shapely_transform(backward, oriented))})

    if open_space_geometries:
        open_space = unary_union(open_space_geometries)
        open_space_wgs84 = shapely_transform(backward, shapely_transform(
            lambda x, y, z=None: (
                x * math.cos(math.radians(criteria.orientation_deg)) - y * math.sin(math.radians(criteria.orientation_deg)),
                x * math.sin(math.radians(criteria.orientation_deg)) + y * math.cos(math.radians(criteria.orientation_deg)),
            ),
            open_space,
        ))
        feature_candidates.append({"feature_type": "open_space", "name": "Public open space reserve", "geometry": _json_geometry(open_space_wgs84)})

    if criteria.include_drainage and criteria.drainage_reserve_m > 0:
        drainage_inner = metric_boundary.buffer(-max(outer_reserve_m, criteria.drainage_reserve_m))
        drainage = metric_boundary.difference(drainage_inner) if not drainage_inner.is_empty else metric_boundary
        if not drainage.is_empty:
            feature_candidates.append({"feature_type": "drainage", "name": "Drainage reserve", "geometry": _json_geometry(shapely_transform(backward, drainage))})

    total_plot_area = sum(float(candidate["area_sqm"]) for candidate in plot_candidates)
    open_space_area = sum(cell.area for cell in open_space_geometries)
    diagnostics = {
        "planning_crs": f"EPSG:{metric_epsg}",
        "estate_area_sqm": round(float(metric_boundary.area), 2),
        "target_plot_area_sqm": float(criteria.target_plot_area_sqm),
        "estimated_plot_count": len(plot_candidates),
        "total_plot_area_sqm": round(total_plot_area, 2),
        "estate_planning_area_sqm": round(float(planning_area.area), 2),
        "plot_coverage_percent": round((total_plot_area / planning_area.area) * 100, 2),
        "open_space_area_sqm": round(float(open_space_area), 2),
        "open_space_percent": round((open_space_area / planning_area.area) * 100, 2),
        "road_count": sum(1 for item in feature_candidates if item["feature_type"] == "road"),
        "road_width_m": float(criteria.road_width_m),
        "edge_reserve_m": float(criteria.edge_reserve_m),
        "warnings": [
            "Concept layout only: a qualified planner/surveyor must verify dimensions, access, drainage, services and authority requirements.",
            "Road and open-space values are planning assumptions supplied for this proposal, not statutory approvals.",
        ],
    }
    return {
        "criteria": criteria.model_dump(),
        "diagnostics": diagnostics,
        "plot_candidates": plot_candidates,
        "feature_candidates": feature_candidates,
    }
