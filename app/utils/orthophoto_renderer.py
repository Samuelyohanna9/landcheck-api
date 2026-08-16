# app/utils/orthophoto_renderer.py

import matplotlib
matplotlib.use("Agg")

import os
import math
import re
from io import BytesIO
import numpy as np
import geopandas as gpd
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import matplotlib.lines as mlines
import matplotlib.tri as mtri
import matplotlib.patheffects as patheffects
import requests
from sqlalchemy import text
from shapely import wkb
from datetime import datetime
import contextily as ctx
from PIL import Image

from app.utils.elevation import fetch_dem_elevation_points
from app.utils.hazard_common import fetch_buildings_near
# NOTE: map_renderer_layout is imported lazily inside _draw_topo_features, not at module level -
# it imports FROM this module (_try_add_arcgis_world_imagery etc.), so a top-level import here
# would be circular.

from reportlab.lib.pagesizes import A4, A3, A2, A1, A0
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas

# (connect timeout, read timeout) in seconds for every basemap tile fetch. Without this, a
# stalled (not cleanly failed) connection to the tile provider has no time budget of its own and
# can hang well past the point where a retry/fallback would actually help - this matters most for
# the full-export path below, which chains up to 3 providers sequentially.
_BASEMAP_FETCH_TIMEOUT = (5, 20)
_ARCGIS_EXPORT_TIMEOUT = (6, 30)
_ARCGIS_WORLD_IMAGERY_EXPORT_URL = os.getenv(
    "ARCGIS_WORLD_IMAGERY_EXPORT_URL",
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/export",
).strip()
_ARCGIS_ORTHO_PREVIEW_MAX_EDGE = max(1024, int(os.getenv("ARCGIS_ORTHO_PREVIEW_MAX_EDGE", "1600")))
_ARCGIS_ORTHO_EXPORT_MAX_EDGE = max(1600, int(os.getenv("ARCGIS_ORTHO_EXPORT_MAX_EDGE", "4096")))
_ORTHOPHOTO_SOURCE_GSD_METERS = max(0.1, float(os.getenv("ORTHOPHOTO_SOURCE_GSD_METERS", "0.4")))
_ORTHOPHOTO_MAX_UPSCALE_FACTOR = max(1.0, float(os.getenv("ORTHOPHOTO_MAX_UPSCALE_FACTOR", "2.0")))

# Same public Mapbox token already used by the frontend map (mapboxLoader.ts / VITE_MAPBOX_TOKEN) -
# Mapbox public (pk.*) tokens are meant to be shared between client and server use, so the Hetzner
# deployment just needs this env var set to that same value.
MAPBOX_ACCESS_TOKEN = os.getenv("MAPBOX_ACCESS_TOKEN", "").strip()


def _mapbox_satellite_url() -> str:
    # @2x = retina tiles (512px instead of 256px per tile) - sharper render at the same zoom level
    # than Esri's default 256px tiles, on top of Mapbox's imagery having denser high-res (30-50cm)
    # coverage in Nigeria's larger cities than Esri's default World Imagery layer.
    return (
        "https://api.mapbox.com/v4/mapbox.satellite/{z}/{x}/{y}@2x.jpg90"
        f"?access_token={MAPBOX_ACCESS_TOKEN}"
    )


def _compute_arcgis_export_size(
    fig_width: float,
    fig_height: float,
    map_width_frac: float,
    map_height_frac: float,
    dpi: int,
    preview_mode: bool,
) -> tuple[int, int]:
    max_edge = _ARCGIS_ORTHO_PREVIEW_MAX_EDGE if preview_mode else _ARCGIS_ORTHO_EXPORT_MAX_EDGE
    base_width = max(640, int(round(fig_width * map_width_frac * dpi)))
    base_height = max(640, int(round(fig_height * map_height_frac * dpi)))
    upscale_factor = 1.0 if preview_mode else 1.5
    width = max(640, int(round(base_width * upscale_factor)))
    height = max(640, int(round(base_height * upscale_factor)))
    scale = min(max_edge / max(width, 1), max_edge / max(height, 1), 1.0)
    return max(640, int(round(width * scale))), max(640, int(round(height * scale)))


def _load_arcgis_export_image(
    *,
    xmin: float,
    ymin: float,
    xmax: float,
    ymax: float,
    axis_epsg: int,
    pixel_width: int,
    pixel_height: int,
    preview_mode: bool,
    timeout: tuple[float, float] | None = None,
) -> np.ndarray:
    params = {
        "bbox": f"{xmin},{ymin},{xmax},{ymax}",
        "bboxSR": axis_epsg,
        "imageSR": axis_epsg,
        "size": f"{pixel_width},{pixel_height}",
        "format": "jpg" if preview_mode else "png32",
        "transparent": "false",
        "f": "image",
    }
    response = requests.get(
        _ARCGIS_WORLD_IMAGERY_EXPORT_URL,
        params=params,
        timeout=timeout or _ARCGIS_EXPORT_TIMEOUT,
        headers={"User-Agent": "LandCheck-Orthophoto/1.0"},
    )
    response.raise_for_status()
    content_type = (response.headers.get("Content-Type") or "").lower()
    if "json" in content_type:
        raise RuntimeError(f"ArcGIS exportImage returned JSON instead of imagery: {response.text[:240]}")
    image = Image.open(BytesIO(response.content)).convert("RGB")
    return np.asarray(image)


