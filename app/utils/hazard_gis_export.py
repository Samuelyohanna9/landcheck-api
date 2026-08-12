from __future__ import annotations

import io
import os
import tempfile
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import geopandas as gpd
from shapely.geometry import shape


def _boundary_gdf(boundary_geojson: Dict[str, Any], risk_class: str, risk_score: float) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"risk_class": [risk_class], "risk_score_pct": [risk_score]},
        geometry=[shape(boundary_geojson)],
        crs="EPSG:4326",
    )


def _value_points_gdf(value_points, value_key: str) -> gpd.GeoDataFrame:
    if not value_points:
        return gpd.GeoDataFrame({value_key: []}, geometry=[], crs="EPSG:4326")
    return gpd.GeoDataFrame(
        {value_key: [p.get(value_key) for p in value_points]},
        geometry=gpd.points_from_xy([p["lng"] for p in value_points], [p["lat"] for p in value_points]),
        crs="EPSG:4326",
    )


def build_hazard_gis_export_zip(
    *,
    hazard_type: str,
    boundary_geojson: Dict[str, Any],
    buildings_gdf: Optional[gpd.GeoDataFrame],
    value_points,
    value_key: str,
    risk_class: str,
    risk_score: float,
    method_text: str,
) -> bytes:
    """Bundles the exact vector data a hazard map was rendered from into a ZIP: individual
    GeoJSON layers (universal, human-readable) plus a single multi-layer GeoPackage (the modern
    single-file GIS format QGIS/ArcGIS both read natively) - so a client can pull this straight
    into their own GIS software for further visualization, overlay with other datasets, or
    independent analysis, rather than being limited to the static map image.
    """
    boundary_gdf = _boundary_gdf(boundary_geojson, risk_class, risk_score)
    points_gdf = _value_points_gdf(value_points, value_key)
    has_buildings = buildings_gdf is not None and len(buildings_gdf) > 0

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("boundary.geojson", boundary_gdf.to_json())
        zf.writestr(f"{value_key}_points.geojson", points_gdf.to_json())
        if has_buildings:
            zf.writestr("buildings.geojson", buildings_gdf.to_json())

        with tempfile.TemporaryDirectory() as tmpdir:
            gpkg_path = os.path.join(tmpdir, "hazard_data.gpkg")
            try:
                boundary_gdf.to_file(gpkg_path, layer="boundary", driver="GPKG")
                if len(points_gdf):
                    points_gdf.to_file(gpkg_path, layer=f"{value_key}_points", driver="GPKG")
                if has_buildings:
                    buildings_gdf.to_file(gpkg_path, layer="buildings", driver="GPKG")
                with open(gpkg_path, "rb") as f:
                    zf.writestr("hazard_data.gpkg", f.read())
            except Exception:
                # GeoJSON layers above already cover every GIS package worth using - the
                # GeoPackage is a bonus convenience, not a hard requirement for this export.
                pass

        generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        readme = (
            f"LandCheck {hazard_type.title()} Hazard - GIS Data Export\n"
            f"Generated: {generated}\n"
            f"Risk class: {risk_class} ({risk_score}%)\n\n"
            f"{method_text}\n\n"
            "Files in this package:\n"
            "  boundary.geojson         - the analyzed plot boundary, with risk class/score as attributes\n"
            f"  {value_key}_points.geojson  - the sample points the hazard surface was built from\n"
            + ("  buildings.geojson        - building footprints, with a `threatened` true/false attribute\n" if has_buildings else "")
            + "  hazard_data.gpkg         - all of the above combined as layers in one GeoPackage\n\n"
            "Coordinate reference system: EPSG:4326 (WGS84) throughout.\n"
            "Screening-level data for further analysis - verify independently before relying on it.\n"
        )
        zf.writestr("README.txt", readme)

    buf.seek(0)
    return buf.getvalue()
