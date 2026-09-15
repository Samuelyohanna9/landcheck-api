from __future__ import annotations

"""Renders a whole Estate's approved layout - every plot, road, drainage reserve and open space -
as one clean, single-page technical plan PDF, styled after a conventional Nigerian survey/site
plan (red parent boundary, black subdivision lines, a chequered graphical scale, a two-tone north
arrow). This is deliberately separate from the per-plot Survey Plan renderer
(app/utils/map_renderer_layout.py), which draws one plot's own title-block plan; this one draws
the whole subdivision at once, the way a developer would hand out a site layout plan to a buyer or
a field team.
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
PLOT_EDGE = "#000000"
ROAD_FILL = "#c9cdd2"
ROAD_LINE = "#8a9099"
DRAINAGE_FILL = "#bcdcee"
OPEN_SPACE_FILL = "#cdeedb"
BOUNDARY_LINE = "#d1332b"  # red parent-parcel boundary, per surveying convention
INK = "#101827"
MUTED = "#6b7685"

# ISO paper sizes, landscape (width mm, height mm).
PAPER_SIZES_MM: dict[str, tuple[float, float]] = {
    "A0": (1189, 841),
    "A1": (841, 594),
    "A2": (594, 420),
    "A3": (420, 297),
    "A4": (297, 210),
}


def _figsize_inches(paper_size: str) -> tuple[float, float]:
    width_mm, height_mm = PAPER_SIZES_MM.get(str(paper_size or "A3").strip().upper(), PAPER_SIZES_MM["A3"])
    return (width_mm / 25.4, height_mm / 25.4)


def _paper_scale_factor(paper_size: str) -> float:
    """Font sizes and line widths in matplotlib are absolute (points), completely independent of
    figure size - so without this, an A0 sheet would carry the exact same tiny text as an A4 one,
    just spread across a much bigger page. This scales every point-based size relative to A3
    (this renderer's baseline), using the page diagonal so both ISO axes contribute evenly."""
    a3_w, a3_h = _figsize_inches("A3")
    width_in, height_in = _figsize_inches(paper_size)
    return math.hypot(width_in, height_in) / math.hypot(a3_w, a3_h)


def _round_scale_length(target_m: float) -> float:
    """Pick a clean round scale-bar length (1/2/5 x 10^n) near the target."""
    if target_m <= 0:
        return 100.0
    exponent = math.floor(math.log10(target_m))
    base = target_m / (10 ** exponent)
    nice = 1 if base < 1.5 else 2 if base < 3.5 else 5 if base < 7.5 else 10
    return nice * (10 ** exponent)


def _draw_scale_bar(ax, *, minx: float, miny: float, span_x: float, scale: float) -> None:
    """A chequered (alternating black/white) graphical scale bar, the conventional survey-plan
    style - unlike a stated "Scale 1:N" ratio, it stays accurate even if the sheet is later
    resized or photocopied, since it's measured directly off the same drawing."""
    total_length = _round_scale_length(span_x * 0.22)
    segments = 4
    seg_length = total_length / segments
    bar_height = span_x * 0.011
    x0 = minx + span_x * 0.02
    y0 = miny - span_x * 0.06
    for i in range(segments):
        color = "black" if i % 2 == 0 else "white"
        ax.add_patch(mpatches.Rectangle((x0 + i * seg_length, y0), seg_length, bar_height, facecolor=color, edgecolor="black", linewidth=0.9 * scale, clip_on=False, zorder=9))
    for i in range(segments + 1):
        x = x0 + i * seg_length
        ax.plot([x, x], [y0 - bar_height * 0.25, y0 + bar_height * 1.25], color="black", linewidth=0.9 * scale, clip_on=False, zorder=9)
        ax.text(x, y0 - bar_height * 1.7, f"{i * seg_length:,.0f}", ha="center", va="top", fontsize=6.5 * scale, color=INK, clip_on=False, zorder=9)
    ax.text(x0 + total_length / 2, y0 + bar_height * 2.6, "SCALE (METRES)", ha="center", va="bottom", fontsize=7 * scale, fontweight="bold", color=INK, clip_on=False, zorder=9)


def _draw_north_arrow(ax, *, scale: float) -> None:
    """A two-tone kite north arrow (left half solid, right half outline) - the conventional
    surveying symbol, rather than a plain annotate() arrow."""
    cx, cy = 0.965, 0.885
    h, w = 0.052, 0.016
    top = (cx, cy + h)
    bottom = (cx, cy - h * 0.35)
    mid_left = (cx - w, cy)
    mid_right = (cx + w, cy)
    left_half = plt.Polygon([top, mid_left, bottom], closed=True, transform=ax.transAxes, facecolor=INK, edgecolor=INK, linewidth=0.9 * scale, clip_on=False, zorder=12)
    right_half = plt.Polygon([top, mid_right, bottom], closed=True, transform=ax.transAxes, facecolor="white", edgecolor=INK, linewidth=0.9 * scale, clip_on=False, zorder=12)
    ax.add_patch(left_half)
    ax.add_patch(right_half)
    ax.text(cx, cy - h * 0.35 - 0.018, "N", transform=ax.transAxes, ha="center", va="top", fontsize=12 * scale, fontweight="bold", color=INK, clip_on=False, zorder=12)


def _legend_swatch(ax, x: float, y: float, size: float, *, facecolor: str, label: str, scale: float) -> None:
    ax.add_patch(mpatches.Rectangle((x, y), size, size, transform=ax.transAxes, facecolor=facecolor, edgecolor=INK, linewidth=0.8 * scale, clip_on=False, zorder=11))
    ax.text(x + size * 1.6, y + size / 2, label, transform=ax.transAxes, ha="left", va="center", fontsize=8.5 * scale, color=INK, clip_on=False, zorder=11)


def render_estate_layout_pdf(
    *,
    estate: Any,
    organization_name: str,
    plots: list[Any],
    features: list[Any],
    to_shape_fn,
    output_path: str,
    paper_size: str = "A3",
    customer_names_by_plot_id: dict[int, str] | None = None,
) -> dict:
    """plots: list of EstatePlot rows (geometry present). features: list of EstateSpatialFeature
    rows (status active). to_shape_fn: geoalchemy2.shape.to_shape, passed in to avoid importing
    the DB layer twice. customer_names_by_plot_id: when given, each plot with an entry gets the
    customer's name printed under its area - omit (None/empty) for a clean, name-free plan suited
    to marketing/PR use. Returns a small dict of counts for the caller to log/audit."""
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
    label_margin = label_step * 1.6

    scale = _paper_scale_factor(paper_size)
    fig, ax = plt.subplots(figsize=_figsize_inches(paper_size))

    for feature in features:
        geometry = to_shape_fn(feature.geometry)
        metric_geometry = shapely_transform(forward, geometry)
        kind = str(feature.feature_type or "")
        face = DRAINAGE_FILL if kind == "drainage" else OPEN_SPACE_FILL if kind == "open_space" else ROAD_FILL
        geoms = list(metric_geometry.geoms) if metric_geometry.geom_type.startswith("Multi") or metric_geometry.geom_type == "GeometryCollection" else [metric_geometry]
        label_anchor = metric_geometry.centroid
        line_road_part = None
        for part in geoms:
            if part.geom_type == "Polygon":
                ax.add_patch(mpatches.Polygon(list(part.exterior.coords), closed=True, facecolor=face, edgecolor="none", linewidth=0, zorder=1))
            elif part.geom_type == "LineString" and kind == "road":
                xs, ys = zip(*part.coords)
                ax.plot(xs, ys, color=ROAD_LINE, linewidth=3.2 * scale, solid_capstyle="round", zorder=1)
                line_road_part = part
        if feature.name and not label_anchor.is_empty:
            label_x, label_y = label_anchor.x, label_anchor.y
            if line_road_part is not None and len(line_road_part.coords) >= 2:
                # Offset perpendicular to the road's own direction (not a fixed global direction),
                # so the label moves off the line itself rather than drifting toward whatever
                # happens to be "up" - which could be outside the Estate boundary entirely.
                (x1, y1), (x2, y2) = line_road_part.coords[0], line_road_part.coords[-1]
                dx, dy = x2 - x1, y2 - y1
                length = math.hypot(dx, dy) or 1.0
                nx, ny = -dy / length, dx / length
                label_x += nx * label_step * 1.6
                label_y += ny * label_step * 1.6
            # Clamp so a label can never render outside the plotted extent (e.g. a road running
            # right along the boundary edge).
            label_x = min(max(label_x, metric_minx + label_margin), metric_maxx - label_margin)
            label_y = min(max(label_y, metric_miny + label_margin), metric_maxy - label_margin)
            ax.text(
                label_x, label_y, feature.name,
                fontsize=7 * scale, color=MUTED, ha="center", va="center", zorder=4, style="italic",
                bbox=dict(boxstyle="round,pad=0.2", facecolor="white", edgecolor="none", alpha=0.85),
            )

    for plot in plots:
        polygon_wgs84 = to_shape_fn(plot.geometry)
        metric_polygon = shapely_transform(forward, polygon_wgs84)
        # Black subdivision lines - only the outer parent boundary is drawn in red, below.
        ax.add_patch(mpatches.Polygon(list(metric_polygon.exterior.coords), closed=True, facecolor=PLOT_FACE, edgecolor=PLOT_EDGE, linewidth=0.9 * scale, zorder=2))
        centroid = metric_polygon.centroid
        area_label = f"{float(plot.area_sqm):,.0f} m²" if plot.area_sqm is not None else ""
        customer_name = (customer_names_by_plot_id or {}).get(plot.id)
        ax.text(centroid.x, centroid.y + label_step * (1.0 if customer_name else 0.9), str(plot.plot_number), fontsize=7.5 * scale, fontweight="bold", color=INK, ha="center", va="center", zorder=3)
        if area_label:
            ax.text(centroid.x, centroid.y - label_step * (0.55 if customer_name else 0.9), area_label, fontsize=6 * scale, color=MUTED, ha="center", va="center", zorder=3)
        if customer_name:
            ax.text(centroid.x, centroid.y - label_step * 1.7, customer_name, fontsize=5.8 * scale, color=MUTED, ha="center", va="center", zorder=3, style="italic")

    if estate.boundary is not None:
        boundary_metric = shapely_transform(forward, to_shape_fn(estate.boundary))
        # The parent parcel boundary - solid red, per surveying convention, drawn over every
        # subdivision line so the overall extent always reads clearly.
        ax.add_patch(mpatches.Polygon(list(boundary_metric.exterior.coords), closed=True, facecolor="none", edgecolor=BOUNDARY_LINE, linewidth=2.6 * scale, zorder=5))

    pad_x = span_x * 0.08 + 1
    pad_y = span_y * 0.19 + 1
    ax.set_xlim(metric_minx - pad_x, metric_maxx + pad_x)
    ax.set_ylim(metric_miny - pad_y, metric_maxy + pad_y)
    ax.set_aspect("equal")
    ax.axis("off")

    _draw_scale_bar(ax, minx=metric_minx, miny=metric_miny, span_x=span_x, scale=scale)
    _draw_north_arrow(ax, scale=scale)

    # Row pitch and box height are both derived from the same scaled font size used for the
    # legend text, instead of a fixed axes-fraction guess - the earlier fixed 0.021 step was tuned
    # for a smaller legend font and started overlapping once that font size was bumped up.
    legend_x = 0.02
    box_top = 0.99
    title_y = box_top - 0.028
    step = 0.034
    entries = [
        (PLOT_FACE, "Plot"),
        (ROAD_FILL, "Road"),
        (OPEN_SPACE_FILL, "Open space"),
        (DRAINAGE_FILL, "Drainage"),
    ]
    row_ys = [title_y - step * (index + 1.15) for index in range(len(entries) + 1)]  # +1 for the boundary row
    box_bottom = row_ys[-1] - step * 0.55
    legend_box = mpatches.FancyBboxPatch(
        (0.008, box_bottom), 0.175, box_top - box_bottom, transform=ax.transAxes,
        boxstyle="round,pad=0.006,rounding_size=0.006", facecolor="white", edgecolor=INK, linewidth=0.9 * scale, zorder=10,
    )
    ax.add_patch(legend_box)
    ax.text(legend_x, title_y, "LEGEND", transform=ax.transAxes, fontsize=8.5 * scale, fontweight="bold", color=INK, zorder=11)
    for (facecolor, label), row_y in zip(entries, row_ys):
        _legend_swatch(ax, legend_x, row_y, 0.012, facecolor=facecolor, label=label, scale=scale)
    boundary_row_y = row_ys[-1]
    ax.plot([legend_x, legend_x + 0.012], [boundary_row_y + 0.006, boundary_row_y + 0.006], color=BOUNDARY_LINE, linewidth=2.6 * scale, transform=ax.transAxes, clip_on=False, zorder=11)
    ax.text(legend_x + 0.012 * 1.6, boundary_row_y + 0.006, "Estate boundary", transform=ax.transAxes, ha="left", va="center", fontsize=8.5 * scale, color=INK, clip_on=False, zorder=11)

    generated_at = datetime.now(timezone.utc).strftime("%d %b %Y")
    title_text = (
        f"{estate.name or 'Estate'}\n"
        f"{organization_name or ''}\n"
        f"Site Layout Plan\n"
        f"{len(plots)} plots · Generated {generated_at} · CRS EPSG:{metric_epsg}"
    )
    ax.text(
        0.985, 0.025, title_text, transform=ax.transAxes, ha="right", va="bottom",
        fontsize=9 * scale, color=INK, linespacing=1.7, zorder=11,
        bbox=dict(boxstyle="round,pad=0.5", facecolor="white", edgecolor=INK, linewidth=0.9 * scale),
    )

    with PdfPages(output_path) as pdf:
        pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)
    return {"plot_count": len(plots), "feature_count": len(features), "coordinate_epsg": metric_epsg, "paper_size": str(paper_size or "A3").upper(), "included_customer_names": bool(customer_names_by_plot_id)}
