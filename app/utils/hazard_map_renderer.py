from __future__ import annotations

import io
import math
from typing import Any, Dict, List, Optional, Tuple

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import matplotlib.tri as mtri
import numpy as np
from matplotlib.collections import PolyCollection
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

# Graduated depth bands matching how professional flood hazard maps (e.g. German HQ/HWGK
# hydraulic reports) present flow depth - a light-to-dark blue ramp with a fixed legend, not a
# single flat "risk color" fill.
DEPTH_BANDS = [0.05, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5]
DEPTH_BAND_LABELS = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5]
DEPTH_COLORS = ["#e0f2fe", "#bae6fd", "#7dd3fc", "#38bdf8", "#0ea5e9", "#0284c7", "#1d4ed8", "#1e3a8a"]

BUILDING_THREATENED_COLOR = "#dc2626"
BUILDING_SAFE_COLOR = "#9ca3af"
BOUNDARY_COLOR = "#0f172a"
CONTOUR_COLOR = "#78350f"
LAND_COLOR = "#f4f1e6"

# Slope steepness bands - a green-to-red earth-tone ramp is the standard cartographic convention
# for terrain/erosion hazard (vs. flood's blue), so the two hazard types read as visually distinct
# products at a glance even before a viewer reads either legend.
SLOPE_BAND_LABELS = [0.0, 2.0, 5.0, 10.0, 15.0, 25.0]
SLOPE_COLORS = ["#dcfce7", "#bbf7d0", "#fde047", "#fb923c", "#ef4444", "#7f1d1d"]
BUILDING_SLOPE_THREATENED_DEG = 15.0


def _local_slope_triangulation(points: List[Dict[str, float]], display_epsg: int):
    """Projects the surveyor's own elevation points and triangulates them, returning
    (xs, ys, triang, per_triangle_slope_deg, sliver_mask) for facet-shaded rendering - slope from
    a sparse point set is a per-triangle-face quantity (constant across each flat facet of the
    TIN), not a smoothly interpolated per-node value, so this is rendered with a flat-shaded
    PolyCollection, not tricontourf.

    A triangle with a near-zero horizontal (map-view) footprint - e.g. from near-collinear points
    at the edge of a small/regular point set - can report a near-vertical slope from pure
    floating-point noise even though the real terrain is gentle. Those slivers are masked out here
    (both from rendering and from the trifinder used to classify buildings) rather than being left
    to show up as a fake "cliff" patch or a phantom top-of-scale legend entry.
    """
    xs, ys, zs = _points_to_projected_xyz(points, "elevation_m", display_epsg)
    if len(xs) < 3:
        return None
    triang = mtri.Triangulation(xs, ys)

    horizontal_areas = []
    for tri_indices in triang.triangles:
        x0, y0 = xs[tri_indices[0]], ys[tri_indices[0]]
        x1, y1 = xs[tri_indices[1]], ys[tri_indices[1]]
        x2, y2 = xs[tri_indices[2]], ys[tri_indices[2]]
        horizontal_areas.append(abs((x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)) / 2.0)
    horizontal_areas = np.array(horizontal_areas)
    min_area = max(float(np.median(horizontal_areas)) * 0.05, 1e-6) if len(horizontal_areas) else 0.0
    sliver_mask = horizontal_areas < min_area
    if sliver_mask.any():
        triang.set_mask(sliver_mask)

    facet_slopes = []
    for tri_indices in triang.triangles:
        p0 = np.array([xs[tri_indices[0]], ys[tri_indices[0]], zs[tri_indices[0]]])
        p1 = np.array([xs[tri_indices[1]], ys[tri_indices[1]], zs[tri_indices[1]]])
        p2 = np.array([xs[tri_indices[2]], ys[tri_indices[2]], zs[tri_indices[2]]])
        normal = np.cross(p1 - p0, p2 - p0)
        normal_len = float(np.linalg.norm(normal))
        if normal_len < 1e-9:
            facet_slopes.append(0.0)
            continue
        cos_angle = max(-1.0, min(1.0, abs(normal[2]) / normal_len))
        facet_slopes.append(math.degrees(math.acos(cos_angle)))
    return xs, ys, triang, np.array(facet_slopes), sliver_mask