def _try_add_arcgis_world_imagery(
    ax,
    *,
    target_xlim: tuple[float, float],
    target_ylim: tuple[float, float],
    axis_epsg: int,
    fig_width: float,
    fig_height: float,
    map_width_frac: float,
    map_height_frac: float,
    dpi: int,
    preview_mode: bool,
    timeout: tuple[float, float] | None = None,
) -> bool:
    try:
        pixel_width, pixel_height = _compute_arcgis_export_size(
            fig_width,
            fig_height,
            map_width_frac,
            map_height_frac,
            dpi,
            preview_mode,
        )
        image = _load_arcgis_export_image(
            xmin=target_xlim[0],
            ymin=target_ylim[0],
            xmax=target_xlim[1],
            ymax=target_ylim[1],
            axis_epsg=axis_epsg,
            pixel_width=pixel_width,
            pixel_height=pixel_height,
            preview_mode=preview_mode,
            timeout=timeout,
        )
        ax.imshow(
            image,
            extent=(target_xlim[0], target_xlim[1], target_ylim[0], target_ylim[1]),
            origin="upper",
            interpolation="none",
            zorder=0,
        )
        return True
    except Exception as e:
        print(f"ArcGIS World Imagery exportImage failed: {e}")
        return False


def _choose_imagery_scale_ratio(
    requested_scale_ratio: int,
    *,
    fig_width: float,
    fig_height: float,
    map_width_frac: float,
    map_height_frac: float,
    dpi: int,
) -> tuple[int, bool]:
    paper_ground_width = fig_width * map_width_frac * 0.0254
    paper_ground_height = fig_height * map_height_frac * 0.0254
    map_pixel_width = max(1.0, fig_width * map_width_frac * dpi)
    map_pixel_height = max(1.0, fig_height * map_height_frac * dpi)
    min_ground_width = (map_pixel_width * _ORTHOPHOTO_SOURCE_GSD_METERS) / _ORTHOPHOTO_MAX_UPSCALE_FACTOR
    min_ground_height = (map_pixel_height * _ORTHOPHOTO_SOURCE_GSD_METERS) / _ORTHOPHOTO_MAX_UPSCALE_FACTOR
    min_scale_width = int(math.ceil(min_ground_width / max(paper_ground_width, 1e-6)))
    min_scale_height = int(math.ceil(min_ground_height / max(paper_ground_height, 1e-6)))
    recommended_scale = max(requested_scale_ratio, min_scale_width, min_scale_height)
    return recommended_scale, recommended_scale != requested_scale_ratio


_NICE_CONTOUR_STEPS = (0.02, 0.05, 0.1, 0.2, 0.25, 0.5, 1, 2, 2.5, 5, 10, 20, 25, 50, 100)


def _nice_contour_step(elevation_span_m: float, target_bands: int = 10) -> float:
    # Picks a round contour interval that lands close to `target_bands` bands across whatever
    # relief is actually in the sampled area, rather than a fixed span->step lookup table. The old
    # table bottomed out at a 0.25m step, so a low-relief dataset (a handful of nearby survey
    # corners spanning under a metre - which is exactly what "Your Data" elevation points usually
    # are, see _draw_topo_contours) could round down to just 2-3 levels total: one interior
    # contour line and two flat filled blocks, not a real-looking topo map. Deriving the step from
    # a target band count instead means even a gentle, low-relief site still gets subdivided into
    # several bands (a uniformly sloped surface genuinely does show several evenly-spaced parallel
    # contours on a real topo map at a fine-enough interval), while a steep site still gets a
    # coarse interval instead of an unreadable tangle of lines.
    if elevation_span_m <= 0:
        return _NICE_CONTOUR_STEPS[0]
    raw_step = elevation_span_m / max(1, target_bands)
    return min(_NICE_CONTOUR_STEPS, key=lambda s: abs(s - raw_step))


def _draw_topo_contours(
    ax,
    plot_geom_wgs84,
    display_epsg: int,
    target_xlim: tuple,
    target_ylim: tuple,
    elevation_points,
    font_scale: float = 1.0,
    is_user_data: bool = False,
    contour_interval_override: float | None = None,
) -> bool:
    """Draws real contour lines derived from sampled elevation points - either the surveyor's own
    uploaded heights (is_user_data=True) or a free global DEM fetched for the plot's footprint.
    Returns False (leaving the axes untouched) when there isn't enough usable elevation data,
    so the caller can fall back to the "elevation data unavailable" message.
    """
    points = elevation_points if elevation_points and len(elevation_points) >= 3 else None
    if points is None:
        try:
            boundary_geojson = plot_geom_wgs84.__geo_interface__
        except Exception:
            boundary_geojson = None
        points = fetch_dem_elevation_points(boundary_geojson) if boundary_geojson else None
        is_user_data = False
    if not points or len(points) < 3:
        return False

    try:
        points_gdf = gpd.GeoDataFrame(
            {"elevation_m": [float(p["elevation_m"]) for p in points]},
            geometry=gpd.points_from_xy([float(p["lng"]) for p in points], [float(p["lat"]) for p in points]),
            crs="EPSG:4326",
        ).to_crs(epsg=display_epsg)
    except Exception:
        return False

    xs = points_gdf.geometry.x.to_numpy()
    ys = points_gdf.geometry.y.to_numpy()
    elevations = points_gdf["elevation_m"].to_numpy()
    finite_mask = np.isfinite(xs) & np.isfinite(ys) & np.isfinite(elevations)
    xs, ys, elevations = xs[finite_mask], ys[finite_mask], elevations[finite_mask]
    if len(xs) < 3:
        return False

    elev_min, elev_max = float(np.min(elevations)), float(np.max(elevations))
    span = elev_max - elev_min
    if span < 0.05:
        # Dead-flat sampled area - a contour map of it is just noise, not useful terrain info.
        return False

    try:
        triang = mtri.Triangulation(xs, ys)
    except Exception:
        return False

    # A user-picked interval overrides the automatic band-count heuristic. If it's too coarse for
    # this site's actual relief (fewer than 2 levels would result), fall back to Auto rather than
    # silently rendering a contour-less map just because the surveyor picked, say, 10m on a 2m-tall
    # site - the override is a preference, not a guarantee, and Auto always produces a valid map.
    step = contour_interval_override if contour_interval_override and contour_interval_override > 0 else None
    if step is not None:
        levels = np.arange(math.floor(elev_min / step) * step, math.ceil(elev_max / step) * step + step, step)
        if len(levels) < 2:
            step = None
    if step is None:
        step = _nice_contour_step(span)
        levels = np.arange(math.floor(elev_min / step) * step, math.ceil(elev_max / step) * step + step, step)
    if len(levels) < 2:
        return False

    try:
        ax.tricontourf(triang, elevations, levels=levels, cmap="terrain", alpha=0.5, zorder=0)
        # Minor (intermediate) contours: thin, unlabeled - real topo maps only print elevation
        # text on the bold index lines, not every line, or a map with many bands turns into a
        # wall of numbers.
        ax.tricontour(
            triang, elevations, levels=levels, colors="#5b3a1a", linewidths=0.5 * font_scale, zorder=1,
        )
        index_levels = levels[::5] if len(levels) > 10 else levels[::2] if len(levels) > 4 else levels
        if len(index_levels) >= 2:
            index_contours = ax.tricontour(
                triang, elevations, levels=index_levels, colors="#3f2a12", linewidths=1.4 * font_scale, zorder=2,
            )
            labels = ax.clabel(index_contours, inline=True, fontsize=max(5, 6.5 * font_scale), fmt="%g m")
            for label in labels:
                label.set_path_effects([patheffects.withStroke(linewidth=2, foreground="white")])
        if is_user_data:
            ax.scatter(
                xs, ys, s=(10 * font_scale) ** 2, c="#1d4ed8", marker="+", linewidths=1.1 * font_scale, zorder=3,
            )
    except Exception:
        return False

    ax.set_xlim(target_xlim)
    ax.set_ylim(target_ylim)
    return True


