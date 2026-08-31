from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

# Deterministic, no-AI quality-control pass over a plot's own boundary coordinates - the surveyor's
# "✨ AI CHECK" button. Same discipline as plan_reader.py's check_survey_plan: closure/area/
# duplicate/outlier math must be exact and reproducible, never a second source of hallucination.
# This module deliberately never calls an AI model - it's marketed as "AI-assisted QC" the same way
# a spell-checker is "smart," not because an LLM wrote any of these numbers.

_PROJECTED_SYSTEMS = {"minna_31", "minna_32", "minna_33", "utm_31n", "utm_32n", "utm_33n"}
# Same loose Nigeria-wide bounding boxes as plan_reader.py's out-of-range check - not a precise
# zone check, just enough to catch an obvious mistyped/dropped digit.
_NIGERIA_PROJECTED_EASTING_RANGE = (60000.0, 940000.0)
_NIGERIA_PROJECTED_NORTHING_RANGE = (350000.0, 1600000.0)
_NIGERIA_LATLON_RANGE = {"lng": (1.0, 16.0), "lat": (2.0, 15.0)}

# Two boundary stations this close together are almost certainly the same point entered twice, not
# two legitimately adjacent corners.
DUPLICATE_COORDINATE_TOLERANCE_M = 0.05
# A segment more than this many times the median segment length is worth a second look.
LONG_SEGMENT_OUTLIER_MULTIPLE = 3.0
# 1:500 - generous for a hand-entered plan (real cadastral adjustment often targets tighter), since
# this is a screening flag for a surveyor to double check, not a certification/adjustment tolerance.
CLOSURE_TOLERANCE_FRACTION = 0.002
AREA_MISMATCH_TOLERANCE_FRACTION = 0.08


def _shoelace_area_and_perimeter(points: List[Dict[str, float]]) -> Dict[str, float]:
    n = len(points)
    if n < 3:
        return {"area_m2": 0.0, "perimeter_m": 0.0}
    area = 0.0
    perimeter = 0.0
    for i in range(n):
        x1, y1 = points[i]["x"], points[i]["y"]
        x2, y2 = points[(i + 1) % n]["x"], points[(i + 1) % n]["y"]
        area += x1 * y2 - x2 * y1
        perimeter += math.hypot(x2 - x1, y2 - y1)
    return {"area_m2": abs(area) / 2.0, "perimeter_m": perimeter}


