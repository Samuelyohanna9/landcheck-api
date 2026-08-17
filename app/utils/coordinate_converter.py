# app/utils/coordinate_converter.py
# Coordinate conversion utility for Nigerian survey systems

import math
from typing import List, Tuple

from pyproj import Transformer, CRS

# Define common coordinate systems used in Nigeria
COORDINATE_SYSTEMS = {
    "wgs84": {
        "name": "WGS84 (Lat/Lon)",
        "epsg": 4326,
        "description": "Global GPS coordinates (Latitude, Longitude)"
    },
    "wgs84_nigeria_meters": {
        "name": "WGS84 Nigeria Metres",
        "epsg": 32632,
        "description": "Auto-select WGS84 / UTM zone 31N, 32N, or 33N for Nigeria"
    },
    "utm_31n": {
        "name": "UTM Zone 31N",
        "epsg": 32631,
        "description": "Western Nigeria (Easting, Northing)"
    },
    "utm_32n": {
        "name": "UTM Zone 32N",
        "epsg": 32632,
        "description": "Central Nigeria (Easting, Northing)"
    },
    "utm_33n": {
        "name": "UTM Zone 33N",
        "epsg": 32633,
        "description": "Eastern Nigeria (Easting, Northing)"
    },
    "minna_31": {
        "name": "Minna Datum Zone 31",
        "epsg": 26331,
        "description": "Nigerian National Grid - West (Clarke 1880)"
    },
    "minna_32": {
        "name": "Minna Datum Zone 32",
        "epsg": 26332,
        "description": "Nigerian National Grid - Central (Clarke 1880)"
    },
    "minna_33": {
        "name": "Minna Datum Zone 33",
        "epsg": 26333,
        "description": "Nigerian National Grid - East (Clarke 1880)"
    },
    "ghana_utm_30n": {
        "name": "Ghana UTM Zone 30N",
        "epsg": 32630,
        "description": "Modern GPS-compatible grid covering most of Ghana (Easting, Northing)"
    },
    "ghana_leigon_grid": {
        "name": "Ghana Leigon National Grid",
        "epsg": 25000,
        "description": "Ghana's national cadastral grid since 1978 (Clarke 1880 RGS)"
    },
    "uganda_utm_35n": {
        "name": "Uganda UTM Zone 35N",
        "epsg": 32635,
        "description": "Modern GPS-compatible grid, western Uganda - west of 30E (Easting, Northing)"
    },
    "uganda_utm_36n": {
        "name": "Uganda UTM Zone 36N",
        "epsg": 32636,
        "description": "Modern GPS-compatible grid, eastern Uganda - east of 30E (Easting, Northing)"
    },
    "uganda_arc1960_35n": {
        "name": "Uganda Arc 1960 Zone 35N",
        "epsg": 21095,
        "description": "Local pre-GPS datum, western Uganda - west of 30E (Clarke 1880 RGS)"
    },
    "uganda_arc1960_36n": {
        "name": "Uganda Arc 1960 Zone 36N",
        "epsg": 21096,
        "description": "Local pre-GPS datum, eastern Uganda - east of 30E (Clarke 1880 RGS)"
    },
    "uganda_utm_35s": {
        "name": "Uganda UTM Zone 35S",
        "epsg": 32735,
        "description": "Modern GPS-compatible grid, SW Uganda - south of equator, west of 30E (Easting, Northing)"
    },
    "uganda_utm_36s": {
        "name": "Uganda UTM Zone 36S",
        "epsg": 32736,
        "description": "Modern GPS-compatible grid, southern Uganda - south of equator, east of 30E (Easting, Northing)"
    },
    "uganda_arc1960_35s": {
        "name": "Uganda Arc 1960 Zone 35S",
        "epsg": 21035,
        "description": "Local pre-GPS datum, SW Uganda - south of equator, west of 30E (Clarke 1880 RGS)"
    },
    "uganda_arc1960_36s": {
        "name": "Uganda Arc 1960 Zone 36S",
        "epsg": 21036,
        "description": "Local pre-GPS datum, southern Uganda - south of equator, east of 30E (Clarke 1880 RGS)"
    }
}

WGS84_NIGERIA_METERS = "wgs84_nigeria_meters"
NIGERIA_UTM_ZONES = ("utm_31n", "utm_32n", "utm_33n")


def is_nigeria_auto_utm_coordinate_system(system: str) -> bool:
    return str(system or "").strip().lower() == WGS84_NIGERIA_METERS


def _looks_like_projected_coordinates(x: float, y: float) -> bool:
    return abs(float(x)) > 180 or abs(float(y)) > 90


def resolve_nigeria_wgs84_meters_zone(lng: float) -> str:
    if not math.isfinite(float(lng)):
        return "utm_32n"
    if lng < 6:
        return "utm_31n"
    if lng < 12:
        return "utm_32n"
    return "utm_33n"


def infer_nigeria_wgs84_meters_zone_from_projected(x: float, y: float) -> str:
    if not math.isfinite(float(x)) or not math.isfinite(float(y)):
        return "utm_32n"

    candidates: list[tuple[str, float, float]] = []
    for zone in NIGERIA_UTM_ZONES:
        epsg = int(COORDINATE_SYSTEMS[zone]["epsg"])
        try:
            transformer = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
            lng, lat = transformer.transform(float(x), float(y))
        except Exception:
            continue
        if validate_nigeria_bounds(float(lng), float(lat)):
            candidates.append((zone, float(lng), float(lat)))

    if len(candidates) == 1:
        return candidates[0][0]

    if len(candidates) > 1:
        for zone, lng, _lat in candidates:
            if resolve_nigeria_wgs84_meters_zone(lng) == zone:
                return zone
        return candidates[0][0]

    return "utm_32n"