def _draw_topo_features(ax, db, plot_id, plot_geom_wgs84, display_epsg, scale_ratio, font_scale=1.0, fig=None) -> bool:
    """Draws roads, rivers, and buildings on top of the topo contour layer - the "natural features
    and man-made objects" half of an actual topographic map, not just colored elevation bands.
    Reuses the same data sources and drawing primitives as the main survey-plan renderer and the
    hazard maps, just with plain topo-map symbology (thin solid roads, blue rivers, hatched
    building outlines) instead of the cadastral plan's double-line/hatch treatment - a different
    visual context. Safe to call even when there's no elevation data at all - features are drawn
    independently of whether contours rendered. Returns True if anything was actually drawn.
    """
    # Imported lazily: map_renderer_layout imports FROM this module at its own top level, so a
    # module-level import here would be circular.
    from app.utils.map_renderer_layout import _fetch_live_road_geoms, _draw_road_edges, draw_building_hatch, draw_key_box

    drew_anything = False
    has_roads = has_rivers = has_buildings = False

    try:
        boundary_geojson = plot_geom_wgs84.__geo_interface__
    except Exception:
        boundary_geojson = None

    # Roads - the same live query the survey-plan templates and Road Names panel already use.
    try:
        road_geoms = _fetch_live_road_geoms(db, plot_id)
    except Exception:
        road_geoms = []
    if road_geoms:
        try:
            projected_roads = gpd.GeoSeries(road_geoms, crs="EPSG:4326").to_crs(epsg=display_epsg)
            _draw_road_edges(
                ax, list(projected_roads), font_scale=font_scale, color="#2b2b2b", linestyle="-",
                scale_ratio=scale_ratio,
            )
            has_roads = True
            drew_anything = True
        except Exception:
            pass

    # Rivers - same detected_features snapshot table buildings/rivers already come from elsewhere;
    # no standalone river-drawing helper exists to reuse, so draw plain lines directly.
    try:
        river_rows = db.execute(
            text("SELECT geom FROM detected_features WHERE plot_id = :plot_id AND feature_type = 'river'"),
            {"plot_id": plot_id},
        ).fetchall()
        river_geoms = [wkb.loads(row[0]) for row in river_rows]
    except Exception:
        river_geoms = []
    if river_geoms:
        try:
            projected_rivers = gpd.GeoSeries(river_geoms, crs="EPSG:4326").to_crs(epsg=display_epsg)
            for geom in projected_rivers:
                lines = geom.geoms if geom.geom_type == "MultiLineString" else [geom]
                for line in lines:
                    if line.geom_type != "LineString" or line.is_empty:
                        continue
                    xs_r, ys_r = line.xy
                    ax.plot(xs_r, ys_r, color="#1f78d1", linewidth=1.3 * font_scale, zorder=5)
            has_rivers = True
            drew_anything = True
        except Exception:
            pass

    # Buildings - the same fetch the Flood/Erosion hazard maps already use.
    if boundary_geojson is not None:
        try:
            building_geoms = fetch_buildings_near(db, boundary_geojson, buffer_m=120)
        except Exception:
            building_geoms = []
        if building_geoms:
            try:
                draw_building_hatch(
                    ax, building_geoms, display_epsg, scale_ratio or 1000, font_scale=font_scale,
                    color="#4a4a4a", hatch_type="diagonal",
                )
                has_buildings = True
                drew_anything = True
            except Exception:
                pass

    if drew_anything and fig is not None:
        try:
            draw_key_box(fig, has_buildings=has_buildings, has_roads=has_roads, has_rivers=has_rivers, font_scale=font_scale)
        except Exception:
            pass

    return drew_anything


# Paper size mapping (ReportLab points)
PAPER_SIZES_REPORTLAB = {
    "A4": A4,
    "A3": A3,
    "A2": A2,
    "A1": A1,
    "A0": A0,
}

