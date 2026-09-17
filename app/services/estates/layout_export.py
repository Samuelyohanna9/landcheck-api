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


# Rough average glyph width for a sans-serif font, as a fraction of its point size - bold runs
# wider than regular. Good enough to keep a label from overflowing its plot without needing an
# actual font-metrics query (this file only ever runs headless, off-screen, generating one PDF).
_BOLD_CHAR_WIDTH = 0.62
_REGULAR_CHAR_WIDTH = 0.54


def _fit_fontsize(text: str, available_width_pts: float, available_height_pts: float, max_fontsize: float, *, char_width: float) -> float:
    """Largest font size (capped at max_fontsize) that keeps `text` inside the given width/height
    in points. Returns 0 if there's no room even at the smallest legible size, so the caller can
    skip the label instead of drawing overlapping garbage."""
    if not text or available_width_pts <= 0 or available_height_pts <= 0:
        return 0.0
    width_limited = available_width_pts / (len(text) * char_width)
    return max(0.0, min(max_fontsize, width_limited, available_height_pts))


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


def _format_total_area(area_sqm: float) -> str:
    if area_sqm >= 10000:
        return f"{area_sqm / 10000:,.2f} ha"
    return f"{area_sqm:,.0f} m²"


def _draw_double_boundary_line(ax, polygon, *, offset_m: float, color: str, linewidth: float, zorder: int) -> None:
    """Two parallel strokes (one just inside, one just outside the true line) instead of a single
    stroke - the conventional cadastral "double border" convention for a parent-parcel boundary.
    Falls back to a single (slightly heavier) line if buffering the polygon by offset_m produces
    nothing usable, e.g. an extremely small or degenerate boundary."""
    def _rings(geom):
        if geom is None or geom.is_empty:
            return []
        polys = list(geom.geoms) if geom.geom_type.startswith("Multi") else [geom]
        return [p for p in polys if not p.is_empty and p.exterior is not None]

    try:
        outer_rings = _rings(polygon.buffer(offset_m))
        inner_rings = _rings(polygon.buffer(-offset_m))
    except Exception:
        outer_rings, inner_rings = [], []

    if outer_rings and inner_rings:
        for poly in outer_rings + inner_rings:
            xs, ys = poly.exterior.xy
            ax.plot(xs, ys, color=color, linewidth=linewidth, zorder=zorder, solid_joinstyle="round", solid_capstyle="round")
    else:
        xs, ys = polygon.exterior.xy
        ax.plot(xs, ys, color=color, linewidth=linewidth * 1.8, zorder=zorder, solid_joinstyle="round", solid_capstyle="round")


PLOT_STATUS_FACE = {
    "available": PLOT_FACE,
    "reserved": "#fff4cc",
    "allocated": "#d9f2e3",
    "on_hold": "#eeeeee",
}