def resolve_coordinate_system_key(system: str, x: float | None = None, y: float | None = None) -> str:
    clean = str(system or "wgs84").strip().lower()
    if clean == WGS84_NIGERIA_METERS:
        if x is not None and y is not None and math.isfinite(float(x)) and math.isfinite(float(y)) and _looks_like_projected_coordinates(float(x), float(y)):
            return infer_nigeria_wgs84_meters_zone_from_projected(float(x), float(y))
        if x is not None and math.isfinite(float(x)):
            return resolve_nigeria_wgs84_meters_zone(float(x))
        return "utm_32n"
    return clean


def get_transformer(source_crs: str, target_crs: str = "wgs84") -> Transformer:
    """
    Get a pyproj Transformer for converting between coordinate systems.
    """
    resolved_source_crs = resolve_coordinate_system_key(source_crs)
    resolved_target_crs = resolve_coordinate_system_key(target_crs)
    source_epsg = COORDINATE_SYSTEMS.get(resolved_source_crs, {}).get("epsg", 4326)
    target_epsg = COORDINATE_SYSTEMS.get(resolved_target_crs, {}).get("epsg", 4326)

    return Transformer.from_crs(
        f"EPSG:{source_epsg}",
        f"EPSG:{target_epsg}",
        always_xy=True
    )


def convert_coordinates(
    coords: List[List[float]],
    source_crs: str,
    target_crs: str = "wgs84"
) -> List[List[float]]:
    """
    Convert a list of coordinates from one CRS to another.

    Args:
        coords: List of [x, y] or [easting, northing] or [lon, lat] pairs
        source_crs: Source coordinate system key (wgs84, utm_31n, utm_32n, minna_31, minna_32)
        target_crs: Target coordinate system key (default: wgs84)

    Returns:
        List of converted [lon, lat] coordinates in target CRS
    """
    if not coords:
        return coords

    sample_x = float(coords[0][0])
    sample_y = float(coords[0][1])
    resolved_source_crs = resolve_coordinate_system_key(source_crs, sample_x, sample_y)
    resolved_target_crs = (
        resolve_coordinate_system_key(target_crs, sample_x, sample_y)
        if resolved_source_crs == "wgs84"
        else resolve_coordinate_system_key(target_crs)
    )

    if resolved_source_crs == resolved_target_crs:
        return coords

    transformer = get_transformer(resolved_source_crs, resolved_target_crs)

    converted = []
    for coord in coords:
        x, y = coord[0], coord[1]
        new_x, new_y = transformer.transform(x, y)
        converted.append([new_x, new_y])

    return converted


def convert_single_coordinate(
    x: float,
    y: float,
    source_crs: str,
    target_crs: str = "wgs84"
) -> Tuple[float, float]:
    """
    Convert a single coordinate pair.

    Args:
        x: X coordinate (longitude or easting)
        y: Y coordinate (latitude or northing)
        source_crs: Source coordinate system key
        target_crs: Target coordinate system key

    Returns:
        Tuple of (x, y) in target CRS
    """
    resolved_source_crs = resolve_coordinate_system_key(source_crs, x, y)
    resolved_target_crs = (
        resolve_coordinate_system_key(target_crs, x, y)
        if resolved_source_crs == "wgs84"
        else resolve_coordinate_system_key(target_crs)
    )

    if resolved_source_crs == resolved_target_crs:
        return (x, y)

    transformer = get_transformer(resolved_source_crs, resolved_target_crs)
    return transformer.transform(x, y)


def detect_coordinate_system(coords: List[List[float]]) -> str:
    """
    Attempt to auto-detect coordinate system based on value ranges.
    This is a heuristic and may not always be accurate.

    Args:
        coords: List of coordinate pairs

    Returns:
        Detected coordinate system key
    """
    if not coords:
        return "wgs84"

    # Sample the first few coordinates
    sample = coords[:min(3, len(coords))]

    avg_x = sum(c[0] for c in sample) / len(sample)
    avg_y = sum(c[1] for c in sample) / len(sample)

    # WGS84 Lat/Lon ranges for Nigeria: Lon 2-15, Lat 4-14
    if 2 <= avg_x <= 15 and 4 <= avg_y <= 14:
        return "wgs84"

    # UTM Easting typically 166,000 - 834,000, Northing 0 - 10,000,000
    if 100000 <= avg_x <= 900000 and 400000 <= avg_y <= 1600000:
        # Nigerian Northing range is roughly 450,000 to 1,550,000
        # Determine zone based on easting
        if avg_x < 500000:
            return "utm_31n"
        else:
            return "utm_32n"

    # Minna datum has similar ranges to UTM
    # Would need additional context to differentiate

    return "wgs84"  # Default fallback


def get_coordinate_systems_list() -> List[dict]:
    """
    Get list of available coordinate systems for frontend dropdown.
    """
    return [
        {
            "key": key,
            "name": info["name"],
            "epsg": info["epsg"],
            "description": info["description"]
        }
        for key, info in COORDINATE_SYSTEMS.items()
    ]


# Validation helpers
def validate_wgs84(lon: float, lat: float) -> bool:
    """Validate WGS84 coordinates are within valid ranges."""
    return -180 <= lon <= 180 and -90 <= lat <= 90


def validate_utm(easting: float, northing: float) -> bool:
    """Validate UTM coordinates are within reasonable ranges."""
    return 100000 <= easting <= 900000 and 0 <= northing <= 10000000


def validate_nigeria_bounds(lon: float, lat: float) -> bool:
    """Check if WGS84 coordinates are within Nigeria's approximate bounds."""
    return 2.5 <= lon <= 14.7 and 4.0 <= lat <= 14.0