# Paper sizes in inches for matplotlib figures
PAPER_SIZES_IN = {
    "A4": (8.27, 11.69),     # 210 x 297 mm
    "A3": (11.69, 16.54),    # 297 x 420 mm
    "A2": (16.54, 23.39),    # 420 x 594 mm
    "A1": (23.39, 33.11),    # 594 x 841 mm
    "A0": (33.11, 46.81),    # 841 x 1189 mm
}

# Font scale factors relative to A4
SCALE_FACTORS = {
    "A4": 1.0,
    "A3": 1.25,
    "A2": 1.5,
    "A1": 1.8,
    "A0": 2.2,
}


def format_station_label(label) -> str:
    raw = str(label or "").strip()
    if not raw:
        return raw
    # Format codes like SCAD5130 -> SCAD\n5130 for clearer station labeling.
    match = re.match(r"^([A-Za-z]{1,4})\s*[-_/]?\s*(\d+)$", raw)
    if match:
        return f"{match.group(1).upper()}\n{match.group(2)}"
    return raw


def get_paper_config(paper_size: str):
    size = paper_size.upper() if paper_size else "A4"
    if size not in PAPER_SIZES_IN:
        size = "A4"
    return {
        "width": PAPER_SIZES_IN[size][0],
        "height": PAPER_SIZES_IN[size][1],
        "scale": SCALE_FACTORS[size],
        "name": size,
    }

# =======================
# Helpers
# =======================

def nice_grid_step(span_m: float) -> float:
    if span_m <= 0:
        return 100.0
    base = 10 ** math.floor(math.log10(span_m))
    steps = np.array([0.02, 0.05, 0.1, 0.2, 0.5, 1.0]) * base
    return float(steps[np.argmin(np.abs(steps - span_m / 6))])


def parse_scale_ratio(scale_text: str) -> int:
    try:
        s = str(scale_text).replace(" ", "")
        if ":" in s:
            return int(s.split(":")[1])
        return int(s)
    except Exception:
        return 1000


def apply_true_scale(ax, geom, scale_ratio, map_w_in, map_h_in):
    minx, miny, maxx, maxy = geom.bounds
    cx = (minx + maxx) / 2
    cy = (miny + maxy) / 2

    paper_w_m = map_w_in * 0.0254
    paper_h_m = map_h_in * 0.0254

    real_w = paper_w_m * scale_ratio
    real_h = paper_h_m * scale_ratio

    ax.set_xlim(cx - real_w / 2, cx + real_w / 2)
    ax.set_ylim(cy - real_h / 2, cy + real_h / 2)


# =======================
# Layout helpers
# =======================

def draw_sheet_frame(fig, font_scale=1.0):
    fig.add_artist(patches.Rectangle((0.02, 0.02), 0.96, 0.96,
                                     transform=fig.transFigure, fill=False, lw=2*font_scale, zorder=10))
    fig.add_artist(patches.Rectangle((0.03, 0.03), 0.94, 0.94,
                                     transform=fig.transFigure, fill=False, lw=0.8*font_scale, zorder=10))


def draw_title_block(fig, title_text, plot_id, scale_text, location, lga, state, font_scale=1.0):
    # Plot number at top right corner for identification
    fig.text(0.94, 0.95, f"Plot #{plot_id}", ha="right", fontsize=int(8*font_scale), weight="bold",)
    
    y = 0.955
    fig.text(0.5, y, title_text, ha="center", fontsize=int(12*font_scale), weight="bold")
    fig.text(0.5, y-0.030, f"LOCATED AT: {location}", ha="center", fontsize=int(9*font_scale))
    fig.text(0.5, y-0.050, lga, ha="center", fontsize=int(9*font_scale))
    fig.text(0.5, y-0.070, state, ha="center", fontsize=int(9*font_scale))
    fig.text(0.5, y-0.100, f"SCALE {scale_text}", ha="center", fontsize=int(9*font_scale))


def draw_footer(fig, crs, source, surveyor, rank, font_scale=1.0):
    y = 0.155
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    fig.text(0.05, y, f"SURVEYOR: {surveyor}", fontsize=int(8*font_scale))
    fig.text(0.05, y-0.018, f"RANK: {rank}", fontsize=int(8*font_scale))
    fig.text(0.05, y-0.036, "SIGNATURE: ____________________", fontsize=int(8*font_scale))
    fig.text(0.05, y-0.054, f"DATE PRINTED: {now}", fontsize=int(8*font_scale))
    fig.text(0.05, 0.05, crs, fontsize=int(7*font_scale), color="blue")
    fig.text(0.95, 0.05, source, fontsize=int(7*font_scale), ha="right")