def check_plot_survey_quality(
    points: List[Dict[str, Any]],
    coordinate_system: str,
    stated_area_m2: Optional[float] = None,
) -> Dict[str, Any]:
    """Runs the full deterministic checklist against a plot's boundary points (already-saved
    survey_input_coordinates, boundary-flagged points only - see plots.py's
    _normalize_survey_input_coordinates/_build_exact_measurement_polygon for the same filtering).
    stated_area_m2 is an optional, not-persisted comparison figure the surveyor can type in just
    for this one check (mirrors the "Plan area entered" line in the target UI).
    """
    items: List[Dict[str, str]] = []
    system = str(coordinate_system or "unknown").strip().lower()
    is_projected = system in _PROJECTED_SYSTEMS
    station_count = len(points)

    if station_count < 3:
        return {
            "items": [{
                "severity": "error", "code": "too_few_stations",
                "message": f"Only {station_count} boundary station(s) entered - a closed boundary needs at least 3.",
            }],
            "station_count": station_count, "area_m2": None, "perimeter_m": None,
            "closure_error_m": None, "closure_ratio": None, "review_count": 1, "overall_status": "review",
        }
    items.append({"severity": "ok", "code": "station_count", "message": f"{station_count} boundary station(s) detected."})

    duplicate_found = False
    for i in range(station_count):
        for j in range(i + 1, station_count):
            dist = math.hypot(points[i]["x"] - points[j]["x"], points[i]["y"] - points[j]["y"])
            if dist <= DUPLICATE_COORDINATE_TOLERANCE_M:
                duplicate_found = True
                items.append({
                    "severity": "warning", "code": "duplicate_coordinate",
                    "message": f'Stations "{points[i]["station"]}" and "{points[j]["station"]}" are only {dist:.2f}m apart - likely the same point entered twice.',
                })
    if not duplicate_found:
        items.append({"severity": "ok", "code": "no_duplicates", "message": "No duplicate coordinates detected."})

    range_ok = True
    if is_projected:
        ex_lo, ex_hi = _NIGERIA_PROJECTED_EASTING_RANGE
        ny_lo, ny_hi = _NIGERIA_PROJECTED_NORTHING_RANGE
        for p in points:
            if not (ex_lo <= p["x"] <= ex_hi and ny_lo <= p["y"] <= ny_hi):
                range_ok = False
                items.append({
                    "severity": "warning", "code": "coordinate_out_of_range",
                    "message": f'Station "{p["station"]}" ({p["x"]:.2f}, {p["y"]:.2f}) looks outside the plausible range for {system} - check for a mistyped digit.',
                })
    elif system == "wgs84":
        lng_lo, lng_hi = _NIGERIA_LATLON_RANGE["lng"]
        lat_lo, lat_hi = _NIGERIA_LATLON_RANGE["lat"]
        for p in points:
            if not (lng_lo <= p["x"] <= lng_hi and lat_lo <= p["y"] <= lat_hi):
                range_ok = False
                items.append({
                    "severity": "warning", "code": "coordinate_out_of_range",
                    "message": f'Station "{p["station"]}" ({p["x"]:.6f}, {p["y"]:.6f}) looks outside Nigeria for WGS84 - check for a mistyped digit or swapped lat/lng.',
                })
    if range_ok:
        items.append({"severity": "ok", "code": "format_consistent", "message": f"Coordinate format consistent for {system}."})

    geometry = _shoelace_area_and_perimeter(points)
    area_m2 = geometry["area_m2"]
    perimeter_m = geometry["perimeter_m"]

    closure_dx = 0.0
    closure_dy = 0.0
    for i in range(station_count):
        p1, p2 = points[i], points[(i + 1) % station_count]
        closure_dx += p2["x"] - p1["x"]
        closure_dy += p2["y"] - p1["y"]
    closure_error_m = math.hypot(closure_dx, closure_dy)
    closure_ratio_text: Optional[str] = None
    if perimeter_m > 0:
        closure_fraction = closure_error_m / perimeter_m
        closure_ratio_text = f"1:{int(round(1 / closure_fraction)):,}" if closure_fraction > 0 else "1:∞ (exact)"
        if closure_fraction > CLOSURE_TOLERANCE_FRACTION:
            items.append({
                "severity": "warning", "code": "closure_error",
                "message": f"Boundary closure error is {closure_error_m:.3f}m over a {perimeter_m:.1f}m perimeter ({closure_ratio_text}) - review station order and coordinates.",
            })
        else:
            items.append({"severity": "ok", "code": "boundary_closes", "message": f"Boundary closes within tolerance ({closure_ratio_text})."})

    items.append({"severity": "ok", "code": "area_computed", "message": f"Calculated area: {area_m2:,.2f} sqm."})

    edge_distances: List[tuple[str, str, float]] = []
    for i in range(station_count):
        p1, p2 = points[i], points[(i + 1) % station_count]
        edge_distances.append((str(p1["station"]), str(p2["station"]), math.hypot(p2["x"] - p1["x"], p2["y"] - p1["y"])))
    if len(edge_distances) >= 4:
        sorted_d = sorted(d for _, _, d in edge_distances)
        median_d = sorted_d[len(sorted_d) // 2] or 1.0
        outlier_floor = max(median_d * LONG_SEGMENT_OUTLIER_MULTIPLE, 20.0)
        for from_st, to_st, d in edge_distances:
            if d > outlier_floor:
                items.append({
                    "severity": "warning", "code": "long_segment",
                    "message": f'Segment "{from_st}" to "{to_st}" is {d:.1f}m - much longer than the {median_d:.1f}m median segment. Review recommended.',
                })

    if isinstance(stated_area_m2, (int, float)) and stated_area_m2 > 0 and area_m2 > 0:
        diff_fraction = abs(area_m2 - stated_area_m2) / stated_area_m2
        if diff_fraction > AREA_MISMATCH_TOLERANCE_FRACTION:
            items.append({
                "severity": "warning", "code": "area_mismatch",
                "message": f"Plan area entered: {stated_area_m2:,.1f} sqm vs calculated {area_m2:,.1f} sqm - {diff_fraction * 100:.1f}% difference.",
            })
        else:
            items.append({
                "severity": "ok", "code": "area_matches",
                "message": f"Entered area ({stated_area_m2:,.1f} sqm) matches calculated area within tolerance.",
            })

    if system == "unknown" or not system:
        items.append({"severity": "warning", "code": "unknown_coordinate_system", "message": "Coordinate system is not set for this plot."})
    else:
        items.append({"severity": "ok", "code": "crs_consistent", "message": f"CRS/project settings consistent ({system})."})

    review_count = sum(1 for item in items if item["severity"] in ("warning", "error"))
    return {
        "items": items,
        "station_count": station_count,
        "area_m2": round(area_m2, 2),
        "perimeter_m": round(perimeter_m, 2),
        "closure_error_m": round(closure_error_m, 3),
        "closure_ratio": closure_ratio_text,
        "review_count": review_count,
        "overall_status": "ok" if review_count == 0 else "review",
    }