def _display_epsg_for(geom_wgs84: BaseGeometry) -> int:
    centroid = geom_wgs84.centroid
    utm_zone = int((centroid.x + 180) / 6) + 1
    return 32600 + utm_zone if centroid.y >= 0 else 32700 + utm_zone


def _points_to_projected_xyz(points: List[Dict[str, float]], value_key: str, display_epsg: int):
    lons = [float(p["lng"]) for p in points]
    lats = [float(p["lat"]) for p in points]
    values = [float(p[value_key]) for p in points]
    gdf = gpd.GeoDataFrame(
        {"value": values},
        geometry=gpd.points_from_xy(lons, lats),
        crs="EPSG:4326",
    ).to_crs(epsg=display_epsg)
    xs = gdf.geometry.x.to_numpy()
    ys = gdf.geometry.y.to_numpy()
    vals = gdf["value"].to_numpy()
    finite = np.isfinite(xs) & np.isfinite(ys) & np.isfinite(vals)
    return xs[finite], ys[finite], vals[finite]


def _nice_scalebar_length(span_m: float) -> int:
    for candidate in (10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000):
        if span_m / candidate <= 6:
            return candidate
    return 10000


def _draw_scalebar(ax, span_m: float):
    length_m = _nice_scalebar_length(span_m)
    segments = 4
    seg_len = length_m / segments
    x0 = ax.get_xlim()[0] + span_m * 0.04
    y0 = ax.get_ylim()[0] + span_m * 0.04
    bar_h = span_m * 0.012
    for i in range(segments):
        face = "black" if i % 2 == 0 else "white"
        ax.add_patch(mpatches.Rectangle(
            (x0 + i * seg_len, y0), seg_len, bar_h,
            facecolor=face, edgecolor="black", linewidth=0.8, zorder=15,
        ))
    ax.text(x0, y0 - span_m * 0.025, "0", ha="center", fontsize=7, zorder=15)
    for i in range(1, segments + 1):
        ax.text(x0 + i * seg_len, y0 - span_m * 0.025, f"{int(length_m * i / segments)}", ha="center", fontsize=7, zorder=15)
    ax.text(x0 + (segments * seg_len) / 2, y0 + bar_h + span_m * 0.015, "Metres", ha="center", fontsize=7.5, weight="bold", zorder=15)


def _draw_north_arrow(ax, span_m: float):
    x = ax.get_xlim()[1] - span_m * 0.06
    y_base = ax.get_ylim()[1] - span_m * 0.14
    size = span_m * 0.045
    ax.annotate(
        "N", xy=(x, y_base + size), xytext=(x, y_base),
        ha="center", fontsize=10, weight="bold", color=BOUNDARY_COLOR,
        arrowprops=dict(facecolor=BOUNDARY_COLOR, edgecolor=BOUNDARY_COLOR, width=2.5, headwidth=9),
        zorder=15,
    )


def _draw_contours(ax, contour_points: Optional[List[Dict[str, float]]], display_epsg: int) -> None:
    """Terrain elevation contour lines for spatial context - shared by both hazard map types."""
    if not contour_points or len(contour_points) < 3:
        return
    try:
        xs, ys, elevations = _points_to_projected_xyz(contour_points, "elevation_m", display_epsg)
        if len(xs) < 3 or float(np.ptp(elevations)) <= 0.5:
            return
        triang = mtri.Triangulation(xs, ys)
        span = float(np.ptp(elevations))
        step = next((s for ceiling, s in ((2, 0.25), (5, 0.5), (10, 1), (25, 2), (50, 5), (100, 10), (250, 20)) if span <= ceiling), 50)
        levels = np.arange(math.floor(float(np.min(elevations)) / step) * step, float(np.max(elevations)) + step, step)
        if len(levels) >= 2:
            ax.tricontour(triang, elevations, levels=levels, colors=CONTOUR_COLOR, linewidths=0.5, alpha=0.55, zorder=1)
    except Exception:
        pass