def render_estate_layout_thumbnail_png(
    *,
    estate: Any,
    plots: list[Any],
    features: list[Any],
    to_shape_fn,
    output_path: Any,
    figsize_inches: tuple[float, float] = (7.6, 4.7),
    dpi: int = 170,
) -> None:
    """A simplified, chrome-free snapshot of the layout (no legend/scale bar/north arrow, plots
    tinted by commercial status) sized for embedding inside another document - e.g. the Estate
    Performance Report - rather than standing alone as a survey plan in its own right.
    output_path accepts anything matplotlib's savefig does, including a BytesIO buffer."""
    all_polygons_wgs84: list[Polygon] = [to_shape_fn(plot.geometry) for plot in plots if plot.geometry]
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

    fig, ax = plt.subplots(figsize=figsize_inches)

    for feature in features:
        geometry = to_shape_fn(feature.geometry)
        metric_geometry = shapely_transform(forward, geometry)
        kind = str(feature.feature_type or "")
        face = DRAINAGE_FILL if kind == "drainage" else OPEN_SPACE_FILL if kind == "open_space" else ROAD_FILL
        geoms = list(metric_geometry.geoms) if metric_geometry.geom_type.startswith("Multi") or metric_geometry.geom_type == "GeometryCollection" else [metric_geometry]
        for part in geoms:
            if part.geom_type == "Polygon":
                ax.add_patch(mpatches.Polygon(list(part.exterior.coords), closed=True, facecolor=face, edgecolor="none", linewidth=0, zorder=1))
            elif part.geom_type == "LineString" and kind == "road":
                xs, ys = zip(*part.coords)
                ax.plot(xs, ys, color=ROAD_LINE, linewidth=2.4, solid_capstyle="round", zorder=1)

    metric_plot_polygons = []
    for plot in plots:
        if not plot.geometry:
            continue
        metric_polygon = shapely_transform(forward, to_shape_fn(plot.geometry))
        metric_plot_polygons.append(metric_polygon)
        face = PLOT_STATUS_FACE.get(str(plot.commercial_status or ""), PLOT_FACE)
        ax.add_patch(mpatches.Polygon(list(metric_polygon.exterior.coords), closed=True, facecolor=face, edgecolor=PLOT_EDGE, linewidth=0.6, zorder=2))

    if estate.boundary is not None:
        boundary_metric = shapely_transform(forward, to_shape_fn(estate.boundary))
        ax.add_patch(mpatches.Polygon(list(boundary_metric.exterior.coords), closed=True, facecolor="none", edgecolor=BOUNDARY_LINE, linewidth=1.8, zorder=5))

    metric_polys_for_bounds = [shapely_transform(forward, poly) for poly in all_polygons_wgs84]
    metric_minx = min(poly.bounds[0] for poly in metric_polys_for_bounds)
    metric_miny = min(poly.bounds[1] for poly in metric_polys_for_bounds)
    metric_maxx = max(poly.bounds[2] for poly in metric_polys_for_bounds)
    metric_maxy = max(poly.bounds[3] for poly in metric_polys_for_bounds)
    span_x = metric_maxx - metric_minx
    span_y = metric_maxy - metric_miny
    pad_x = span_x * 0.06 + 1
    pad_y = span_y * 0.06 + 1
    ax.set_xlim(metric_minx - pad_x, metric_maxx + pad_x)
    ax.set_ylim(metric_miny - pad_y, metric_maxy + pad_y)
    ax.set_aspect("equal")
    ax.axis("off")
    fig.tight_layout(pad=0.2)
    fig.savefig(output_path, dpi=dpi, bbox_inches="tight", facecolor="white")
    plt.close(fig)


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

    # Axis limits (and therefore the data-to-page scale) are fixed before anything is drawn, so the
    # plot loop below can measure each plot's actual on-page size and skip or shrink labels that
    # would otherwise overlap - a big Estate (hundreds of small plots on one sheet) has nowhere near
    # enough room to print a plot number and area on every single one without them running together.
    pad_x = span_x * 0.08 + 1
    pad_y = span_y * 0.19 + 1
    ax.set_xlim(metric_minx - pad_x, metric_maxx + pad_x)
    ax.set_ylim(metric_miny - pad_y, metric_maxy + pad_y)
    ax.set_aspect("equal")
    fig.canvas.draw()
    (x0_pt, _), (x1_pt, _) = ax.transData.transform((0, 0)), ax.transData.transform((1, 0))
    points_per_meter = (x1_pt - x0_pt) * 72.0 / fig.dpi

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

        # How much room this specific plot actually has on the printed page - not every plot on a
        # 500-lot sheet is big enough for a bold plot number, an area line and a customer name
        # without them overlapping the neighbouring plot's own labels. Sizing text against the
        # plot's actual width (not just a generic small/medium/large bucket) matters because a
        # narrow-but-tall plot and a short-but-wide one have very different room for the same text.
        pminx, pminy, pmaxx, pmaxy = metric_polygon.bounds
        width_pts = (pmaxx - pminx) * points_per_meter
        height_pts = (pmaxy - pminy) * points_per_meter
        MIN_LEGIBLE_FONT = 3.2

        number_text = str(plot.plot_number)
        number_fontsize = _fit_fontsize(number_text, width_pts * 0.9, height_pts * 0.85, 7.5 * scale, char_width=_BOLD_CHAR_WIDTH)
        if number_fontsize < MIN_LEGIBLE_FONT:
            continue  # no room for even the plot number without overlapping its neighbours - leave the boundary unlabeled

        area_label = f"{float(plot.area_sqm):,.0f} m²" if plot.area_sqm is not None else ""
        customer_name = (customer_names_by_plot_id or {}).get(plot.id)
        line_height = number_fontsize * 1.35

        area_fontsize = 0.0
        if area_label and height_pts >= line_height * 2:
            area_fontsize = _fit_fontsize(area_label, width_pts * 0.9, height_pts * 0.4, min(6 * scale, number_fontsize * 0.9), char_width=_REGULAR_CHAR_WIDTH)
            if area_fontsize < MIN_LEGIBLE_FONT:
                area_fontsize = 0.0

        customer_fontsize = 0.0
        if customer_name and area_fontsize and height_pts >= line_height * 3:
            customer_fontsize = _fit_fontsize(customer_name, width_pts * 0.9, height_pts * 0.32, min(5.8 * scale, number_fontsize * 0.85), char_width=_REGULAR_CHAR_WIDTH)
            if customer_fontsize < MIN_LEGIBLE_FONT:
                customer_fontsize = 0.0

        if not area_fontsize:
            ax.text(centroid.x, centroid.y, number_text, fontsize=number_fontsize, fontweight="bold", color=INK, ha="center", va="center", zorder=3)
        elif not customer_fontsize:
            ax.text(centroid.x, centroid.y + label_step * 0.9, number_text, fontsize=number_fontsize, fontweight="bold", color=INK, ha="center", va="center", zorder=3)
            ax.text(centroid.x, centroid.y - label_step * 0.9, area_label, fontsize=area_fontsize, color=MUTED, ha="center", va="center", zorder=3)
        else:
            ax.text(centroid.x, centroid.y + label_step * 1.0, number_text, fontsize=number_fontsize, fontweight="bold", color=INK, ha="center", va="center", zorder=3)
            ax.text(centroid.x, centroid.y - label_step * 0.55, area_label, fontsize=area_fontsize, color=MUTED, ha="center", va="center", zorder=3)
            ax.text(centroid.x, centroid.y - label_step * 1.7, customer_name, fontsize=customer_fontsize, color=MUTED, ha="center", va="center", zorder=3, style="italic")

    if estate.boundary is not None:
        boundary_metric = shapely_transform(forward, to_shape_fn(estate.boundary))
        # The parent parcel boundary - a double red line (two parallel strokes), the conventional
        # cadastral "double border" style, drawn over every subdivision line so the overall extent
        # always reads clearly. The gap between the two strokes is sized in real page points (not
        # a fraction of the site's own size), so it looks the same on a 2-plot infill as on a
        # 500-plot estate.
        boundary_gap_m = (2.6 / points_per_meter) if points_per_meter > 0 else max(span_x, span_y) * 0.003
        _draw_double_boundary_line(ax, boundary_metric, offset_m=boundary_gap_m, color=BOUNDARY_LINE, linewidth=1.3 * scale, zorder=5)

        # Each boundary edge's real-world length, centered on the edge and set just outside the
        # line - away from the estate's own centroid, so it sits over open page rather than the
        # plots it encloses - the standard survey-plan convention of dimensioning the parent
        # parcel's own perimeter.
        boundary_centroid = boundary_metric.centroid
        boundary_label_offset = (16.0 / points_per_meter) if points_per_meter > 0 else max(span_x, span_y) * 0.018
        min_edge_pts = 24.0  # too short on the page to hold "123.45m" without overlapping its neighbours
        exterior_coords = list(boundary_metric.exterior.coords)
        for i in range(len(exterior_coords) - 1):
            x1, y1 = exterior_coords[i]
            x2, y2 = exterior_coords[i + 1]
            edge_length_m = math.hypot(x2 - x1, y2 - y1)
            if edge_length_m * points_per_meter < min_edge_pts:
                continue
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            dx, dy = mx - boundary_centroid.x, my - boundary_centroid.y
            dist = math.hypot(dx, dy) or 1.0
            ox, oy = dx / dist, dy / dist
            label_x = mx + ox * boundary_label_offset
            label_y = my + oy * boundary_label_offset
            # Keep the text upright (never upside down) while still running parallel to its edge.
            angle_deg = math.degrees(math.atan2(y2 - y1, x2 - x1))
            if angle_deg > 90:
                angle_deg -= 180
            elif angle_deg < -90:
                angle_deg += 180
            ax.text(
                label_x, label_y, f"{edge_length_m:,.2f}m",
                fontsize=6.5 * scale, color=BOUNDARY_LINE, ha="center", va="center",
                rotation=angle_deg, rotation_mode="anchor", fontweight="bold", clip_on=False, zorder=6,
            )

    ax.axis("off")

    _draw_scale_bar(ax, minx=metric_minx, miny=metric_miny, span_x=span_x, scale=scale)
    _draw_north_arrow(ax, scale=scale)

    # Bottom-row layout (scale bar, legend, title block) is worked out in real page points, then
    # converted to axes-fraction, rather than fixed axes-fraction guesses - a tall/narrow estate's
    # axes box is far fewer points wide than a wide one's (aspect="equal" shrinks whichever
    # dimension has "extra" room), so a fixed fraction like "start the legend at 0.30" means a
    # very different amount of real space depending on the estate's own shape.
    axes_width_pts = (span_x + 2 * pad_x) * points_per_meter if points_per_meter > 0 else 0.0
    axes_height_pts = (span_y + 2 * pad_y) * points_per_meter if points_per_meter > 0 else 0.0
    def _pts_to_frac(pts: float) -> float:
        return (pts / axes_width_pts) if axes_width_pts > 0 else pts / 1000.0
    def _pts_to_frac_y(pts: float) -> float:
        return (pts / axes_height_pts) if axes_height_pts > 0 else pts / 1000.0

    total_area_sqm = boundary_metric.area if estate.boundary is not None else sum(float(p.area_sqm or 0) for p in plots)
    generated_at = datetime.now(timezone.utc).strftime("%d %b %Y")

    # Title moved to a centered, all-caps heading at the top of the sheet (was a small bordered
    # card at bottom-right) - the double-line frame added at the very end of this function is what
    # now visually "borders" the page, so this no longer needs its own box.
    title_heading = f"LAYOUT PLAN FOR {(estate.name or 'ESTATE').upper()}"
    title_org = (organization_name or "").upper()
    title_info = (
        f"{len(plots)} PLOTS · TOTAL AREA {_format_total_area(total_area_sqm).upper()} · "
        f"GENERATED {generated_at.upper()} · CRS EPSG:{metric_epsg}"
    )
    heading_font_pts = 13.0 * scale
    org_font_pts = 9.5 * scale
    info_font_pts = 8.0 * scale

    def _required_shrink(text: str, font_pts: float, char_width: float, max_width_pts: float) -> float:
        width_pts = len(text) * font_pts * char_width
        return (max_width_pts / width_pts) if width_pts > max_width_pts > 0 else 1.0

    # All three lines shrink together (never independently, so the heading stays visibly the
    # biggest) whenever the longest of them - usually title_info, not the heading itself - would
    # otherwise overflow the sheet's own width. Matters most for a narrow/tall estate, where the
    # axes box is far fewer points wide than a wide one's.
    # All-caps text (this whole title block) runs wider per character than the mixed-case text
    # _REGULAR_CHAR_WIDTH was calibrated against elsewhere in this file, so the bold estimate is
    # used for all three lines here, with a tighter width cap - better to shrink a bit more than
    # strictly necessary than to let a line clip the frame.
    max_title_line_width_pts = axes_width_pts * 0.78 if axes_width_pts > 0 else None
    if max_title_line_width_pts:
        shrink = min(
            _required_shrink(title_heading, heading_font_pts, _BOLD_CHAR_WIDTH, max_title_line_width_pts),
            _required_shrink(title_org, org_font_pts, _BOLD_CHAR_WIDTH, max_title_line_width_pts) if title_org else 1.0,
            _required_shrink(title_info, info_font_pts, _BOLD_CHAR_WIDTH, max_title_line_width_pts),
        )
        if shrink < 1.0:
            heading_font_pts = max(heading_font_pts * shrink, 7.0 * scale)
            org_font_pts = max(org_font_pts * shrink, 6.0 * scale)
            info_font_pts = max(info_font_pts * shrink, 5.2 * scale)

    title_top_y = 0.965
    ax.text(0.5, title_top_y, title_heading, transform=ax.transAxes, ha="center", va="top", fontsize=heading_font_pts, fontweight="bold", color=INK, clip_on=False, zorder=11)
    next_y = title_top_y - _pts_to_frac_y(heading_font_pts * 1.5)
    if title_org:
        ax.text(0.5, next_y, title_org, transform=ax.transAxes, ha="center", va="top", fontsize=org_font_pts, color=INK, clip_on=False, zorder=11)
        next_y -= _pts_to_frac_y(org_font_pts * 1.5)
    ax.text(0.5, next_y, title_info, transform=ax.transAxes, ha="center", va="top", fontsize=info_font_pts, color=MUTED, clip_on=False, zorder=11)

    legend_font_pts = 8.5 * scale
    swatch_pts = 9.5 * scale
    swatch_frac = _pts_to_frac(swatch_pts)
    legend_items = [
        ("swatch", PLOT_FACE, "Plot"),
        ("swatch", ROAD_FILL, "Road"),
        ("swatch", OPEN_SPACE_FILL, "Open space"),
        ("swatch", DRAINAGE_FILL, "Drainage"),
        ("line", BOUNDARY_LINE, "Estate boundary"),
    ]
    legend_label_pts = [len("LEGEND") * legend_font_pts * _BOLD_CHAR_WIDTH + 14 * scale]
    legend_label_pts += [swatch_pts * 1.4 + len(label) * legend_font_pts * _REGULAR_CHAR_WIDTH + 16 * scale for _, _, label in legend_items]
    legend_row_width_frac = _pts_to_frac(sum(legend_label_pts))

    scale_bar_end_x = (metric_minx + span_x * 0.02) + _round_scale_length(span_x * 0.22)
    scale_bar_end_frac = ((scale_bar_end_x - (metric_minx - pad_x)) / (span_x + 2 * pad_x)) if (span_x + 2 * pad_x) > 0 else 0.25
    # The scale bar itself is positioned in data coordinates (so its size stays tied to the real
    # drawing, not the page), which means its axes-fraction Y position - unlike everything else on
    # this row - shifts with the estate's own aspect ratio. Its own topmost element is the "SCALE
    # (METRES)" caption, `bar_height * 2.6` above its baseline (see _draw_scale_bar).
    scale_bar_top_data = (metric_miny - span_x * 0.06) + (span_x * 0.011) * 2.6
    scale_bar_top_frac = ((scale_bar_top_data - (metric_miny - pad_y)) / (span_y + 2 * pad_y)) if (span_y + 2 * pad_y) > 0 else 0.05

    row_y = 0.024
    legend_x = max(0.30, scale_bar_end_frac + 0.02)
    if legend_x + legend_row_width_frac > 0.97:
        # Not enough room to fit the scale bar and legend side by side on one row for this
        # estate's shape - the legend gets its own row above them instead, clearing whichever sits
        # higher: the base row itself, or the scale bar's own caption.
        legend_row_y = max(row_y + swatch_frac + 0.02, scale_bar_top_frac + 0.015)
        legend_x = 0.02
    else:
        legend_row_y = row_y

    cursor_x = legend_x
    ax.text(cursor_x, legend_row_y + swatch_frac / 2, "LEGEND", transform=ax.transAxes, ha="left", va="center", fontsize=legend_font_pts, fontweight="bold", color=INK, clip_on=False, zorder=11)
    cursor_x += _pts_to_frac(legend_label_pts[0])
    for (kind, color, label), item_width_pts in zip(legend_items, legend_label_pts[1:]):
        if kind == "swatch":
            ax.add_patch(mpatches.Rectangle((cursor_x, legend_row_y), swatch_frac, swatch_frac, transform=ax.transAxes, facecolor=color, edgecolor=INK, linewidth=0.8 * scale, clip_on=False, zorder=11))
        else:
            ax.plot([cursor_x, cursor_x + swatch_frac], [legend_row_y + swatch_frac / 2] * 2, color=color, linewidth=2.6 * scale, transform=ax.transAxes, clip_on=False, zorder=11)
        text_x = cursor_x + swatch_frac * 1.4
        ax.text(text_x, legend_row_y + swatch_frac / 2, label, transform=ax.transAxes, ha="left", va="center", fontsize=legend_font_pts, color=INK, clip_on=False, zorder=11)
        cursor_x += _pts_to_frac(item_width_pts)

    # A double-line black frame around the entire sheet - the title heading, scale bar, legend and
    # the site plan itself all sit inside it, the conventional bordered "drawing sheet" convention
    # (distinct from the estate's own red boundary line drawn earlier, which traces the actual
    # parcel, not the page). Drawn last, in axes-fraction, so it's the true outermost element -
    # bbox_inches="tight" then sizes the saved page to it rather than to whatever happens to be
    # widest among the title/scale/legend/labels.
    frame_margin = 0.01
    frame_gap = 0.007
    for m in (frame_margin, frame_margin + frame_gap):
        ax.add_patch(mpatches.Rectangle(
            (m, m), 1 - 2 * m, 1 - 2 * m, transform=ax.transAxes,
            fill=False, edgecolor="black", linewidth=1.5 * scale, clip_on=False, zorder=20,
        ))

    with PdfPages(output_path) as pdf:
        pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)
    return {"plot_count": len(plots), "feature_count": len(features), "coordinate_epsg": metric_epsg, "paper_size": str(paper_size or "A3").upper(), "included_customer_names": bool(customer_names_by_plot_id)}
