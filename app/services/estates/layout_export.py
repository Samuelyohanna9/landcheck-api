from __future__ import annotations

"""Renders a whole Estate's approved layout - every plot, road, drainage reserve and open space -
as one clean, single-page technical plan PDF. This is deliberately separate from the per-plot
Survey Plan renderer (app/utils/map_renderer_layout.py), which draws one plot's own title-block
plan; this one draws the whole subdivision at once, the way a developer would hand out a site
layout plan to a buyer or a field team.
"""

import math
from datetime import datetime, timezone
from typing import Any

import matplotlib
if matplotlib.get_backend().lower() != "agg":
    matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.backends.backend_pdf import PdfPages
from pyproj import Transformer
from shapely.geometry import Polygon
from shapely.ops import transform as shapely_transform

from app.routers.plots import _metric_epsg_for_wgs84_polygon

PLOT_FACE = "#fdfdfb"
PLOT_EDGE = "#2b2b2b"
ROAD_FILL = "#c9cdd2"
ROAD_LINE = "#8a9099"
DRAINAGE_FILL = "#bcdcee"
OPEN_SPACE_FILL = "#cdeedb"
BOUNDARY_LINE = "#1a8f5a"
INK = "#101827"
MUTED = "#6b7685"


def _round_scale_length(target_m: float) -> float:
    """Pick a clean round scale-bar length (1/2/5 x 10^n) near the target."""
    if target_m <= 0:
        return 100.0
    exponent = math.floor(math.log10(target_m))
    base = target_m / (10 ** exponent)
    nice = 1 if base < 1.5 else 2 if base < 3.5 else 5 if base < 7.5 else 10
    return nice * (10 ** exponent)


def _draw_scale_bar(ax, *, minx: float, miny: float, span_x: float) -> None:
    length = _round_scale_length(span_x * 0.18)
    x0 = minx + span_x * 0.02
    y0 = miny - span_x * 0.04
    ax.plot([x0, x0 + length], [y0, y0], color=INK, linewidth=1.6, solid_capstyle="butt", clip_on=False)
    for x in (x0, x0 + length / 2, x0 + length):
        ax.plot([x, x], [y0 - span_x * 0.004, y0 + span_x * 0.004], color=INK, linewidth=1.2, clip_on=False)
    ax.text(x0 + length / 2, y0 - span_x * 0.018, f"{length:,.0f} m", ha="center", va="top", fontsize=7, color=INK, clip_on=False)


def _draw_north_arrow(ax) -> None:
    ax.annotate(
        "N",
        xy=(0.965, 0.90), xycoords="axes fraction",
        xytext=(0.965, 0.80), textcoords="axes fraction",
        ha="center", va="center", fontsize=10, fontweight="bold", color=INK,
        arrowprops=dict(arrowstyle="-|>", color=INK, lw=1.6),
    )


def _legend_swatch(ax, x: float, y: float, size: float, *, facecolor: str, edgecolor: str, label: str) -> float:
    ax.add_patch(mpatches.Rectangle((x, y), size, size, transform=ax.transAxes, facecolor=facecolor, edgecolor=edgecolor, linewidth=0.8, clip_on=False, zorder=11))
    ax.text(x + size * 1.5, y + size / 2, label, transform=ax.transAxes, ha="left", va="center", fontsize=7.5, color=INK, clip_on=False, zorder=11)
    return y