def _slope_band_colors_for(slopes: np.ndarray) -> np.ndarray:
    thresholds = SLOPE_BAND_LABELS[1:]
    indices = np.clip(np.digitize(slopes, thresholds), 0, len(SLOPE_COLORS) - 1)
    return np.array(SLOPE_COLORS)[indices]


def _draw_buildings(ax, buildings: List[BaseGeometry], display_epsg: int, threatened_fn) -> Tuple[int, int]:
    """Plots real building footprints, red where threatened_fn(centroid_x, centroid_y) says so.
    threatened_fn returns a boolean array given projected centroid coordinate arrays.
    """
    if not buildings:
        return 0, 0
    try:
        gdf_buildings = gpd.GeoDataFrame(geometry=buildings, crs="EPSG:4326").to_crs(epsg=display_epsg)
        gdf_buildings = gdf_buildings[gdf_buildings.geometry.is_valid & ~gdf_buildings.geometry.is_empty]
        buildings_total = len(gdf_buildings)
        if buildings_total == 0:
            return 0, 0
        centroids = gdf_buildings.geometry.centroid
        cx = centroids.x.to_numpy()
        cy = centroids.y.to_numpy()
        threatened_mask = threatened_fn(cx, cy)
        gdf_buildings["threatened"] = threatened_mask
        buildings_threatened = int(np.asarray(threatened_mask).sum())
        safe_gdf = gdf_buildings[~gdf_buildings["threatened"]]
        if len(safe_gdf):
            safe_gdf.plot(ax=ax, facecolor=BUILDING_SAFE_COLOR, edgecolor="#4b5563", linewidth=0.3, zorder=4)
        threatened_gdf = gdf_buildings[gdf_buildings["threatened"]]
        if len(threatened_gdf):
            threatened_gdf.plot(ax=ax, facecolor=BUILDING_THREATENED_COLOR, edgecolor="#7f1d1d", linewidth=0.4, zorder=5)
        return buildings_total, buildings_threatened
    except Exception:
        return 0, 0