def add_north_arrow(ax, font_scale=1.0, style: str = "one_side_stem", color: str = "black"):
    fig = ax.figure
    col = "blue" if str(color).lower() == "blue" else "black"
    style = str(style or "one_side_stem").strip().lower()
    box = ax.get_position()
    x = float(box.x1)
    y = min(0.93, float(box.y1) + 0.060)
    size = 0.032 * max(0.8, font_scale)

    if style in ("one_side_stem", "one-sided-stem", "oneside_stem", "stacked_4n", "stacked4n", "vertical_4n"):
        # One-sided "4-shape" head per provided reference code.
        stem_bottom = y - size * 1.20
        stem_top = y + size * 0.80
        head_knee_x = x - size * 0.20
        head_knee_y = y + size * 0.40
        n_y = y - size * 0.25
        line_lw = max(1.0, 1.2 * font_scale)

        # Main shaft
        fig.add_artist(
            mlines.Line2D(
                [x, x],
                [stem_bottom, stem_top],
                transform=fig.transFigure,
                color=col,
                lw=line_lw,
                zorder=20,
                solid_capstyle="butt",
            )
        )
        # Diagonal from top of shaft down-left
        fig.add_artist(
            mlines.Line2D(
                [x, head_knee_x],
                [stem_top, head_knee_y],
                transform=fig.transFigure,
                color=col,
                lw=line_lw,
                zorder=21,
                solid_capstyle="butt",
            )
        )
        # Horizontal back to shaft
        fig.add_artist(
            mlines.Line2D(
                [head_knee_x, x],
                [head_knee_y, head_knee_y],
                transform=fig.transFigure,
                color=col,
                lw=line_lw,
                zorder=21,
                solid_capstyle="butt",
            )
        )
        fig.text(
            x,
            n_y,
            "N",
            ha="center",
            va="center",
            transform=fig.transFigure,
            fontsize=int(12.0 * font_scale),
            color=col,
            weight="normal",
            fontfamily="DejaVu Sans",
            zorder=25,
        )
        return

    if style == "triangle":
        tri = patches.Polygon(
            [(x, y + size), (x - size * 0.6, y - size * 0.6), (x + size * 0.6, y - size * 0.6)],
            closed=True,
            facecolor=col,
            edgecolor=col,
            transform=fig.transFigure,
            zorder=20,
        )
        fig.add_artist(tri)
        fig.text(x, y + size * 1.15, "N", ha="center", va="center",
                 fontsize=int(11 * font_scale), color=col, weight="bold")
        return

    if style == "chevron":
        tri = patches.Polygon(
            [(x, y + size * 1.05), (x - size * 0.55, y - size * 0.55), (x + size * 0.55, y - size * 0.55)],
            closed=True,
            facecolor="white",
            edgecolor=col,
            lw=1.2 * font_scale,
            transform=fig.transFigure,
            zorder=20,
        )
        fig.add_artist(tri)
        fig.add_artist(patches.Polygon(
            [(x, y + size * 0.85), (x - size * 0.35, y - size * 0.35), (x + size * 0.35, y - size * 0.35)],
            closed=True,
            facecolor=col,
            edgecolor=col,
            transform=fig.transFigure,
            zorder=21,
        ))
        fig.text(x, y + size * 1.2, "N", ha="center", va="center",
                 fontsize=int(10 * font_scale), color=col, weight="bold")
        return

    if style == "orienteering":
        circle = patches.Circle(
            (x, y),
            radius=size * 0.95,
            fill=False,
            edgecolor=col,
            lw=1.1 * font_scale,
            transform=fig.transFigure,
            zorder=20,
        )
        fig.add_artist(circle)
        outer = patches.Polygon(
            [(x, y + size), (x - size * 0.35, y - size * 0.2), (x + size * 0.35, y - size * 0.2)],
            closed=True,
            facecolor="white",
            edgecolor=col,
            lw=1.0 * font_scale,
            transform=fig.transFigure,
            zorder=21,
        )
        inner = patches.Polygon(
            [(x, y + size * 0.8), (x - size * 0.2, y - size * 0.15), (x + size * 0.2, y - size * 0.15)],
            closed=True,
            facecolor=col,
            edgecolor=col,
            transform=fig.transFigure,
            zorder=22,
        )
        fig.add_artist(outer)
        fig.add_artist(inner)
        fig.text(x, y + size * 1.2, "N", ha="center", va="center",
                 fontsize=int(10 * font_scale), color=col, weight="bold")
        return

    if style == "star":
        pts = []
        for i in range(8):
            ang = math.radians(i * 45)
            r = size if i % 2 == 0 else size * 0.45
            pts.append((x + r * math.sin(ang), y + r * math.cos(ang)))
        star = patches.Polygon(
            pts,
            closed=True,
            facecolor="white",
            edgecolor=col,
            lw=1.0 * font_scale,
            transform=fig.transFigure,
            zorder=20,
        )
        fig.add_artist(star)
        fig.text(x, y + size * 1.1, "N", ha="center", va="center",
                 fontsize=int(10 * font_scale), color=col, weight="bold")
        return

    if style == "compass":
        circle = patches.Circle(
            (x, y),
            radius=size * 0.9,
            fill=False,
            edgecolor=col,
            lw=1.1 * font_scale,
            transform=fig.transFigure,
            zorder=20,
        )
        fig.add_artist(circle)
        north = patches.Polygon(
            [(x, y + size), (x - size * 0.4, y), (x + size * 0.4, y)],
            closed=True,
            facecolor=col,
            edgecolor=col,
            transform=fig.transFigure,
            zorder=21,
        )
        south = patches.Polygon(
            [(x, y - size), (x - size * 0.35, y), (x + size * 0.35, y)],
            closed=True,
            facecolor="white",
            edgecolor=col,
            transform=fig.transFigure,
            zorder=21,
        )
        fig.add_artist(north)
        fig.add_artist(south)
        fig.text(x, y + size * 1.15, "N", ha="center", va="center",
                 fontsize=int(10 * font_scale), color=col, weight="bold")
        return

    # default: classic arrow
    ax.annotate(
        "N",
        xy=(x, y + size * 0.95),
        xytext=(x, y - size * 0.75),
        xycoords="figure fraction",
        arrowprops=dict(facecolor=col, edgecolor=col, width=2*font_scale, headwidth=8*font_scale),
        ha="center",
        fontsize=int(12*font_scale),
        weight="bold",
        color=col,
        zorder=20,
    )



def choose_scalebar_length(scale_ratio):
    if scale_ratio <= 500: return 50
    if scale_ratio <= 1000: return 100
    if scale_ratio <= 2000: return 200
    return 500


