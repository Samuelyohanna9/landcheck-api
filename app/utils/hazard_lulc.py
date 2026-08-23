from __future__ import annotations

import base64
import io
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

import ee
import geopandas as gpd
import numpy as np
from shapely.geometry import shape
from sqlalchemy.orm import Session

from app.utils.gee_client import init_gee
from app.utils.hazard_map_renderer import LULC_CLASS_COLORS, _display_epsg_for, render_lulc_hazard_map

logger = logging.getLogger(__name__)

# Esri's 10m Global Land Use Land Cover from Sentinel-2 (Impact Observatory / National Geographic
# Society), hosted on Earth Engine's community catalog. Chosen specifically because its class
# scheme/colors are the ones the user's reference poster uses - ESA WorldCover and Google Dynamic
# World both use different class lists (no "Rangeland", different wetland/grass splits) and would
# not visually match. Verified live against the catalog (gee-community-catalog.org) rather than
# assumed - community datasets do occasionally get renamed/rehosted, so if this ever 404s, check
# that catalog page for the current asset ID first.
#
# The single-year (2020) asset is PRIMARY, not the multi-year "_TS" time-series collection - a real
# test against the time series produced an implausible result (~93% "Snow/Ice" and an out-of-scheme
# "Class 11" for a savanna plot in Adamawa, Nigeria), most likely from mosaicking across years whose
# per-tile coverage/encoding doesn't line up cleanly. The single well-established 2020 classification
# doesn't have that multi-year-composite risk. The time series is kept as a fallback in case the
# single-year asset is ever unavailable, but a genuinely wrong-but-non-empty result from it must not
# be silently trusted either - see the valid-class-code filter in compute_lulc_summary below.
LULC_ASSET_ID = "projects/sat-io/open-datasets/landcover/ESRI_Global-LULC_10m"
LULC_ASSET_ID_FALLBACK = "projects/sat-io/open-datasets/landcover/ESRI_Global-LULC_10m_TS"

LULC_REFERENCES = [
    {
        "short": "Karra et al. (2021)",
        "citation": (
            "Karra, K., Kontgis, C., Statman-Weil, Z., Mazzariello, J.C., Mathis, M., Brumby, S.P. "
            "(2021). Global land use/land cover with Sentinel-2 and deep learning. IEEE International "
            "Geoscience and Remote Sensing Symposium (IGARSS), 4704-4707."
        ),
        "url": "https://doi.org/10.1109/IGARSS47720.2021.9553499",
    },
    {
        "short": "Copernicus GLO-30 DEM",
        "citation": (
            "European Space Agency, Sinergise (2021). Copernicus Global Digital Elevation Model. "
            "Distributed by OpenTopography."
        ),
        "url": "https://doi.org/10.5069/G9028PQB",
    },
]

# Pixel budget for a single ee.Image.sampleRectangle() request/response - keeps a dense-grid fetch
# for an unusually large boundary from producing an oversized getInfo() payload. _fetch_ee_grid
# steps its scale coarser (never finer) until a request would fit within this.
_MAX_GRID_PIXELS = 500 * 500


def _is_known_class_code(class_id_str: str) -> bool:
    try:
        return int(float(class_id_str)) in LULC_CLASS_COLORS
    except (TypeError, ValueError):
        return False


def _load_landcover_source_image(asset_id: str) -> "ee.Image":
    """Loads a landcover asset as a single classification image, regardless of whether it's
    published as an ImageCollection (a set of tiles needing .mosaic() to merge into one continuous
    image - the common pattern for global community-hosted datasets) or a plain single ee.Image.
    Not verifiable against the live catalog from this environment, so this tries the collection
    form first (the more likely one for a global-coverage dataset) and falls back to a bare Image.
    ee calls are lazy - neither branch actually validates anything server-side until the caller
    evaluates it (e.g. via reduceRegion().getInfo()), so this only reflects client-side type errors,
    not "does this asset exist" - the real validation happens in compute_lulc_summary's caller loop.
    """
    try:
        return ee.ImageCollection(asset_id).mosaic().rename("landcover")
    except Exception:
        return ee.Image(asset_id).rename("landcover")