def render_flood_hazard_map(
    boundary_geojson: Dict[str, Any],
    depth_points: Optional[List[Dict[str, float]]],
    contour_points: Optional[List[Dict[str, float]]],
    buildings: List[BaseGeometry],
    risk_class: str,
    class_color: str,
    return_period: int,
    buffer_m: float = 1000,
) -> Tuple[bytes, Dict[str, int]]:
    """Renders a premium, locally-composited flood hazard map: a graduated depth surface (real
    JRC/GloFAS values, not a flat risk-tier fill), terrain contour context, and real OSM building
    footprints colored red where they sit in the flood zone. Returns (png_bytes, stats) where
    stats reports how many buildings were found and how many are threatened.
    """
    boundary_geom = shape(boundary_geojson)
    display_epsg = _display_epsg_for(boundary_geom)

    gdf_boundary = gpd.GeoDataFrame(geometry=[boundary_geom], crs="EPSG:4326").to_crs(epsg=display_epsg)
    boundary_proj = gdf_boundary.geometry.iloc[0]
    minx, miny, maxx, maxy = boundary_proj.buffer(buffer_m).bounds
    span_m = max(maxx - minx, maxy - miny)

    fig, ax = plt.subplots(figsize=(9.2, 8.4), dpi=150)
    ax.set_aspect("equal")
    ax.set_facecolor(LAND_COLOR)
    ax.set_xlim(minx, maxx)
    ax.set_ylim(miny, maxy)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)

    # Terrain contour lines - context only, drawn first so everything else sits on top.
    _draw_contours(ax, contour_points, display_epsg)

    depth_available = False
    drawn_band_labels: List[float] = []
    drawn_band_colors: List[str] = []
    depth_capped = False
    if depth_points and len(depth_points) >= 3:
        try:
            xs, ys, depths = _points_to_projected_xyz(depth_points, "depth_m", display_epsg)
            if len(xs) >= 3 and float(np.nanmax(depths)) > 0.05:
                triang = mtri.Triangulation(xs, ys)
                max_depth = float(np.nanmax(depths))
                # Only include bands the data actually reaches, so the legend matches what's
                # really on the map instead of always showing the full 0-3.5m+ palette.
                band_count = sum(1 for b in DEPTH_BAND_LABELS if b < max_depth) or 1
                depth_capped = band_count < len(DEPTH_BAND_LABELS)
                levels = [DEPTH_BANDS[0]] + DEPTH_BAND_LABELS[1:band_count] + [max_depth + 0.01]
                colors_for_levels = DEPTH_COLORS[:band_count]
                ax.tricontourf(triang, depths, levels=levels, colors=colors_for_levels, zorder=2, alpha=0.88)
                depth_interp = mtri.LinearTriInterpolator(triang, depths)
                depth_available = True
                drawn_band_labels = DEPTH_BAND_LABELS[:band_count]
                drawn_band_colors = colors_for_levels
        except Exception:
            depth_available = False

    # Buildings - the headline upgrade: real footprints, colored by whether they actually sit in
    # the flood zone, rather than an abstract polygon-level score.
    def _flood_threatened(cx, cy):
        if not depth_available:
            return np.zeros(len(cx), dtype=bool)
        interpolated = depth_interp(cx, cy)
        values = np.ma.filled(interpolated, 0.0)
        mask_invalid = np.ma.getmaskarray(interpolated) if np.ma.is_masked(interpolated) else np.zeros(len(cx), dtype=bool)
        return (~mask_invalid) & (values > 0.05)

    buildings_total, buildings_threatened = _draw_buildings(ax, buildings, display_epsg, _flood_threatened)

    # Plot boundary outline, always on top.
    gdf_boundary.plot(ax=ax, facecolor="none", edgecolor=BOUNDARY_COLOR, linewidth=2.4, zorder=10)

    _draw_scalebar(ax, span_m)
    _draw_north_arrow(ax, span_m)

    ax.set_title(
        f"Flood Hazard Map — {return_period}-Year Return Period",
        fontsize=12, weight="bold", color="#111827", pad=10,
    )

    legend_handles = []
    if depth_available:
        legend_handles.append(mpatches.Patch(facecolor="none", edgecolor="none", label=f"RP{return_period} Flow Depth"))
        for i, lo in enumerate(drawn_band_labels):
            is_last = i == len(drawn_band_labels) - 1
            if is_last and depth_capped:
                label = f"> {lo:.1f} m"
            else:
                hi = drawn_band_labels[i + 1] if not is_last else lo + 0.5
                label = f"{lo:.1f} - {hi:.1f} m"
            legend_handles.append(mpatches.Patch(facecolor=drawn_band_colors[i], edgecolor="none", label=label))
    if buildings_total:
        legend_handles.append(mpatches.Patch(facecolor=BUILDING_THREATENED_COLOR, edgecolor="#7f1d1d", label="Threatened building"))
        legend_handles.append(mpatches.Patch(facecolor=BUILDING_SAFE_COLOR, edgecolor="#4b5563", label="Other building"))
    legend_handles.append(mpatches.Patch(facecolor="none", edgecolor=BOUNDARY_COLOR, linewidth=2, label="Plot boundary"))

    if legend_handles:
        ax.legend(
            handles=legend_handles, loc="upper left", fontsize=7.5, framealpha=0.92,
            edgecolor="#d1d5db", handlelength=1.4, handleheight=1.4, borderpad=0.7,
        )

    if not depth_available:
        ax.text(
            0.5, 0.5, "No flood depth data available for this location\nShowing plot boundary and buildings only",
            transform=ax.transAxes, ha="center", va="center", fontsize=10, color="#6b7280",
        )

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=150)
    plt.close(fig)
    buf.seek(0)

    return buf.getvalue(), {"buildings_total": buildings_total, "buildings_threatened": buildings_threatened}