def add_scalebar(ax, length_m, segments=4, font_scale=1.0):
    trans = ax.transAxes
    x0, y0, bar_h = 0.32, -0.12, 0.018
    seg = 0.25 / segments

    for i in range(segments):
        face = "black" if i % 2 == 0 else "white"
        ax.add_patch(patches.Rectangle(
            (x0 + i * seg, y0), seg, bar_h,
            transform=trans, facecolor=face, edgecolor="black", lw=0.8*font_scale, clip_on=False, zorder=15
        ))

    ax.add_patch(patches.Rectangle(
        (x0, y0), 0.25, bar_h,
        transform=trans, fill=False, edgecolor="black", lw=1.2*font_scale, clip_on=False, zorder=16
    ))

    ax.text(x0, y0 - 0.04, "0", transform=trans, ha="center", fontsize=int(8*font_scale))
    for i in range(1, segments + 1):
        ax.text(x0 + i * seg, y0 - 0.04, f"{int(length_m * i / segments)}",
                transform=trans, ha="center", fontsize=int(8*font_scale))
    ax.text(x0 + 0.125, y0 + bar_h + 0.02, "meters", transform=trans, ha="center", fontsize=int(8*font_scale))


def draw_grid(ax, minor, major, font_scale=1.0):
    xmin, xmax = ax.get_xlim()
    ymin, ymax = ax.get_ylim()

    # Tick-only grid to keep map area clean
    tick_len = (xmax - xmin) * 0.01
    xs = np.arange(math.floor(xmin / major) * major, xmax + 0.1, major)
    ys = np.arange(math.floor(ymin / major) * major, ymax + 0.1, major)

    for x in xs:
        if x < xmin or x > xmax:
            continue
        ax.plot([x, x], [ymax, ymax - tick_len], color="blue", lw=0.6*font_scale, alpha=0.5, zorder=3)
        ax.plot([x, x], [ymin, ymin + tick_len], color="blue", lw=0.6*font_scale, alpha=0.5, zorder=3)

    for y in ys:
        if y < ymin or y > ymax:
            continue
        ax.plot([xmin, xmin + tick_len], [y, y], color="blue", lw=0.6*font_scale, alpha=0.5, zorder=3)
        ax.plot([xmax, xmax - tick_len], [y, y], color="blue", lw=0.6*font_scale, alpha=0.5, zorder=3)


def annotate_vertices_orthophoto(ax, poly, station_names=None, font_scale=1.0, scale_ratio: int = 1000):
    """Add station name labels to plot vertices in orthophoto."""
    from shapely.geometry import Point

    coords = list(poly.exterior.coords)
    default_labels = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    labels = station_names if station_names else default_labels

    for i in range(len(coords) - 1):
        p1 = Point(coords[i])
        p2 = Point(coords[i + 1])
        label = format_station_label(labels[i % len(labels)])

        seg_dx = p2.x - p1.x
        seg_dy = p2.y - p1.y
        seg_len = math.hypot(seg_dx, seg_dy) or 1.0
        nx, ny = -seg_dy / seg_len, seg_dx / seg_len
        station_offset = max(2.0, (5.0 / 1000.0) * scale_ratio)
        lx = p1.x + nx * station_offset
        ly = p1.y + ny * station_offset

        ax.text(
            lx,
            ly,
            label,
            fontsize=int(8*font_scale),
            color="black",
            ha="center",
            va="center",
            weight="bold",
            multialignment="center",
            linespacing=0.95,
            zorder=25,
            bbox=dict(facecolor="white", edgecolor="black", boxstyle="round,pad=0.15", alpha=0.85)
        )


def draw_coordinate_frame(ax, spacing, axis_epsg=3857, label_epsg=3857, font_scale=1.0):
    """Draw coordinate frame with labels in the requested coordinate system."""
    from pyproj import Transformer

    xmin, xmax = ax.get_xlim()
    ymin, ymax = ax.get_ylim()
    pad = (xmax - xmin) * 0.035

    ax.add_patch(patches.Rectangle((xmin-pad, ymin-pad), (xmax-xmin)+2*pad, (ymax-ymin)+2*pad,
                                   fill=False, lw=1.5*font_scale, clip_on=False, zorder=10))
    ax.add_patch(patches.Rectangle((xmin, ymin), (xmax-xmin), (ymax-ymin),
                                   fill=False, lw=1.0*font_scale, zorder=10))

    transformer = None
    if axis_epsg != label_epsg:
        transformer = Transformer.from_crs(axis_epsg, label_epsg, always_xy=True)

    xs = np.arange(math.floor(xmin / spacing) * spacing, xmax + 0.1, spacing)
    ys = np.arange(math.floor(ymin / spacing) * spacing, ymax + 0.1, spacing)

    # Draw easting labels at the top
    for x in xs:
        if x >= xmin and x <= xmax:
            if transformer:
                tx, _ = transformer.transform(x, (ymin + ymax) / 2)
                label = f"{int(tx)}"
            else:
                label = f"{int(x)}"
            ax.text(x, ymax + pad*0.45, label, ha="center", fontsize=int(7*font_scale), color="blue", zorder=11)

    # Draw northing labels on both sides
    for y in ys:
        if y >= ymin and y <= ymax:
            if transformer:
                _, ty = transformer.transform((xmin + xmax) / 2, y)
                label = f"{int(ty)}"
            else:
                label = f"{int(y)}"
            ax.text(xmin-pad*0.45, y, label, va="center", ha="right", fontsize=int(7*font_scale), color="blue", rotation=90, zorder=11)
            ax.text(xmax+pad*0.45, y, label, va="center", ha="left", fontsize=int(7*font_scale), color="blue", rotation=90, zorder=11)


# =======================
# Main Renderer
# =======================