def _fetch_ee_grid(
    image_single_band: "ee.Image",
    band_name: str,
    minx: float, miny: float, maxx: float, maxy: float,
    display_epsg: int,
    base_scale_m: float,
    default_value: float = 0,
) -> Tuple[Optional[np.ndarray], float]:
    """Samples a single-band ee.Image as a dense 2D array covering EXACTLY [minx,miny,maxx,maxy] in
    EPSG:{display_epsg} metres, at (up to) base_scale_m resolution - returns (grid, scale_m_used).

    The image is reprojected into that same metric CRS first and the sampling region is requested
    as an explicit rectangle IN that CRS (ee.Geometry.Rectangle(..., proj=..., geodesic=False)), so
    the returned pixel grid's bounds are known exactly - no approximation between what Earth Engine
    samples and what matplotlib later draws at extent=(minx,maxx,miny,maxy).

    ee.Image.sampleRectangle() returns the pixel grid as nested-list metadata on the returned image
    (read via .get(band_name).getInfo()), not a normal raster file - no rasterio/GDAL needed. Returns
    (None, scale_m) if the fetch fails for any reason (dataset unavailable, no coverage, etc.) -
    callers must treat a hillshade/overlay grid as optional, same as every other satellite fetch in
    this app's hazard modules.
    """
    proj = f"EPSG:{display_epsg}"
    scale_m = float(base_scale_m)
    for _attempt in range(4):
        width_px = max(2, int(round((maxx - minx) / scale_m)))
        height_px = max(2, int(round((maxy - miny) / scale_m)))
        if width_px * height_px <= _MAX_GRID_PIXELS:
            break
        scale_m *= 1.8
    else:
        return None, scale_m

    try:
        region = ee.Geometry.Rectangle([minx, miny, maxx, maxy], proj=proj, geodesic=False)
        reprojected = image_single_band.reproject(crs=proj, scale=scale_m)
        rect = reprojected.sampleRectangle(region=region, defaultValue=default_value)
        raw = rect.get(band_name).getInfo()
        arr = np.array(raw, dtype=float)
        if arr.size == 0:
            return None, scale_m
        return arr, scale_m
    except Exception:
        return None, scale_m