def render_erosion_hazard_map(
    boundary_geojson: Dict[str, Any],
    local_elevation_points: Optional[List[Dict[str, float]]],
    dem_slope_points: Optional[List[Dict[str, float]]],
    contour_points: Optional[List[Dict[str, float]]],
    buildings: List[BaseGeometry],
    risk_class: str,
    class_color: str,
    buffer_m: float = 500,
) -> Tuple[bytes, Dict[str, int]]:
    """Renders a premium erosion hazard map: a graduated slope-steepness surface (green-to-red,
    the standard terrain-hazard convention, distinct from flood's blue) plus terrain contours and
    real building footprints colored red where they sit on erosion-prone ground. When the
    surveyor's own elevation points are available, slope is rendered as flat-shaded TIN facets
    (a real triangulated surface from their data) instead of the smoother DEM-derived surface.
    """
    boundary_geom = shape(boundary_geojson)
    display_epsg = _display_epsg_for(boundary_geom)

    gdf_boundary = gpd.GeoDataFrame(geometry=[boundary_geom], crs="EPSG:4326").to_crs(epsg=display_epsg)
    boundary_proj = gdf_boundary.geometry.iloc[0]
    minx, miny, maxx, maxy = boundary_proj.buffer(buffer_m).bounds
    span_m = max(maxx - minx, maxy - miny)

    fig, ax = plt.subplots(figsize=(9.2, 8.4), dpi=150)
    ax.set_aspect("equal")
    ax.set_facecolor(LAND_COLOR)
    ax.set_xlim(minx, maxx)
    ax.set_ylim(miny, maxy)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)

    _draw_contours(ax, contour_points, display_epsg)

    slope_available = False
    slope_from_local = False
    used_band_indices: List[int] = []
    slope_interp = None
    trifinder = None
    facet_colors_by_index: Optional[np.ndarray] = None

    if local_elevation_points and len(local_elevation_points) >= 3:
        try:
            result = _local_slope_triangulation(local_elevation_points, display_epsg)
            if result is not None:
                xs, ys, triang, facet_slopes, sliver_mask = result
                trifinder = triang.get_trifinder()
                thresholds = SLOPE_BAND_LABELS[1:]
                facet_band_idx = np.clip(np.digitize(facet_slopes, thresholds), 0, len(SLOPE_COLORS) - 1)
                # Slivers keep a real (if meaningless) band index so trifinder lookups into a
                # masked triangle don't crash, but they must never be drawn or counted in the
                # legend - only genuine, non-degenerate facets should be visible.
                facet_colors_by_index = facet_band_idx
                keep = ~sliver_mask
                face_colors = np.array(SLOPE_COLORS)[facet_band_idx[keep]]
                triangle_verts = [[(xs[i], ys[i]) for i in tri] for tri in triang.triangles[keep]]
                if triangle_verts:
                    pc = PolyCollection(triangle_verts, facecolors=face_colors, edgecolors="none", zorder=2, alpha=0.88)
                    ax.add_collection(pc)
                    slope_available = True
                    slope_from_local = True
                    used_band_indices = sorted(set(facet_band_idx[keep].tolist()))
        except Exception:
            slope_available = False

    if not slope_available and dem_slope_points and len(dem_slope_points) >= 3:
        try:
            xs, ys, slopes = _points_to_projected_xyz(dem_slope_points, "slope_deg", display_epsg)
            if len(xs) >= 3:
                triang = mtri.Triangulation(xs, ys)
                max_slope = float(np.nanmax(slopes))
                band_count = sum(1 for b in SLOPE_BAND_LABELS if b < max_slope) or 1
                levels = [SLOPE_BAND_LABELS[0]] + SLOPE_BAND_LABELS[1:band_count] + [max(max_slope, SLOPE_BAND_LABELS[0]) + 0.5]
                levels = sorted(set(levels))
                if len(levels) >= 2:
                    colors_for_levels = SLOPE_COLORS[: len(levels) - 1]
                    ax.tricontourf(triang, slopes, levels=levels, colors=colors_for_levels, zorder=2, alpha=0.88)
                    slope_interp = mtri.LinearTriInterpolator(triang, slopes)
                    slope_available = True
                    used_band_indices = list(range(len(colors_for_levels)))
        except Exception:
            slope_available = False

    def _erosion_threatened(cx, cy):
        n = len(cx)
        if not slope_available:
            return np.zeros(n, dtype=bool)
        if slope_from_local and trifinder is not None:
            tri_idx = trifinder(np.asarray(cx), np.asarray(cy))
            inside = tri_idx >= 0
            band_idx = np.zeros(n, dtype=int)
            band_idx[inside] = facet_colors_by_index[tri_idx[inside]]
            slope_estimate = np.array(SLOPE_BAND_LABELS)[band_idx]
            return inside & (slope_estimate >= BUILDING_SLOPE_THREATENED_DEG)
        if slope_interp is not None:
            interpolated = slope_interp(cx, cy)
            values = np.ma.filled(interpolated, 0.0)
            mask_invalid = np.ma.getmaskarray(interpolated) if np.ma.is_masked(interpolated) else np.zeros(n, dtype=bool)
            return (~mask_invalid) & (values >= BUILDING_SLOPE_THREATENED_DEG)
        return np.zeros(n, dtype=bool)

    buildings_total, buildings_threatened = _draw_buildings(ax, buildings, display_epsg, _erosion_threatened)

    gdf_boundary.plot(ax=ax, facecolor="none", edgecolor=BOUNDARY_COLOR, linewidth=2.4, zorder=10)

    _draw_scalebar(ax, span_m)
    _draw_north_arrow(ax, span_m)

    ax.set_title("Erosion Hazard Map — Slope Steepness", fontsize=12, weight="bold", color="#111827", pad=10)

    legend_handles = []
    if slope_available:
        source_label = "Your Surveyed Slope" if slope_from_local else "Estimated Slope (30m DEM)"
        legend_handles.append(mpatches.Patch(facecolor="none", edgecolor="none", label=source_label))
        band_edges = SLOPE_BAND_LABELS + [None]
        for idx in used_band_indices:
            lo = band_edges[idx]
            hi = band_edges[idx + 1] if idx + 1 < len(band_edges) else None
            label = f"{lo:.0f} - {hi:.0f}°" if hi is not None else f"> {lo:.0f}°"
            legend_handles.append(mpatches.Patch(facecolor=SLOPE_COLORS[idx], edgecolor="none", label=label))
    if buildings_total:
        legend_handles.append(mpatches.Patch(facecolor=BUILDING_THREATENED_COLOR, edgecolor="#7f1d1d", label=f"On slope > {BUILDING_SLOPE_THREATENED_DEG:.0f}°"))
        legend_handles.append(mpatches.Patch(facecolor=BUILDING_SAFE_COLOR, edgecolor="#4b5563", label="Other building"))
    legend_handles.append(mpatches.Patch(facecolor="none", edgecolor=BOUNDARY_COLOR, linewidth=2, label="Plot boundary"))

    if legend_handles:
        ax.legend(
            handles=legend_handles, loc="upper left", fontsize=7.5, framealpha=0.92,
            edgecolor="#d1d5db", handlelength=1.4, handleheight=1.4, borderpad=0.7,
        )

    if not slope_available:
        ax.text(
            0.5, 0.5, "No slope data available for this location\nShowing plot boundary and buildings only",
            transform=ax.transAxes, ha="center", va="center", fontsize=10, color="#6b7280",
        )

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=150)
    plt.close(fig)
    buf.seek(0)

    return buf.getvalue(), {"buildings_total": buildings_total, "buildings_threatened": buildings_threatened}
