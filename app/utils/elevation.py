from __future__ import annotations

from typing import Any, Dict, List, Optional

import ee

from app.utils.gee_client import init_gee

# Global, free 30m DEMs available in the Earth Engine catalog, tried in order. Copernicus GLO-30
# is the newest/most consistent global mosaic; NASADEM fills in anywhere it has gaps.
_DEM_SOURCES = [
    lambda: ee.ImageCollection("COPERNICUS/DEM/GLO30_2024_1").select("DEM").mosaic(),
    lambda: ee.Image("NASA/NASADEM_HGT/001").select("elevation"),
]


def fetch_dem_elevation_points(
    boundary_geojson: Dict[str, Any],
    buffer_m: float = 80,
    scale_m: int = 30,
) -> Optional[List[Dict[str, float]]]:
    """Samples a small point cloud of {lng, lat, elevation_m} around a plot boundary from a free
    global DEM. Returns None if Earth Engine isn't configured or no elevation data is available -
    callers should treat that as "fall back to a flat/no-contour render", not raise.
    """
    try:
        init_gee()
    except Exception:
        return None

    try:
        geom = ee.Geometry(boundary_geojson)
        region = geom.buffer(buffer_m)
    except Exception:
        return None

    for build_dem in _DEM_SOURCES:
        try:
            dem = build_dem()
            sampled = dem.rename("elevation").addBands(ee.Image.pixelLonLat())
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
            elevations = info.get("elevation") or []
            if len(lons) < 3 or len(lons) != len(lats) or len(lons) != len(elevations):
                continue
            points = [
                {"lng": float(lon), "lat": float(lat), "elevation_m": float(elev)}
                for lon, lat, elev in zip(lons, lats, elevations)
                if elev is not None
            ]
            if len(points) >= 3:
                return points
        except Exception:
            continue

    return None