def render_orthophoto_png(
    db, plot_id, output_path,
    title_text="ORTHOPHOTO", location_text="", lga_text="", state_text="",
    scale_text="1 : 1000", crs_footer_text="ORIGIN: WGS84 (UTM Projection)",
    source_footer_text="SOURCE: LandCheck System", surveyor_name="", surveyor_rank="",
    tile_source="esri", station_names=None,
    coordinate_system="wgs84", epsg_code=4326,
    use_topo_map=False,
    topo_source="opentopomap",
    elevation_points=None,
    contour_interval=None,
    paper_size="A4",
    north_arrow_style="one_side_stem",
    north_arrow_color="black",
    preview_mode: bool = False,
):
    # Fetch Geometry from DB
    res = db.execute(text("SELECT geom FROM plots WHERE id=:id"), {"id": plot_id}).fetchone()
    if not res: 
        raise ValueError("Plot not found")

    plot_geom = wkb.loads(res[0])

    display_epsg = epsg_code
    if coordinate_system == "wgs84" or epsg_code == 4326:
        centroid = plot_geom.centroid
        utm_zone = int((centroid.x + 180) / 6) + 1
        hemisphere = "north" if centroid.y >= 0 else "south"
        display_epsg = 32600 + utm_zone if hemisphere == "north" else 32700 + utm_zone

    gdf_plot = gpd.GeoDataFrame(geometry=[plot_geom], crs="EPSG:4326").to_crs(epsg=display_epsg)
    poly = gdf_plot.geometry.iloc[0]

    # Canvas setup based on paper size
    paper_config = get_paper_config(paper_size)
    fig_width = paper_config["width"]
    fig_height = paper_config["height"]
    font_scale = paper_config["scale"]
    if preview_mode:
        dpi = 120
    else:
        dpi = 300 if paper_config["name"] in ["A4", "A3"] else 220 if paper_config["name"] == "A2" else 160

    fig = plt.figure(figsize=(fig_width, fig_height), dpi=dpi)
    canvas_agg = FigureCanvas(fig)
    
    # Match survey plan layout so scale/plot size aligns across previews
    map_left, map_bottom, map_width, map_height = 0.10, 0.30, 0.80, 0.45
    ax = fig.add_axes([map_left, map_bottom, map_width, map_height])

    requested_scale_ratio = parse_scale_ratio(scale_text)
    effective_scale_ratio = requested_scale_ratio
    scale_text_for_layout = scale_text
    if not use_topo_map:
        effective_scale_ratio, imagery_scaled = _choose_imagery_scale_ratio(
            requested_scale_ratio,
            fig_width=fig_width,
            fig_height=fig_height,
            map_width_frac=map_width,
            map_height_frac=map_height,
            dpi=dpi,
        )
        if imagery_scaled:
            scale_text_for_layout = f"1 : {effective_scale_ratio:,} (imagery view)"
    apply_true_scale(ax, poly, effective_scale_ratio, fig_width * map_width, fig_height * map_height)
    target_xlim = ax.get_xlim()
    target_ylim = ax.get_ylim()

    # Basemap - choose based on use_topo_map setting
    basemap_loaded = False
    axis_crs = f"EPSG:{display_epsg}"

    if use_topo_map:
        # Real contour lines from sampled elevation points, not a draped basemap image - see
        # _draw_topo_contours. "userdata" uses the surveyor's own uploaded heights when there are
        # enough of them; otherwise (or as a fallback) it samples a free global DEM for the plot.
        want_user_data = topo_source == "userdata" and elevation_points
        basemap_loaded = _draw_topo_contours(
            ax,
            plot_geom,
            display_epsg,
            target_xlim,
            target_ylim,
            elevation_points if want_user_data else None,
            font_scale=font_scale,
            is_user_data=bool(want_user_data),
            contour_interval_override=contour_interval,
        )
        # Roads/rivers/buildings are independent of whether contours rendered - a real topo map
        # depicts them regardless of how much elevation relief the site happens to have.
        _draw_topo_features(
            ax, db, plot_id, plot_geom, display_epsg, effective_scale_ratio, font_scale=font_scale, fig=fig,
        )
    else:
        sat_zoom = 16 if preview_mode else 17
        basemap_loaded = _try_add_arcgis_world_imagery(
            ax,
            target_xlim=target_xlim,
            target_ylim=target_ylim,
            axis_epsg=display_epsg,
            fig_width=fig_width,
            fig_height=fig_height,
            map_width_frac=map_width,
            map_height_frac=map_height,
            dpi=dpi,
            preview_mode=preview_mode,
        )
        if preview_mode:
            # Fast path for previews: no long fallback chain.
            if not basemap_loaded and MAPBOX_ACCESS_TOKEN:
                try:
                    ctx.add_basemap(
                        ax,
                        source=_mapbox_satellite_url(),
                        crs=axis_crs,
                        attribution=False,
                        zoom=sat_zoom + 1,
                        reset_extent=True,
                        timeout=_BASEMAP_FETCH_TIMEOUT,
                    )
                    basemap_loaded = True
                except Exception as e:
                    print(f"Mapbox Satellite failed: {e}")
            if not basemap_loaded:
                try:
                    ctx.add_basemap(
                        ax,
                        source=ctx.providers.Esri.WorldImagery,
                        crs=axis_crs,
                        attribution=False,
                        zoom=sat_zoom,
                        reset_extent=True,
                        timeout=_BASEMAP_FETCH_TIMEOUT,
                    )
                    basemap_loaded = True
                except Exception:
                    pass
            if not basemap_loaded:
                try:
                    ctx.add_basemap(
                        ax,
                        source=ctx.providers.OpenStreetMap.Mapnik,
                        crs=axis_crs,
                        attribution=False,
                        zoom=sat_zoom,
                        reset_extent=True,
                        timeout=_BASEMAP_FETCH_TIMEOUT,
                    )
                    basemap_loaded = True
                except Exception:
                    basemap_loaded = False
        else:
        # Use satellite/aerial imagery
            basemap_sources = [
                ("Esri WorldImagery", "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"),
                ("OpenStreetMap", "https://tile.openstreetmap.org/{z}/{x}/{y}.png"),
                ("CartoDB Light", "https://basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png"),
            ]

            # Mapbox Satellite first when configured - denser high-res (30-50cm) coverage in
            # Nigeria's larger cities than Esri's default World Imagery layer, plus @2x retina tiles.
            if not basemap_loaded and MAPBOX_ACCESS_TOKEN:
                try:
                    ctx.add_basemap(
                        ax,
                        source=_mapbox_satellite_url(),
                        crs=axis_crs,
                        attribution=False,
                        zoom=sat_zoom + 1,
                        reset_extent=True,
                        timeout=_BASEMAP_FETCH_TIMEOUT,
                    )
                    basemap_loaded = True
                except Exception as e:
                    print(f"Mapbox Satellite failed: {e}")

            # Then the contextily providers
            if not basemap_loaded:
                try:
                    ctx.add_basemap(
                        ax,
                        source=ctx.providers.Esri.WorldImagery,
                        crs=axis_crs,
                        attribution=False,
                        zoom=sat_zoom,
                        reset_extent=True,
                        timeout=_BASEMAP_FETCH_TIMEOUT,
                    )
                    basemap_loaded = True
                except Exception as e:
                    print(f"Esri WorldImagery failed: {e}")

            if not basemap_loaded:
                try:
                    ctx.add_basemap(
                        ax,
                        source=ctx.providers.OpenStreetMap.Mapnik,
                        crs=axis_crs,
                        attribution=False,
                        zoom=sat_zoom,
                        reset_extent=True,
                        timeout=_BASEMAP_FETCH_TIMEOUT,
                    )
                    basemap_loaded = True
                except Exception as e:
                    print(f"OpenStreetMap failed: {e}")

            if not basemap_loaded:
                # Try URL-based approach as last resort
                for name, url in basemap_sources:
                    try:
                        ctx.add_basemap(
                            ax,
                            source=url,
                            crs=axis_crs,
                            attribution=False,
                            zoom=sat_zoom,
                            reset_extent=True,
                            timeout=_BASEMAP_FETCH_TIMEOUT,
                        )
                        basemap_loaded = True
                        print(f"Loaded basemap from {name}")
                        break
                    except Exception as e:
                        print(f"{name} failed: {e}")
                        continue

    if not basemap_loaded:
        # Add a light green background to represent land if no basemap/contours loaded
        ax.set_facecolor('#e8f4e8')
        if use_topo_map:
            message = "No elevation data available for this plot\nShowing plot boundary"
        else:
            message = "Satellite imagery temporarily unavailable\nShowing plot boundary"
        ax.text(0.5, 0.5, message,
                transform=ax.transAxes, ha="center", va="center", fontsize=10, color="#555", alpha=0.8)

    # Restore target extent after basemap
    ax.set_xlim(target_xlim)
    ax.set_ylim(target_ylim)

    # Plot boundary (GeoPandas can autoscale; reapply limits afterward)
    gdf_plot.plot(ax=ax, facecolor="none", edgecolor="red", lw=2, zorder=20)
    ax.set_xlim(target_xlim)
    ax.set_ylim(target_ylim)

    # Grid Calculation
    span = max(ax.get_xlim()[1] - ax.get_xlim()[0], ax.get_ylim()[1] - ax.get_ylim()[0])
    major = nice_grid_step(span)

    # Features
    draw_grid(ax, major/5, major, font_scale)
    draw_coordinate_frame(ax, major, axis_epsg=display_epsg, label_epsg=display_epsg, font_scale=font_scale)

    # Add station name labels to vertices
    annotate_vertices_orthophoto(ax, poly, station_names, font_scale, scale_ratio=effective_scale_ratio)

    draw_sheet_frame(fig, font_scale)
    draw_title_block(fig, title_text, plot_id, scale_text_for_layout, location_text, lga_text, state_text, font_scale)
    draw_footer(fig, crs_footer_text, source_footer_text, surveyor_name, surveyor_rank, font_scale)
    add_north_arrow(ax, font_scale, style=north_arrow_style, color=north_arrow_color)
    add_scalebar(ax, choose_scalebar_length(effective_scale_ratio), font_scale=font_scale)

    ax.set_aspect("equal")
    ax.axis("off")

    # Save logic. Orthophoto/topo output is photographic (satellite/terrain tiles), which JPEG
    # compresses far more efficiently than PNG at equivalent visual quality - callers pass a
    # .jpg output_path so matplotlib infers JPEG from the extension; pil_kwargs controls quality.
    fig.canvas.draw()
    fig.savefig(output_path, dpi=dpi, pil_kwargs={"quality": 85, "optimize": True})
    plt.close(fig)


def render_orthophoto_pdf_from_png(png_path, pdf_path, paper_size="A4"):
    """Convert PNG to PDF with specified paper size"""
    try:
        if not os.path.exists(png_path) or os.path.getsize(png_path) < 2000:
            raise RuntimeError("Generated PNG is invalid or missing.")

        page_size = PAPER_SIZES_REPORTLAB.get(paper_size.upper(), A4)
        c = canvas.Canvas(pdf_path, pagesize=page_size)
        w, h = page_size
        img = ImageReader(png_path)
        c.drawImage(img, 0, 0, w, h, preserveAspectRatio=True)
        c.showPage()
        c.save()

    finally:
        # THE CLEANUP: Delete the PNG after the PDF is created
        if os.path.exists(png_path):
            os.remove(png_path)
            print(f"Cleaned up temporary image: {png_path}")
