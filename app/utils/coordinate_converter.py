# app/utils/coordinate_converter.py
# Coordinate conversion utility for Nigerian survey systems

from pyproj import Transformer, CRS
from typing import List, Tuple

# Define common coordinate systems used in Nigeria
COORDINATE_SYSTEMS = {
    "wgs84": {
        "name": "WGS84 (Lat/Lon)",
        "epsg": 4326,
        "description": "Global GPS coordinates (Latitude, Longitude)"
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
    }
}


def get_transformer(source_crs: str, target_crs: str = "wgs84") -> Transformer:
    """
    Get a pyproj Transformer for converting between coordinate systems.
    """
    source_epsg = COORDINATE_SYSTEMS.get(source_crs, {}).get("epsg", 4326)
    target_epsg = COORDINATE_SYSTEMS.get(target_crs, {}).get("epsg", 4326)

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
    if source_crs == target_crs:
        return coords

    transformer = get_transformer(source_crs, target_crs)

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
    if source_crs == target_crs:
        return (x, y)

    transformer = get_transformer(source_crs, target_crs)
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