def compute_lulc_summary(
    db: Session,
    boundary_geojson: Dict[str, Any],
    buffer_m: float = 500,
    progress_cb: Optional[Callable[[str, int], None]] = None,
) -> Tuple[Dict[str, Any], bytes]:
    """Land Use / Land Cover summary for a plot: % of area in each Esri landcover class, rendered as
    a 3D hillshade-relief map with the classification overlaid. Purely informational - unlike
    compute_flood_risk/compute_erosion_risk this returns no risk score/tier, since land cover isn't
    itself a hazard. Returns (breakdown, overlay_png).

    `db` is accepted (unused) for signature symmetry with compute_flood_risk/compute_erosion_risk -
    every hazard compute function in this router is called the same way, and LULC may reasonably
    need a Postgres lookup of its own later (e.g. buildings-in-built-area context).
    """
    report = progress_cb or (lambda stage, pct: None)
    report("Connecting to Earth Engine...", 5)
    init_gee()

    geom = ee.Geometry(boundary_geojson)

    # Class-area % is computed over the plot boundary itself (not the wider render buffer) - "how
    # much of MY land is water/trees/crops" is the natural question, the buffer below is only for
    # the map's visual field of view, matching how flood/erosion already separate "what's scored"
    # from "what's shown for context".
    report("Loading land cover classification...", 20)
    hist_info: Dict[str, int] = {}
    landcover_image: Optional["ee.Image"] = None
    for asset_id in (LULC_ASSET_ID, LULC_ASSET_ID_FALLBACK):
        try:
            candidate_image = _load_landcover_source_image(asset_id)
            hist = candidate_image.reduceRegion(
                reducer=ee.Reducer.frequencyHistogram(),
                geometry=geom, scale=10, maxPixels=int(1e9), bestEffort=True,
            ).get("landcover")
            raw_hist = hist.getInfo() or {}
        except Exception:
            logger.warning("Land cover histogram fetch failed for asset %s", asset_id, exc_info=True)
            continue

        # Keep only Esri's actual known class codes (1-9). A real test against the "_TS" asset
        # returned a supposed "Class 11", which doesn't exist in Esri's published scheme at all -
        # that's a masking/encoding artifact from mosaicking across years, not a legitimate 10th
        # land-cover type, and must never be counted as real area or shown as a fabricated legend
        # entry. If most of the pixels this asset returned aren't valid codes, the whole result is
        # untrustworthy (not just the invalid pixels) - move on to the next asset instead.
        raw_pixels = sum(int(v) for v in raw_hist.values())
        valid_hist = {k: v for k, v in raw_hist.items() if _is_known_class_code(k)}
        valid_pixels = sum(int(v) for v in valid_hist.values())
        if raw_pixels and valid_pixels / raw_pixels < 0.5:
            logger.warning(
                "Land cover asset %s returned mostly out-of-scheme class codes (%d/%d pixels valid) - discarding",
                asset_id, valid_pixels, raw_pixels,
            )
            continue
        if valid_hist:
            hist_info = valid_hist
            landcover_image = candidate_image
            break
        logger.warning("Land cover asset %s returned zero classified pixels for this boundary", asset_id)

    try:
        total_area_ha = float(geom.area(1).getInfo()) / 10000.0
    except Exception:
        total_area_ha = 0.0

    total_pixels = sum(int(v) for v in hist_info.values()) if hist_info else 0
    data_available = total_pixels > 0

    class_areas: List[Dict[str, Any]] = []
    for class_id_str, count in sorted(hist_info.items(), key=lambda kv: -kv[1]):
        try:
            class_id = int(float(class_id_str))
        except (TypeError, ValueError):
            continue
        label, color = LULC_CLASS_COLORS.get(class_id, (f"Class {class_id}", "#9ca3af"))
        pct = (count / total_pixels * 100.0) if total_pixels else 0.0
        class_areas.append({
            "class_id": class_id,
            "label": label,
            "color": color,
            "pct": round(pct, 2),
            "area_ha": round(total_area_ha * (count / total_pixels), 3) if total_pixels else 0.0,
        })

    dominant_class = class_areas[0]["label"] if class_areas else None
    dominant_pct = class_areas[0]["pct"] if class_areas else None
    legend = [{"label": c["label"], "color": c["color"]} for c in class_areas]

    # Dense grids for the map's hillshade terrain background + categorical overlay - sampled over
    # the buffered render extent (the map's visual field of view), in the same projected metric CRS
    # the renderer uses for the boundary/scalebar/north-arrow so everything lines up. render_lulc_
    # hazard_map() independently recomputes this exact same extent from boundary_geojson/buffer_m
    # (same pattern render_flood_hazard_map/render_erosion_hazard_map already use) - since
    # _display_epsg_for + the buffer-bounds calculation are pure functions of those two inputs, the
    # two computations are guaranteed identical without needing to pass the extent through
    # explicitly, so each grid's pixel data lines up with what the renderer will draw it against.
    report("Fetching elevation and imagery grids...", 55)
    boundary_geom = shape(boundary_geojson)
    display_epsg = _display_epsg_for(boundary_geom)
    gdf_boundary = gpd.GeoDataFrame(geometry=[boundary_geom], crs="EPSG:4326").to_crs(epsg=display_epsg)
    boundary_proj = gdf_boundary.geometry.iloc[0]
    minx, miny, maxx, maxy = boundary_proj.buffer(buffer_m).bounds

    dem_image = ee.ImageCollection("COPERNICUS/DEM/GLO30_2024_1").select("DEM").mosaic().rename("elevation")
    elevation_grid, elevation_scale_m = _fetch_ee_grid(
        dem_image, "elevation", minx, miny, maxx, maxy, display_epsg, base_scale_m=30.0,
    )
    landcover_grid: Optional[np.ndarray] = None
    landcover_scale_m = 10.0
    if landcover_image is not None:
        landcover_grid, landcover_scale_m = _fetch_ee_grid(
            landcover_image, "landcover", minx, miny, maxx, maxy, display_epsg, base_scale_m=10.0, default_value=0,
        )

    breakdown: Dict[str, Any] = {
        "class_areas": class_areas,
        "class_count": len(class_areas),
        "dominant_class": dominant_class,
        "dominant_pct": dominant_pct,
        "total_area_ha": round(total_area_ha, 3),
        "data_available": data_available,
        "legend": legend,
        "buffer_m": buffer_m,
        "_references": LULC_REFERENCES,
    }

    report("Rendering land cover map...", 80)
    png_bytes, map_stats = render_lulc_hazard_map(
        boundary_geojson=boundary_geojson,
        landcover_grid=landcover_grid,
        landcover_scale_m=landcover_scale_m,
        elevation_grid=elevation_grid,
        elevation_scale_m=elevation_scale_m,
        class_areas=class_areas,
        buffer_m=buffer_m,
    )
    breakdown["_interactive"] = map_stats.get("interactive")

    report("Finalizing report...", 95)
    return breakdown, png_bytes


def overlay_to_data_url(png_bytes: bytes) -> str:
    encoded = base64.b64encode(png_bytes).decode("utf-8")
    return f"data:image/png;base64,{encoded}"


def overlay_to_bytes_io(png_bytes: bytes) -> io.BytesIO:
    return io.BytesIO(png_bytes)