def render_estate_layout_pdf(*, estate: Any, organization_name: str, plots: list[Any], features: list[Any], to_shape_fn, output_path: str) -> dict:
    """plots: list of EstatePlot rows (geometry present). features: list of EstateSpatialFeature
    rows (status active). to_shape_fn: geoalchemy2.shape.to_shape, passed in to avoid importing
    the DB layer twice. Returns a small dict of counts for the caller to log/audit."""
    all_polygons_wgs84: list[Polygon] = [to_shape_fn(plot.geometry) for plot in plots]
    if estate.boundary is not None:
        all_polygons_wgs84.append(to_shape_fn(estate.boundary))
    if not all_polygons_wgs84:
        raise ValueError("Nothing to draw - this Estate has no plot or boundary geometry yet")

    minx = min(poly.bounds[0] for poly in all_polygons_wgs84)
    miny = min(poly.bounds[1] for poly in all_polygons_wgs84)
    maxx = max(poly.bounds[2] for poly in all_polygons_wgs84)
    maxy = max(poly.bounds[3] for poly in all_polygons_wgs84)
    envelope = Polygon([(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy)])
    metric_epsg = _metric_epsg_for_wgs84_polygon(envelope)
    forward = Transformer.from_crs("EPSG:4326", f"EPSG:{metric_epsg}", always_xy=True).transform

    metric_polygons = [shapely_transform(forward, poly) for poly in all_polygons_wgs84]
    metric_minx = min(poly.bounds[0] for poly in metric_polygons)
    metric_miny = min(poly.bounds[1] for poly in metric_polygons)
    metric_maxx = max(poly.bounds[2] for poly in metric_polygons)
    metric_maxy = max(poly.bounds[3] for poly in metric_polygons)
    span_x = metric_maxx - metric_minx
    span_y = metric_maxy - metric_miny
    label_step = max(span_x, span_y) * 0.014

    fig, ax = plt.subplots(figsize=(16.5, 11.7))  # A3 landscape at 72dpi proportions

    for feature in features:
        geometry = to_shape_fn(feature.geometry)
        metric_geometry = shapely_transform(forward, geometry)
        kind = str(feature.feature_type or "")
        face = DRAINAGE_FILL if kind == "drainage" else OPEN_SPACE_FILL if kind == "open_space" else ROAD_FILL
        geoms = list(metric_geometry.geoms) if metric_geometry.geom_type.startswith("Multi") or metric_geometry.geom_type == "GeometryCollection" else [metric_geometry]
        label_point = metric_geometry.centroid
        for part in geoms:
            if part.geom_type == "Polygon":
                ax.add_patch(mpatches.Polygon(list(part.exterior.coords), closed=True, facecolor=face, edgecolor="none", linewidth=0, zorder=1))
            elif part.geom_type == "LineString" and kind == "road":
                xs, ys = zip(*part.coords)
                ax.plot(xs, ys, color=ROAD_LINE, linewidth=3.2, solid_capstyle="round", zorder=1)
        if feature.name and not label_point.is_empty:
            ax.text(
                label_point.x, label_point.y + label_step * 1.8, feature.name,
                fontsize=6, color=MUTED, ha="center", va="center", zorder=4, style="italic",
                bbox=dict(boxstyle="round,pad=0.15", facecolor="white", edgecolor="none", alpha=0.8),
            )

    for plot in plots:
        polygon_wgs84 = to_shape_fn(plot.geometry)
        metric_polygon = shapely_transform(forward, polygon_wgs84)
        ax.add_patch(mpatches.Polygon(list(metric_polygon.exterior.coords), closed=True, facecolor=PLOT_FACE, edgecolor=PLOT_EDGE, linewidth=0.7, zorder=2))
        centroid = metric_polygon.centroid
        area_label = f"{float(plot.area_sqm):,.0f} m²" if plot.area_sqm is not None else ""
        ax.text(centroid.x, centroid.y + label_step * 0.9, str(plot.plot_number), fontsize=6.5, fontweight="bold", color=INK, ha="center", va="center", zorder=3)
        if area_label:
            ax.text(centroid.x, centroid.y - label_step * 0.9, area_label, fontsize=5.2, color=MUTED, ha="center", va="center", zorder=3)

    if estate.boundary is not None:
        boundary_metric = shapely_transform(forward, to_shape_fn(estate.boundary))
        ax.add_patch(mpatches.Polygon(list(boundary_metric.exterior.coords), closed=True, facecolor="none", edgecolor=BOUNDARY_LINE, linewidth=1.8, linestyle=(0, (6, 3)), zorder=5))

    pad_x = span_x * 0.08 + 1
    pad_y = span_y * 0.16 + 1
    ax.set_xlim(metric_minx - pad_x, metric_maxx + pad_x)
    ax.set_ylim(metric_miny - pad_y, metric_maxy + pad_y)
    ax.set_aspect("equal")
    ax.axis("off")

    _draw_scale_bar(ax, minx=metric_minx, miny=metric_miny, span_x=span_x)
    _draw_north_arrow(ax)

    legend_box = mpatches.FancyBboxPatch(
        (0.008, 0.865), 0.155, 0.125, transform=ax.transAxes,
        boxstyle="round,pad=0.006,rounding_size=0.006", facecolor="white", edgecolor="#e4e8ec", linewidth=0.8, zorder=10,
    )
    ax.add_patch(legend_box)
    legend_x = 0.02
    legend_y = 0.958
    step = 0.021
    ax.text(legend_x, legend_y, "LEGEND", transform=ax.transAxes, fontsize=7.5, fontweight="bold", color=INK, zorder=11)
    _legend_swatch(ax, legend_x, legend_y - step * 1.3, 0.012, facecolor=PLOT_FACE, edgecolor=PLOT_EDGE, label="Plot")
    _legend_swatch(ax, legend_x, legend_y - step * 2.3, 0.012, facecolor=ROAD_FILL, edgecolor="none", label="Road")
    _legend_swatch(ax, legend_x, legend_y - step * 3.3, 0.012, facecolor=OPEN_SPACE_FILL, edgecolor="none", label="Open space")
    _legend_swatch(ax, legend_x, legend_y - step * 4.3, 0.012, facecolor=DRAINAGE_FILL, edgecolor="none", label="Drainage")

    generated_at = datetime.now(timezone.utc).strftime("%d %b %Y")
    title_text = (
        f"{estate.name or 'Estate'}\n"
        f"{organization_name or ''}\n"
        f"Site Layout Plan\n"
        f"{len(plots)} plots · Generated {generated_at} · CRS EPSG:{metric_epsg}"
    )
    ax.text(
        0.985, 0.025, title_text, transform=ax.transAxes, ha="right", va="bottom",
        fontsize=8, color=INK, linespacing=1.7, zorder=11,
        bbox=dict(boxstyle="round,pad=0.5", facecolor="white", edgecolor="#e4e8ec", linewidth=0.8),
    )

    with PdfPages(output_path) as pdf:
        pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)
    return {"plot_count": len(plots), "feature_count": len(features), "coordinate_epsg": metric_epsg}
