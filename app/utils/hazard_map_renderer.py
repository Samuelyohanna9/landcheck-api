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
    if contour_points and len(contour_points) >= 3:
        try:
            xs, ys, elevations = _points_to_projected_xyz(contour_points, "elevation_m", display_epsg)
            if len(xs) >= 3 and float(np.ptp(elevations)) > 0.5:
                triang = mtri.Triangulation(xs, ys)
                span = float(np.ptp(elevations))
                step = next((s for ceiling, s in ((2, 0.25), (5, 0.5), (10, 1), (25, 2), (50, 5), (100, 10), (250, 20)) if span <= ceiling), 50)
                levels = np.arange(math.floor(float(np.min(elevations)) / step) * step, float(np.max(elevations)) + step, step)
                if len(levels) >= 2:
                    ax.tricontour(triang, elevations, levels=levels, colors=CONTOUR_COLOR, linewidths=0.5, alpha=0.55, zorder=1)
        except Exception:
            pass

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
    buildings_total = 0
    buildings_threatened = 0
    if buildings:
        try:
            gdf_buildings = gpd.GeoDataFrame(geometry=buildings, crs="EPSG:4326").to_crs(epsg=display_epsg)
            gdf_buildings = gdf_buildings[gdf_buildings.geometry.is_valid & ~gdf_buildings.geometry.is_empty]
            buildings_total = len(gdf_buildings)
            if buildings_total > 0:
                threatened_mask = np.zeros(buildings_total, dtype=bool)
                if depth_available:
                    centroids = gdf_buildings.geometry.centroid
                    cx = centroids.x.to_numpy()
                    cy = centroids.y.to_numpy()
                    interpolated = depth_interp(cx, cy)
                    values = np.ma.filled(interpolated, 0.0)
                    mask_invalid = np.ma.getmaskarray(interpolated) if np.ma.is_masked(interpolated) else np.zeros(buildings_total, dtype=bool)
                    threatened_mask = (~mask_invalid) & (values > 0.05)
                gdf_buildings["threatened"] = threatened_mask
                buildings_threatened = int(threatened_mask.sum())
                safe_gdf = gdf_buildings[~gdf_buildings["threatened"]]
                if len(safe_gdf):
                    safe_gdf.plot(ax=ax, facecolor=BUILDING_SAFE_COLOR, edgecolor="#4b5563", linewidth=0.3, zorder=4)
                threatened_gdf = gdf_buildings[gdf_buildings["threatened"]]
                if len(threatened_gdf):
                    threatened_gdf.plot(ax=ax, facecolor=BUILDING_THREATENED_COLOR, edgecolor="#7f1d1d", linewidth=0.4, zorder=5)
        except Exception:
            pass

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
