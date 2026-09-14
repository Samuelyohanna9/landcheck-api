from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pyproj import Geod
from shapely.geometry import shape
from shapely.validation import explain_validity


@dataclass(frozen=True, slots=True)
class QcIssue:
    severity: str
    code: str
    message: str


def validate_polygon(geojson: dict[str, Any], *, minimum_area_sqm: float = 1.0) -> tuple[float, list[QcIssue]]:
    try:
        polygon = shape(geojson)
    except Exception:
        return 0.0, [QcIssue("error", "invalid_geometry", "Geometry is not valid GeoJSON")]
    if polygon.geom_type != "Polygon" or polygon.is_empty:
        return 0.0, [QcIssue("error", "polygon_required", "A non-empty Polygon is required")]
    if not polygon.is_valid:
        return 0.0, [QcIssue("error", "invalid_geometry", explain_validity(polygon))]
    geod = Geod(ellps="WGS84")
    area_sqm, _ = geod.geometry_area_perimeter(polygon)
    area_sqm = abs(float(area_sqm))
    issues: list[QcIssue] = []
    if area_sqm < minimum_area_sqm:
        issues.append(QcIssue("warning", "small_area", f"Plot area {area_sqm:.2f} sqm is below the review threshold"))
    return area_sqm, issues
