# app/utils/map_renderer_layout.py
# Professional survey plan layout with TRUE SCALE rendering
# Supports multiple paper sizes: A4, A3, A2, A1, A0

import matplotlib
matplotlib.use("Agg")

import math
import textwrap
import re
import numpy as np
import geopandas as gpd
import matplotlib.pyplot as plt
from sqlalchemy import text
from shapely import wkb
from shapely.geometry import LineString, Point, Polygon, shape
from shapely.ops import snap, linemerge, unary_union
import matplotlib.patches as patches
import matplotlib.lines as mlines
import matplotlib.patheffects as patheffects
from matplotlib.font_manager import FontProperties
from matplotlib.path import Path
from datetime import datetime
from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas
import contextily as ctx
from app.utils.orthophoto_renderer import (
    _try_add_arcgis_world_imagery,
    _mapbox_satellite_url,
    MAPBOX_ACCESS_TOKEN,
    _BASEMAP_FETCH_TIMEOUT,
)

# ======================
# Paper Size Configuration
# ======================
# Paper sizes in inches (width, height) for portrait orientation
PAPER_SIZES = {
    "A4": (8.27, 11.69),     # 210 x 297 mm
    "A3": (11.69, 16.54),    # 297 x 420 mm
    "A2": (16.54, 23.39),    # 420 x 594 mm
    "A1": (23.39, 33.11),    # 594 x 841 mm
    "A0": (33.11, 46.81),    # 841 x 1189 mm
}

# Scale factors for fonts and elements relative to A4
SCALE_FACTORS = {
    # Slightly larger default typography to improve readability and better
    # match the reference template output.
    "A4": 1.15,
    "A3": 1.44,
    "A2": 1.72,
    "A1": 2.06,
    "A0": 2.53,
}

DEFAULT_CERTIFICATION_STATEMENT = (
    "I hereby certify that this survey plan is a true representation of the survey "
    "executed by me and conforms with the regulations of surveying profession."
)
DEFAULT_ADAMAWA_AUTHORITY_TITLE = "SURVEYOR GENERAL"
DEFAULT_ADAMAWA_AUTHORITY_DATE = "November, 2024"
DEFAULT_ADAMAWA_ORIGIN_TEXT = "ORIGIN:- WGS 84 UTM ZONE 33N"
DEFAULT_ADAMAWA_TOPO_SHEET_TEXT = "BASED ON GIREI TOPO SHEET 197 NE"
DEFAULT_ADAMAWA_DISCLAIMER_TEXT = (
    "Detail shewn not the result of accurate survey. All bearing and distances shewn on this plan "
    "have been computed from registered Co-ordinates."
)
DEFAULT_ADAMAWA_CHECKED_BY_TEXT = "Checked by OCX................"
DEFAULT_ADAMAWA_PASSED_BY_TEXT = "Passed by Carto..............."
DEFAULT_ADAMAWA_COPYRIGHT_TEXT = "Copy Right Reserved"
DEFAULT_ADAMAWA_PREPARED_BY_TEXT = "Plan Prepared by Office of the Surveyor General Adamawa State"
ADAMAWA_FONT_FAMILY = "DejaVu Serif"
ADAMAWA_BLUE = "#1f2f8a"


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
    """Get paper dimensions and scale factor"""
    size = paper_size.upper() if paper_size else "A4"
    if size not in PAPER_SIZES:
        size = "A4"
    return {
        "width": PAPER_SIZES[size][0],
        "height": PAPER_SIZES[size][1],
        "scale": SCALE_FACTORS[size],
        "name": size,
    }

# ======================
# Geometry & Scale Helpers
# ======================

def calculate_bearing_deg(p1: Point, p2: Point) -> float:
    dx, dy = p2.x - p1.x, p2.y - p1.y
    return (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0


def _clockwise_ring_coords_and_labels(poly, station_names=None):
    coords = list(poly.exterior.coords)
    if len(coords) < 2:
        return coords, []

    vertex_count = max(0, len(coords) - 1)
    default_labels = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    labels = []
    for idx in range(vertex_count):
        if station_names and idx < len(station_names):
            raw = station_names[idx]
        else:
            raw = default_labels[idx % len(default_labels)]
        label = str(raw or "").strip() or default_labels[idx % len(default_labels)]
        labels.append(label)

    try:
        is_ccw = bool(poly.exterior.is_ccw)
    except Exception:
        is_ccw = False

    if not is_ccw or vertex_count <= 2:
        return coords, labels

    # Normalize to clockwise while preserving the first entered point as the start.
    body = coords[:-1]
    reordered_body = [body[0], *reversed(body[1:])]
    reordered_coords = reordered_body + [reordered_body[0]]
    reordered_labels = [labels[0], *reversed(labels[1:])] if labels else labels
    return reordered_coords, reordered_labels


def format_bearing_dms(bearing_deg: float) -> str:
    """Degrees-minutes, for the on-drawing plan display (the back-computation report's own
    deg_to_dms shows seconds - this is deliberately left at minute precision for the plan)."""
    total_minutes = int(round(bearing_deg * 60.0)) % 21600  # 360 * 60, wraps safely
    deg, minutes = divmod(total_minutes, 60)
    return f"{deg}\u00B0{minutes:02d}\u2032"


def format_area_display(area_m2: float) -> str:
    """Below 1 hectare (10,000 sq m), express the area in square meters; at or above, switch to
    hectares - the threshold real Nigerian survey plans use, applied consistently across every
    template's area display instead of each one hardcoding its own fixed unit."""
    if area_m2 < 10000:
        return f"{area_m2:,.3f} SQ. MTRS."
    return f"{area_m2 / 10000.0:,.4f} HA."


def nice_grid_step(span_m: float) -> float:
    if span_m <= 0:
        return 100.0
    base = 10 ** math.floor(math.log10(span_m))
    steps = np.array([0.02, 0.05, 0.1, 0.2, 0.5, 1.0]) * base
    target = span_m / 6.0
    return float(steps[np.argmin(np.abs(steps - target))])


def parse_scale_ratio(scale_text: str) -> int:
    try:
        s = str(scale_text).strip().replace(" ", "")
        if ":" in s:
            _, right = s.split(":")
            return max(1, int(right))
        return max(1, int(s))
    except Exception:
        return 1000


def is_auto_scale_text(scale_text: str | None) -> bool:
    raw = str(scale_text or "").strip().lower()
    if not raw:
        return True
    normalized = raw.replace(" ", "")
    return normalized in {"auto", "fit", "autofit", "1:auto", "1:fit", "1:autofit"}


def compute_fit_scale_ratio(
    geom_for_extent,
    map_width_in: float,
    map_height_in: float,
    *,
    min_scale_ratio: int = 100,
    max_scale_ratio: int = 50000,
    padding_factor: float = 1.18,
) -> int:
    minx, miny, maxx, maxy = geom_for_extent.bounds
    width_m = max(float(maxx - minx), 0.01)
    height_m = max(float(maxy - miny), 0.01)
    inch_to_m = 0.0254
    usable_width_m = max(float(map_width_in) * inch_to_m, 0.001)
    usable_height_m = max(float(map_height_in) * inch_to_m, 0.001)
    ratio_w = width_m / usable_width_m
    ratio_h = height_m / usable_height_m
    fitted = math.ceil(max(ratio_w, ratio_h) * max(padding_factor, 1.0))
    return max(min_scale_ratio, min(max_scale_ratio, int(fitted or min_scale_ratio)))


def resolve_scale_text_and_ratio(
    scale_text: str | None,
    geom_for_extent,
    map_width_in: float,
    map_height_in: float,
    *,
    min_scale_ratio: int = 100,
    max_scale_ratio: int = 50000,
    padding_factor: float = 1.18,
) -> tuple[str, int]:
    if is_auto_scale_text(scale_text):
        ratio = compute_fit_scale_ratio(
            geom_for_extent,
            map_width_in,
            map_height_in,
            min_scale_ratio=min_scale_ratio,
            max_scale_ratio=max_scale_ratio,
            padding_factor=padding_factor,
        )
        return f"1 : {ratio}", ratio
    ratio = parse_scale_ratio(str(scale_text or "1 : 1000"))
    return f"1 : {ratio}", ratio


def road_half_width_m(highway: str) -> float:
    # Use a fixed, reasonable cartographic width for all roads
    return 3.0


def apply_true_scale(ax, geom_for_extent, scale_ratio: int, map_width_in: float, map_height_in: float):
    minx, miny, maxx, maxy = geom_for_extent.bounds
    cx, cy = (minx + maxx) / 2.0, (miny + maxy) / 2.0
    inch_to_m = 0.0254
    real_w = map_width_in * inch_to_m * scale_ratio
    real_h = map_height_in * inch_to_m * scale_ratio
    ax.set_xlim(cx - real_w / 2.0, cx + real_w / 2.0)
    ax.set_ylim(cy - real_h / 2.0, cy + real_h / 2.0)


# Reference scale a "normal" pen weight is calibrated against - 1:2000 is a typical single-plot
# survey plan scale. Feature strokes get thinner at larger scales (small ratios, e.g. 1:500,
# where there's more graphic room per real-world metre) and heavier at smaller scales (large
# ratios, e.g. 1:10000, so a line doesn't vanish once that much ground is compressed onto the
# page) - the same principle real cartographic drafting standards use, just applied continuously
# instead of in fixed scale bands.
_REFERENCE_SCALE_RATIO = 2000.0


def mm_to_pt(mm: float) -> float:
    return (mm / 25.4) * 72.0


def scaled_line_weight(base_mm: float, font_scale: float = 1.0, scale_ratio: float | None = None) -> float:
    """A feature-class pen weight (matplotlib `lw`, in points) derived from a real drafting weight
    in millimetres, adjusted for paper size (font_scale, already tied to paper_config) and for the
    plan's scale_ratio. `base_mm` should be the desired weight at the reference scale on an A4
    sheet (font_scale ~1.0) - e.g. ~0.25mm for a building outline, ~0.3mm for a road/river line.
    """
    ratio = float(scale_ratio) if scale_ratio else _REFERENCE_SCALE_RATIO
    scale_factor = max(0.70, min(1.60, ratio / _REFERENCE_SCALE_RATIO))
    mm = max(0.12, float(base_mm) * max(0.75, font_scale) * scale_factor)
    return mm_to_pt(mm)


def feature_visible_at_scale(size_m: float, scale_ratio: float | None, min_paper_mm: float = 1.4) -> bool:
    """Whether a real-world feature of the given size (its longest dimension, in metres) would
    still render at least `min_paper_mm` on the printed page at this scale - the same
    generalization principle real cartographic products use to omit clutter (a tiny shed, a
    dangling road stub, a short stream segment) that would be illegible once compressed to the
    plan's scale, rather than drawing every feature at every scale regardless of legibility.
    """
    ratio = float(scale_ratio) if scale_ratio else _REFERENCE_SCALE_RATIO
    if ratio <= 0 or size_m is None:
        return True
    paper_mm = (float(size_m) * 1000.0) / ratio
    return paper_mm >= min_paper_mm


def _real_world_extent_m(geom_wgs84, display_epsg: int) -> float | None:
    """Real-world size of a WGS84 geometry once projected: length for a line, bounding-box
    diagonal for a polygon - a practical proxy for "how big does this actually read on the page".
    """
    try:
        projected = gpd.GeoSeries([geom_wgs84], crs="EPSG:4326").to_crs(epsg=display_epsg).iloc[0]
    except Exception:
        return None
    gtype = getattr(projected, "geom_type", "")
    if gtype in ("LineString", "MultiLineString", "LinearRing"):
        return float(projected.length)
    try:
        minx, miny, maxx, maxy = projected.bounds
    except Exception:
        return None
    return math.hypot(maxx - minx, maxy - miny)


def filter_features_by_scale(geoms_wgs84, display_epsg: int, scale_ratio, min_paper_mm: float = 1.6) -> list:
    """Drops real-world-tiny features (a small shed, a short stream stub) that wouldn't render
    legibly at this plan's scale - the same generalization real cartographic products apply,
    rather than drawing every detected feature identically at every scale. Falls back to keeping a
    feature if its size can't be determined, so a projection hiccup never silently drops data.
    """
    geoms = list(geoms_wgs84 or [])
    if not geoms or not scale_ratio:
        return geoms
    kept = []
    for geom in geoms:
        size_m = _real_world_extent_m(geom, display_epsg)
        if size_m is None or feature_visible_at_scale(size_m, scale_ratio, min_paper_mm=min_paper_mm):
            kept.append(geom)
    return kept


# ======================
# Page Layout Elements
# ======================

def draw_sheet_frame(fig):
    fig.add_artist(
        patches.Rectangle((0.02, 0.02), 0.96, 0.96, transform=fig.transFigure, fill=False, lw=2)
    )
    fig.add_artist(
        patches.Rectangle((0.03, 0.03), 0.94, 0.94, transform=fig.transFigure, fill=False, lw=0.8)
    )


def draw_title_block(
    fig, title_text, plot_id, area_m2, scale_text, location_text, lga_text, state_text, font_scale=1.0,
    title_font: str | None = None, title_size: int | None = None,
    area_font: str | None = None, area_size: int | None = None,
    text_color: str = "black",
):
    # Plot number at top right corner for identification
    fig.text(0.94, 0.95, f"Plot #{plot_id}", ha="right", fontsize=int(8*font_scale), weight="bold", color=text_color)

    y = 0.955
    fig.text(
        0.5, y, str(title_text), ha="center", fontsize=title_size if title_size else int(12*font_scale), weight="bold",
        **({"fontfamily": title_font} if title_font else {}),
    )
    fig.text(0.5, y - 0.030, f"LOCATED AT: {location_text}", ha="center", fontsize=int(9*font_scale), color=text_color)
    fig.text(0.5, y - 0.050, str(lga_text), ha="center", fontsize=int(9*font_scale), color=text_color)
    fig.text(0.5, y - 0.070, str(state_text), ha="center", fontsize=int(9*font_scale), color=text_color)
    fig.text(
        0.5, y - 0.100, f"AREA = {format_area_display(area_m2)}", ha="center",
        fontsize=area_size if area_size else int(9*font_scale), color="red",
        **({"fontfamily": area_font} if area_font else {}),
    )
    fig.text(0.5, y - 0.120, f"SCALE  {scale_text}", ha="center", fontsize=int(9*font_scale), color=text_color)


def draw_footer(fig, crs_text, source_text, surveyor, rank, font_scale=1.0, text_color: str = "black"):
    y_top = 0.155
    y_bot = 0.055
    y_bot_source = 0.045
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    fig.text(0.06, y_top, f"SURVEYOR: {surveyor}", fontsize=int(9*font_scale), color=text_color)
    fig.text(0.06, y_top - 0.025, f"RANK: {rank}", fontsize=int(9*font_scale), color=text_color)
    fig.text(0.06, y_top - 0.050, "SIGNATURE: ____________________", fontsize=int(9*font_scale), color=text_color)
    fig.text(0.06, y_top - 0.075, f"DATE PRINTED: {now}", fontsize=int(9*font_scale), color=text_color)

    # Wrapped to a width that stays clear of the KEY box's left edge (x=0.35) - a long CRS
    # description drawn as one line at a fixed x/y can otherwise run far enough right to visually
    # intrude into the legend box sitting above/beside it.
    crs_fontsize = int(8*font_scale)
    crs_lines = _wrap_figure_text(fig, str(crs_text), width_fig=0.27, fontsize=crs_fontsize) or [str(crs_text)]
    crs_line_step = 0.014
    for idx, line in enumerate(crs_lines):
        fig.text(0.06, y_bot - idx * crs_line_step, line, fontsize=crs_fontsize, color="blue")

    fig.text(0.94, y_bot_source, str(source_text), fontsize=int(8*font_scale), ha="right", color=text_color)


def _draw_figure_text_justified(fig, x, y, text, width_fig, fontsize, fontweight="normal"):
    words = (text or "").split()
    if len(words) <= 1:
        fig.text(x, y, text, transform=fig.transFigure, fontsize=fontsize, va="top", ha="left", weight=fontweight)
        return

    try:
        renderer = fig.canvas.get_renderer()
    except Exception:
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()

    fp = FontProperties(size=fontsize, weight=fontweight)

    def px_width(s: str) -> float:
        try:
            w, _, _ = renderer.get_text_width_height_descent(s, fp, ismath=False)
            return float(w)
        except Exception:
            return float(len(s) * fontsize * 0.6)

    fig_w_px = max(float(fig.bbox.width), 1.0)
    target_px = max(width_fig * fig_w_px, 1.0)
    word_widths = [px_width(w) for w in words]
    base_space_px = px_width(" ")
    natural_px = sum(word_widths) + base_space_px * (len(words) - 1)

    # If the line is already wider than the target (or too close), fall back to normal text.
    if natural_px >= target_px * 0.98:
        fig.text(x, y, text, transform=fig.transFigure, fontsize=fontsize, va="top", ha="left", weight=fontweight)
        return

    gap_px = base_space_px + (target_px - natural_px) / max(1, (len(words) - 1))
    cursor_x = x
    for idx, (word, ww) in enumerate(zip(words, word_widths)):
        fig.text(
            cursor_x,
            y,
            word,
            transform=fig.transFigure,
            fontsize=fontsize,
            va="top",
            ha="left",
            weight=fontweight,
        )
        cursor_x += ww / fig_w_px
        if idx < len(words) - 1:
            cursor_x += gap_px / fig_w_px


def _wrap_figure_text(fig, text: str, width_fig: float, fontsize: float, fontweight: str = "normal", fontfamily: str | None = None):
    raw = str(text or "").strip()
    if not raw:
        return []

    words = raw.split()
    if not words:
        return []

    try:
        renderer = fig.canvas.get_renderer()
    except Exception:
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()

    if fontfamily:
        fp = FontProperties(size=fontsize, weight=fontweight, family=fontfamily)
    else:
        fp = FontProperties(size=fontsize, weight=fontweight)

    def px_width(s: str) -> float:
        try:
            w, _, _ = renderer.get_text_width_height_descent(s, fp, ismath=False)
            return float(w)
        except Exception:
            return float(len(s) * fontsize * 0.6)

    fig_w_px = max(float(fig.bbox.width), 1.0)
    max_px = max(width_fig * fig_w_px, 1.0)
    space_px = px_width(" ")

    lines = []
    current = []
    current_px = 0.0
    for word in words:
        w_px = px_width(word)
        needed = w_px if not current else (space_px + w_px)
        if current and (current_px + needed) > max_px:
            lines.append(" ".join(current))
            current = [word]
            current_px = w_px
        else:
            if current:
                current_px += needed
            else:
                current_px = w_px
            current.append(word)
    if current:
        lines.append(" ".join(current))

    return lines


def draw_certification_box(fig, certification_statement: str, surveyor_name: str, key_bounds=None, font_scale=1.0):
    """
    Draw certification text block beside the key (no surrounding box).
    """
    if key_bounds:
        key_x, key_y, key_w, key_h = key_bounds
        # Keep certification visually aligned with the key, but give it a
        # slightly larger text column and lower header baseline for readability.
        x = min(0.90, max(0.64, key_x + key_w + 0.02))
        top_y = key_y + key_h - 0.018
    else:
        x = 0.67
        top_y = 0.215

    fig.text(
        x,
        top_y,
        "CERTIFICATION",
        transform=fig.transFigure,
        fontsize=max(6, int(7 * font_scale)),
        weight="bold",
        va="top",
        ha="left",
    )

    statement = (certification_statement or "").strip() or DEFAULT_CERTIFICATION_STATEMENT
    wrap_width = max(38, int(54 / max(font_scale, 0.8)))
    wrapped_lines = textwrap.wrap(statement, width=wrap_width)
    lines = wrapped_lines[:4]
    if wrapped_lines[4:] and lines:
        lines[-1] = lines[-1][: max(0, len(lines[-1]) - 3)] + "..."

    line_step = 0.019
    start_y = top_y - 0.024

    # Prevent overlap with footer/source text when the key is short (fewer legend items).
    # This issue can look "device-specific" because different plots may have different key heights.
    min_certified_y = 0.085
    projected_certified_y = start_y - max(4, len(lines)) * line_step - 0.006
    if projected_certified_y < min_certified_y:
        lift = (min_certified_y - projected_certified_y)
        top_y += lift
        start_y += lift

    for idx, line in enumerate(lines):
        yy = start_y - idx * line_step
        fs = max(5, int(6 * font_scale))
        fig.text(
            x,
            yy,
            line,
            transform=fig.transFigure,
            fontsize=fs,
            va="top",
            ha="left",
        )

    cert_name = (surveyor_name or "").strip() or "________________"
    fig.text(
        x,
        start_y - max(4, len(lines)) * line_step - 0.006,
        f"Certified by: {cert_name}",
        transform=fig.transFigure,
        fontsize=max(5, int(6 * font_scale)),
        weight="bold",
        va="top",
        ha="left",
    )


def draw_fence_symbol(fig, x0, x1, y, color="black", lw=1.0):
    """
    Stair-step fence symbol similar to the uploaded plan sample.
    """
    span = max(x1 - x0, 0.0001)
    steps = 6
    dx = span / (steps * 2.0)
    amp = 0.006
    pts_x = [x0]
    pts_y = [y]
    cx = x0
    for _ in range(steps):
        cx += dx
        pts_x.append(cx)
        pts_y.append(pts_y[-1])
        pts_x.append(cx)
        pts_y.append(y + amp)
        cx += dx
        pts_x.append(cx)
        pts_y.append(pts_y[-1])
        pts_x.append(cx)
        pts_y.append(y)
    fig.add_artist(
        mlines.Line2D(pts_x, pts_y, transform=fig.transFigure, color=color, lw=max(0.8, lw))
    )


def draw_key_box(fig, has_buildings: bool, has_roads: bool, has_rivers: bool, has_fences: bool = False, font_scale=1.0):
    """
    KEY shows ONLY features that exist on map.
    Buildings symbol is a rectangle (not a line).
    Roads shown as double lines.
    """
    items = []
    # Plot perimeter always
    items.append(("PERIMETER (Plot)", "line", "red", 2))

    if has_buildings:
        items.append(("BUILDINGS", "rect", "black", 1))

    if has_roads:
        items.append(("ROADS", "double_line", "dimgray", 1))  # Changed to double_line

    if has_rivers:
        items.append(("RIVERS", "line", "blue", 1))

    if has_fences:
        items.append(("FENCE", "fence_line", "black", 1))

    # box sizing based on number of items
    # Keep a minimum height so certification/footer layout stays stable across
    # plots that have fewer detected legend items (prevents visual shifts).
    row_h = 0.022
    header_h = 0.028
    padding = 0.015
    min_rows = 5  # perimeter + up to 4 common feature rows
    h = header_h + max(len(items), min_rows) * row_h + padding

    w = 0.30
    x = 0.50 - w / 2.0
    # Keep the key below scale-bar labels (which sit just under the map frame).
    # We target a stable top edge and let the box height vary/fix beneath it.
    key_top_target = 0.198
    y = max(0.045, key_top_target - h)

    fig.add_artist(
        patches.Rectangle((x, y), w, h, transform=fig.transFigure, fill=False, lw=0.9*font_scale)
    )
    fig.text(x + w / 2.0, y + h - 0.020, "KEY", ha="center", fontsize=int(8*font_scale), weight="bold")

    yy = y + h - 0.050
    for lbl, sym, col, lw in items:

        if sym == "line":
            line = mlines.Line2D(
                [x + 0.03, x + 0.10],
                [yy, yy],
                transform=fig.transFigure,
                color=col,
                lw=lw*font_scale,
            )
            fig.add_artist(line)

        elif sym == "double_line":
            # Double line for roads
            offset = 0.004
            line1 = mlines.Line2D(
                [x + 0.03, x + 0.10],
                [yy + offset, yy + offset],
                transform=fig.transFigure,
                color=col,
                lw=lw*font_scale,
            )
            line2 = mlines.Line2D(
                [x + 0.03, x + 0.10],
                [yy - offset, yy - offset],
                transform=fig.transFigure,
                color=col,
                lw=lw*font_scale,
            )
            fig.add_artist(line1)
            fig.add_artist(line2)

        elif sym == "rect":
            # Building rectangle symbol
            fig.add_artist(
                patches.Rectangle(
                    (x + 0.04, yy - 0.008),
                    0.05,
                    0.016,
                    transform=fig.transFigure,
                    fill=False,
                    edgecolor=col,
                    lw=1.2,
                )
            )
            # Show simple hatch inside the building symbol.
            fig.add_artist(
                mlines.Line2D(
                    [x + 0.042, x + 0.088],
                    [yy - 0.003, yy - 0.003],
                    transform=fig.transFigure,
                    color=col,
                    lw=0.8,
                )
            )
            fig.add_artist(
                mlines.Line2D(
                    [x + 0.042, x + 0.088],
                    [yy + 0.003, yy + 0.003],
                    transform=fig.transFigure,
                    color=col,
                    lw=0.8,
                )
            )
        elif sym == "fence_line":
            draw_fence_symbol(fig, x + 0.03, x + 0.10, yy, color=col, lw=lw * font_scale)

        fig.text(x + 0.12, yy, lbl, fontsize=int(7*font_scale), va="center")
        yy -= row_h
    return (x, y, w, h)


# ======================
# Map Decorations
# ======================

def add_north_arrow(
    ax,
    font_scale=1.0,
    style: str = "one_side_stem",
    color: str = "black",
    anchor_x=None,
    anchor_y=None,
    blue_hex: str = "blue",
):
    fig = ax.figure
    col = blue_hex if str(color).lower() == "blue" else "black"
    style = str(style or "one_side_stem").strip().lower()
    # Keep north arrows vertically aligned to the right edge of the map frame
    # in both general and Adamawa templates.
    box = ax.get_position()
    x = float(box.x1) if anchor_x is None else float(anchor_x)
    y = min(0.93, float(box.y1) + 0.060) if anchor_y is None else float(anchor_y)
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

    if style in ("un_marker", "un", "un_grid", "u_n", "grid_marker"):
        # "U.N." grid-north marker used on South-South Nigeria cadastral plans (design supplied
        # as north_arrow_UN.svg): a long stem topped with a narrow pennant and an open loop,
        # labeled "U. N." On the cadastral templates this stem is anchored at (and continues
        # down through the map as) the reference vertex's Easting grid line - see the un_marker
        # anchor_x handling in _render_plot_map_layout_cadastral.
        #
        # Keep the same anchor behavior as the existing layout code, but draw the real supplied
        # SVG geometry instead of the old compressed approximation. The source file uses a
        # 420 x 1400 viewBox, a stem centered at x=210, and the "U." / "N." text baseline at
        # y=855. We therefore treat the incoming (x, y) as the stem center and text baseline.
        svg_stem_x = 210.0
        svg_text_baseline_y = 855.0
        svg_top_y = 25.0
        svg_bottom_y = 1365.0
        svg_text_size = 78.0
        # Preserve the current top clearance in templates, while keeping the original SVG aspect.
        unit = (size * 1.70) / (svg_text_baseline_y - svg_top_y)
        line_lw = max(1.0, 1.1 * font_scale)
        text_font = FontProperties(family=["Times New Roman", "Times", "DejaVu Serif"])

        def sx(px: float) -> float:
            return x + (float(px) - svg_stem_x) * unit

        def sy(py: float) -> float:
            return y - (float(py) - svg_text_baseline_y) * unit

        fig.add_artist(mlines.Line2D(
            [sx(svg_stem_x), sx(svg_stem_x)],
            [sy(svg_bottom_y), sy(svg_top_y)],
            transform=fig.transFigure,
            color=col, lw=line_lw, zorder=20, solid_capstyle="butt",
        ))
        fig.add_artist(mlines.Line2D(
            [sx(210), sx(162), sx(210)],
            [sy(25), sy(585), sy(565)],
            transform=fig.transFigure,
            color=col, lw=line_lw, zorder=21, solid_capstyle="round", solid_joinstyle="round",
        ))
        loop_vertices = [
            (sx(162), sy(585)),
            (sx(185), sy(568)),
            (sx(222), sy(562)),
            (sx(252), sy(575)),
            (sx(292), sy(592)),
            (sx(313), sy(627)),
            (sx(313), sy(665)),
            (sx(313), sy(720)),
            (sx(268), sy(765)),
            (sx(212), sy(765)),
            (sx(158), sy(765)),
            (sx(116), sy(729)),
            (sx(116), sy(680)),
        ]
        loop_codes = [
            Path.MOVETO,
            Path.CURVE4,
            Path.CURVE4,
            Path.CURVE4,
            Path.CURVE4,
            Path.CURVE4,
            Path.CURVE4,
            Path.CURVE4,
            Path.CURVE4,
            Path.CURVE4,
            Path.CURVE4,
            Path.CURVE4,
            Path.CURVE4,
        ]
        fig.add_artist(
            patches.PathPatch(
                Path(loop_vertices, loop_codes),
                transform=fig.transFigure,
                fill=False,
                edgecolor=col,
                lw=line_lw,
                zorder=21,
                capstyle="round",
                joinstyle="round",
            )
        )
        font_size = max(6, svg_text_size * unit * fig.get_figheight() * 72.0)
        fig.text(
            sx(98), sy(855), "U.",
            ha="left", va="baseline",
            fontsize=font_size, color=col, weight="normal",
            fontproperties=text_font, zorder=22,
        )
        fig.text(
            sx(228), sy(855), "N.",
            ha="left", va="baseline",
            fontsize=font_size, color=col, weight="normal",
            fontproperties=text_font, zorder=22,
        )
        return {
            "stem_x": sx(svg_stem_x),
            "stem_top": sy(svg_top_y),
            "stem_bottom": sy(svg_bottom_y),
            "text_baseline_y": sy(svg_text_baseline_y),
        }

    if style in ("nn_arrow", "split_triangle", "nn"):
        # Exact "N.N." survey north arrow based on the supplied north_arrow_NN.svg asset.
        # The caller-provided anchor remains the bottom of the stem so FCT placement is predictable.
        svg_stem_bottom_y = 405.0
        svg_apex_y = 24.0
        svg_base_y = 168.0
        svg_tick_y = 285.5
        svg_text_baseline_y = 285.0
        svg_left_x = 8.0
        svg_right_x = 59.0
        svg_stem_x = 33.5
        svg_font_size = 20.0
        k = (size * 1.55) / (svg_base_y - svg_apex_y)
        line_lw = max(1.0, 1.0 * font_scale)
        text_font = FontProperties(family=["Arial", "Helvetica", "DejaVu Sans"])

        def sx2(px: float) -> float:
            return x + (float(px) - svg_stem_x) * k

        def sy2(py: float) -> float:
            return y + (svg_stem_bottom_y - float(py)) * k

        fig.add_artist(mlines.Line2D(
            [sx2(svg_stem_x), sx2(svg_stem_x)],
            [sy2(svg_stem_bottom_y), sy2(svg_base_y)],
            transform=fig.transFigure,
            color=col,
            lw=line_lw,
            zorder=20,
            solid_capstyle="butt",
        ))
        fig.add_artist(patches.Polygon(
            [(sx2(34), sy2(24)), (sx2(8), sy2(168)), (sx2(34), sy2(168))],
            closed=True,
            facecolor="white",
            edgecolor=col,
            lw=line_lw,
            transform=fig.transFigure,
            zorder=21,
            joinstyle="miter",
        ))
        fig.add_artist(patches.Polygon(
            [(sx2(34), sy2(24)), (sx2(59), sy2(168)), (sx2(34), sy2(168))],
            closed=True,
            facecolor=col,
            edgecolor=col,
            lw=line_lw,
            transform=fig.transFigure,
            zorder=21,
            joinstyle="miter",
        ))
        fig.add_artist(mlines.Line2D(
            [sx2(svg_left_x), sx2(58)],
            [sy2(svg_tick_y), sy2(svg_tick_y)],
            transform=fig.transFigure,
            color=col,
            lw=line_lw * 0.85,
            zorder=22,
        ))
        font_size = max(6, svg_font_size * k * fig.get_figheight() * 72.0)
        fig.text(
            sx2(9),
            sy2(svg_text_baseline_y),
            "N",
            ha="left",
            va="baseline",
            fontsize=font_size,
            color=col,
            weight="bold",
            fontproperties=text_font,
            zorder=23,
        )
        fig.text(
            sx2(42),
            sy2(svg_text_baseline_y),
            "N",
            ha="left",
            va="baseline",
            fontsize=font_size,
            color=col,
            weight="bold",
            fontproperties=text_font,
            zorder=23,
        )
        return {
            "stem_x": sx2(svg_stem_x),
            "stem_top": sy2(svg_apex_y),
            "stem_bottom": sy2(svg_stem_bottom_y),
            "text_baseline_y": sy2(svg_text_baseline_y),
        }

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


def add_scalebar(ax, length_m: float, segments: int = 4, font_scale=1.0):
    # Keep your current fixed position in axes coordinates
    x0, y0, bar_h, total_w = 0.225, -0.15, 0.012, 0.55
    seg_w = total_w / float(segments)

    for i in range(segments):
        xi = x0 + i * seg_w
        face = "black" if i % 2 == 0 else "white"
        ax.add_patch(
            patches.Rectangle(
                (xi, y0),
                seg_w,
                bar_h,
                transform=ax.transAxes,
                facecolor=face,
                edgecolor="black",
                linewidth=0.8*font_scale,
                clip_on=False,
            )
        )

    ax.add_patch(
        patches.Rectangle(
            (x0, y0),
            total_w,
            bar_h,
            transform=ax.transAxes,
            fill=False,
            edgecolor="black",
            linewidth=1.2*font_scale,
            clip_on=False,
        )
    )

    ax.text(
        x0,
        y0 - 0.03,
        "0",
        transform=ax.transAxes,
        ha="center",
        va="top",
        fontsize=int(7*font_scale),
        clip_on=False,
    )
    for i in range(1, segments + 1):
        value = int(round((length_m / segments) * i))
        ax.text(
            x0 + i * seg_w,
            y0 - 0.03,
            f"{value}",
            transform=ax.transAxes,
            ha="center",
            va="top",
            fontsize=int(7*font_scale),
            clip_on=False,
        )

    ax.text(
        x0 + total_w / 2.0,
        y0 + 0.025,
        "meters",
        transform=ax.transAxes,
        ha="center",
        fontsize=int(7*font_scale),
        clip_on=False,
    )


# ======================
# Grid & Annotations
# ======================

def draw_grid(
    ax,
    plot_poly,
    minor: float,
    major: float,
    font_scale=1.0,
    full_grid: bool = False,
    edge_ticks: bool = True,
    color: str = "blue",
    edge_tick_style: str = "multi",
):
    xmin, xmax = ax.get_xlim()
    ymin, ymax = ax.get_ylim()

    # Optional interior grid lines (used by Adamawa template).
    if full_grid:
        # Use axes-fraction lines to guarantee visible interior grid regardless of
        # map extent scaling and export downsampling.
        x_divisions = 8
        y_divisions = 8
        for i in range(1, x_divisions):
            xf = i / x_divisions
            ax.add_line(
                mlines.Line2D(
                    [xf, xf],
                    [0.0, 1.0],
                    transform=ax.transAxes,
                    color=color,
                    lw=0.52 * font_scale,
                    alpha=0.62,
                    zorder=1,
                )
            )

        for j in range(1, y_divisions):
            yf = j / y_divisions
            ax.add_line(
                mlines.Line2D(
                    [0.0, 1.0],
                    [yf, yf],
                    transform=ax.transAxes,
                    color=color,
                    lw=0.52 * font_scale,
                    alpha=0.62,
                    zorder=1,
                )
            )

    if edge_ticks and edge_tick_style == "corners":
        # Akwa Ibom / Rivers / Cross River cadastral style: only 4 survey reference crosses, one
        # at each map-frame corner. No repeated side ticks or full grid lines across the map.
        cross_span_x = 0.018
        cross_span_y = 0.018
        corner_specs = (
            (0.0, 0.0),
            (1.0, 0.0),
            (0.0, 1.0),
            (1.0, 1.0),
        )
        for xf, yf in corner_specs:
            ax.add_line(
                mlines.Line2D(
                    [xf - cross_span_x, xf + cross_span_x],
                    [yf, yf],
                    transform=ax.transAxes,
                    color=color,
                    lw=0.95 * font_scale,
                    alpha=0.95,
                    clip_on=False,
                    zorder=6,
                )
            )
            ax.add_line(
                mlines.Line2D(
                    [xf, xf],
                    [yf - cross_span_y, yf + cross_span_y],
                    transform=ax.transAxes,
                    color=color,
                    lw=0.95 * font_scale,
                    alpha=0.95,
                    clip_on=False,
                    zorder=6,
                )
            )
    elif edge_ticks:
        # Original general-template style: a short inward tick at every major grid crossing along
        # all 4 sides.
        tick_len = (xmax - xmin) * 0.01
        xs = np.arange(math.floor(xmin / major) * major, xmax + 0.1, major)
        ys = np.arange(math.floor(ymin / major) * major, ymax + 0.1, major)

        for x in xs:
            if x < xmin or x > xmax:
                continue
            ax.plot([x, x], [ymax, ymax - tick_len], color=color, lw=0.6*font_scale, alpha=0.5)
            ax.plot([x, x], [ymin, ymin + tick_len], color=color, lw=0.6*font_scale, alpha=0.5)

        for y in ys:
            if y < ymin or y > ymax:
                continue
            ax.plot([xmin, xmin + tick_len], [y, y], color=color, lw=0.6*font_scale, alpha=0.5)
            ax.plot([xmax, xmax - tick_len], [y, y], color=color, lw=0.6*font_scale, alpha=0.5)


def draw_coordinate_frame(
    ax, spacing: float, font_scale=1.0, first_point_info=None, color: str = "blue",
    grid_font: str | None = None, grid_size: int | None = None,
):
    """
    Draw coordinate frame with grid labels.
    first_point_info: tuple (station_name, easting, northing) to display below the grid
    """
    text_kwargs = {"fontfamily": grid_font} if grid_font else {}
    label_fontsize = grid_size if grid_size else int(7*font_scale)
    xmin, xmax = ax.get_xlim()
    ymin, ymax = ax.get_ylim()
    pad = (xmax - xmin) * 0.035

    ax.add_patch(
        patches.Rectangle(
            (xmin - pad, ymin - pad),
            (xmax - xmin) + 2 * pad,
            (ymax - ymin) + 2 * pad,
            fill=False,
            lw=1.5*font_scale,
            clip_on=False,
        )
    )
    ax.add_patch(
        patches.Rectangle(
            (xmin, ymin),
            (xmax - xmin),
            (ymax - ymin),
            fill=False,
            lw=1.0*font_scale,
            clip_on=False,
        )
    )

    xs = np.arange(math.floor(xmin / spacing) * spacing, xmax + 0.1, spacing)
    ys = np.arange(math.floor(ymin / spacing) * spacing, ymax + 0.1, spacing)

    # Draw easting labels at the top - filter to only show labels within bounds
    for x in xs:
        if x >= xmin and x <= xmax:
            ax.text(x, ymax + pad * 0.45, f"{int(round(x))}", ha="center", fontsize=label_fontsize, color=color, **text_kwargs)

    # Draw northing labels on both sides - include ALL grid lines including the first one
    for y in ys:
        if y >= ymin and y <= ymax:
            ax.text(
                xmin - pad * 0.45,
                y,
                f"{int(round(y))}",
                va="center",
                ha="right",
                fontsize=label_fontsize,
                color=color,
                rotation=90,
                **text_kwargs,
            )
            ax.text(
                xmax + pad * 0.45,
                y,
                f"{int(round(y))}",
                va="center",
                ha="left",
                fontsize=label_fontsize,
                color=color,
                rotation=90,
                **text_kwargs,
            )

    # Add first point coordinates text to the LEFT of the grid frame (to avoid scale bar overlap)
    if first_point_info:
        station_name, easting, northing = first_point_info
        coord_text = f"{station_name}: {easting:.2f}E, {northing:.2f}N"
        ax.text(
            xmin,
            ymin - pad * 2.0,
            coord_text,
            ha="left",
            va="top",
            fontsize=grid_size if grid_size else int(8*font_scale),
            color=color,
            weight="normal",
            clip_on=False,
            **text_kwargs,
        )


def annotate_vertices(
    ax,
    poly,
    plot_id: int,
    station_names=None,
    font_scale=1.0,
    first_point_coords=None,
    min_label_length_m: float = 0.0,
    avoid_geom=None,
    scale_ratio: int = 1000,
    boundary_poly=None,
    beacon_style: str = "cross",
    show_station_names: bool = True,
    show_beacons: bool = True,
    text_color: str = "black",
    boundary_color: str = "red",
    station_font: str | None = None,
    station_size: int | None = None,
    bearing_font: str | None = None,
    bearing_size: int | None = None,
):
    """
    Annotate vertices with station names and bearing/distance in RED.
    Applies simple collision-aware placement for tight turns.
    """
    coords, labels = _clockwise_ring_coords_and_labels(poly, station_names=station_names)

    placed_boxes = []
    x0, x1 = ax.get_xlim()
    y0, y1 = ax.get_ylim()
    span_x = max(abs(x1 - x0), 1.0)
    span_y = max(abs(y1 - y0), 1.0)

    def intersects(a, b):
        return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])

    def collides(box):
        return any(intersects(box, other) for other in placed_boxes)

    def estimate_box(x, y, text_value, scale_w, scale_h):
        lines = [line for line in str(text_value).splitlines() if line is not None]
        if not lines:
            lines = [str(text_value)]
        max_len = max(len(line) for line in lines) if lines else 1
        line_count = max(1, len(lines))
        w = span_x * scale_w * max(1.0, max_len / 8.0)
        h = span_y * scale_h * (1.0 + 0.55 * (line_count - 1))
        return (x - w / 2.0, y - h / 2.0, x + w / 2.0, y + h / 2.0)

    def place_text(
        x,
        y,
        text,
        font_size,
        color,
        rotation=0,
        weight="bold",
        scale_w=0.015,
        scale_h=0.02,
        normal=None,
        normal_offset_mult: float = 1.0,
        line_spacing: float = 0.95,
        allow_center: bool = True,
        font_family=None,
    ):
        offset_m = max(2.0, (6.0 / 1000.0) * scale_ratio) * max(0.6, normal_offset_mult)
        candidates = [(x, y)]
        if normal is not None:
            nx, ny = normal
            if boundary_poly is not None:
                test_pt = Point(x + nx * offset_m, y + ny * offset_m)
                if boundary_poly.contains(test_pt):
                    nx, ny = -nx, -ny
            candidates = [
                (x + nx * offset_m, y + ny * offset_m),
                (x - nx * offset_m, y - ny * offset_m),
                (x + nx * offset_m * 1.5, y + ny * offset_m * 1.5),
                (x - nx * offset_m * 1.5, y - ny * offset_m * 1.5),
            ]
            if allow_center:
                candidates.append((x, y))
            if avoid_geom is not None:
                candidates = [c for c in candidates if not avoid_geom.contains(Point(c[0], c[1]))]
                if not candidates:
                    for k in range(2, 7):
                        cand_pos = (x + nx * offset_m * k, y + ny * offset_m * k)
                        cand_neg = (x - nx * offset_m * k, y - ny * offset_m * k)
                        if not avoid_geom.contains(Point(cand_pos[0], cand_pos[1])):
                            candidates.append(cand_pos)
                        if not avoid_geom.contains(Point(cand_neg[0], cand_neg[1])):
                            candidates.append(cand_neg)
                    if not candidates:
                        if allow_center:
                            candidates = [(x, y)]
                        else:
                            candidates = [(x - nx * offset_m * 2.0, y - ny * offset_m * 2.0)]
        elif avoid_geom is not None:
            candidates = [c for c in candidates if not avoid_geom.contains(Point(c[0], c[1]))] or candidates
        offsets = [
            (0, 0),
            (span_x * 0.01, 0),
            (-span_x * 0.01, 0),
            (0, span_y * 0.01),
            (0, -span_y * 0.01),
            (span_x * 0.01, span_y * 0.01),
            (-span_x * 0.01, span_y * 0.01),
            (span_x * 0.01, -span_y * 0.01),
            (-span_x * 0.01, -span_y * 0.01),
        ]
        for dx, dy in offsets:
            bx = estimate_box(x + dx, y + dy, text, scale_w, scale_h)
            if not collides(bx):
                ax.text(
                    x + dx,
                    y + dy,
                    text,
                    fontsize=font_size,
                    color=color,
                    ha="center",
                    va="center",
                    rotation=rotation,
                    weight=weight,
                    multialignment="center",
                    linespacing=line_spacing,
                    zorder=25,
                    **({"fontfamily": font_family} if font_family else {}),
                )
                placed_boxes.append(bx)
                return True
        return False

    skipped = []
    def draw_beacon(px, py):
        size_m = max(1.0, (3.0 / 1000.0) * scale_ratio)
        style = (beacon_style or "circle").lower()
        if style == "square":
            ax.add_patch(
                patches.Rectangle(
                    (px - size_m / 2.0, py - size_m / 2.0),
                    size_m,
                    size_m,
                    fill=False,
                    edgecolor="black",
                    lw=1.0 * font_scale,
                    zorder=24,
                )
            )
        elif style == "triangle":
            ax.add_patch(
                patches.Polygon(
                    [(px, py + size_m / 2.0), (px - size_m / 2.0, py - size_m / 2.0), (px + size_m / 2.0, py - size_m / 2.0)],
                    closed=True,
                    fill=False,
                    edgecolor="black",
                    lw=1.0 * font_scale,
                    zorder=24,
                )
            )
        elif style == "diamond":
            ax.add_patch(
                patches.Polygon(
                    [(px, py + size_m / 2.0), (px - size_m / 2.0, py), (px, py - size_m / 2.0), (px + size_m / 2.0, py)],
                    closed=True,
                    fill=False,
                    edgecolor="black",
                    lw=1.0 * font_scale,
                    zorder=24,
                )
            )
        elif style == "cross":
            ax.add_line(mlines.Line2D([px - size_m / 2.0, px + size_m / 2.0], [py, py], color="black", lw=1.0 * font_scale, zorder=24))
            ax.add_line(mlines.Line2D([px, px], [py - size_m / 2.0, py + size_m / 2.0], color="black", lw=1.0 * font_scale, zorder=24))
        else:
            ax.add_patch(
                patches.Circle(
                    (px, py),
                    radius=size_m / 2.0,
                    fill=False,
                    edgecolor="black",
                    lw=1.0 * font_scale,
                    zorder=24,
                )
            )

    for i in range(len(coords) - 1):
        p1, p2 = Point(coords[i]), Point(coords[i + 1])
        raw_label = labels[i % len(labels)]
        raw_next_label = labels[(i + 1) % len(labels)]
        label = format_station_label(raw_label)
        next_label = format_station_label(raw_next_label)

        seg_dx = p2.x - p1.x
        seg_dy = p2.y - p1.y
        seg_len = math.hypot(seg_dx, seg_dy) or 1.0
        normal = (-seg_dy / seg_len, seg_dx / seg_len)

        if show_beacons:
            draw_beacon(p1.x, p1.y)
        if show_station_names:
            station_offset = max(2.0, (5.0 / 1000.0) * scale_ratio)
            nx, ny = normal
            if boundary_poly is not None:
                test_pt = Point(p1.x + nx * station_offset, p1.y + ny * station_offset)
                if boundary_poly.contains(test_pt):
                    nx, ny = -nx, -ny
            place_text(
                p1.x + nx * station_offset,
                p1.y + ny * station_offset,
                label,
                font_size=station_size if station_size else int(8 * font_scale),
                color=text_color,
                rotation=0,
                weight="normal",
                scale_w=0.010,
                scale_h=0.016,
                normal=None,
                font_family=station_font,
            )

        bearing, dist = calculate_bearing_deg(p1, p2), p1.distance(p2)
        if min_label_length_m and dist < min_label_length_m:
            skipped.append({
                "from": label,
                "to": next_label,
                "bearing": bearing,
                "distance": dist,
            })
            continue

        mx, my = (p1.x + p2.x) / 2.0, (p1.y + p2.y) / 2.0
        ang = math.degrees(math.atan2(p2.y - p1.y, p2.x - p1.x))
        if ang < -90 or ang > 90:
            ang += 180

        label_nx, label_ny = normal
        label_offset_mult = 1.0
        # Keep bearing/distance vertical spacing consistent on every boundary edge.
        label_line_spacing = 2.2
        fence_edge = False
        base_offset = max(2.0, (6.0 / 1000.0) * scale_ratio)
        if boundary_poly is not None:
            test_pt = Point(mx + label_nx * base_offset, my + label_ny * base_offset)
            if boundary_poly.contains(test_pt):
                label_nx, label_ny = -label_nx, -label_ny
        if avoid_geom is not None:
            try:
                check_buf = avoid_geom.buffer(max(0.2, base_offset * 0.15))
                plus_pt = Point(mx + label_nx * base_offset, my + label_ny * base_offset)
                minus_pt = Point(mx - label_nx * base_offset, my - label_ny * base_offset)
                plus_blocked = check_buf.contains(plus_pt)
                minus_blocked = check_buf.contains(minus_pt)
                if plus_blocked and not minus_blocked:
                    label_nx, label_ny = -label_nx, -label_ny
            except Exception:
                pass
        # If this edge is fenced, push the bearing/distance text farther away
        # so the red annotation clears the fence teeth.
        if avoid_geom is not None:
            try:
                seg = LineString([(p1.x, p1.y), (p2.x, p2.y)])
                fence_touch_tol = max(0.5, (2.5 / 1000.0) * scale_ratio)
                if seg.buffer(fence_touch_tol).intersects(avoid_geom):
                    fence_edge = True
                    # Keep a subtle extra separation only on fenced edges.
                    label_offset_mult = 1.15
            except Exception:
                pass
        if fence_edge and avoid_geom is not None:
            try:
                plus_pt = Point(mx + label_nx * base_offset * label_offset_mult, my + label_ny * base_offset * label_offset_mult)
                minus_pt = Point(mx - label_nx * base_offset * label_offset_mult, my - label_ny * base_offset * label_offset_mult)
                plus_d = avoid_geom.distance(plus_pt)
                minus_d = avoid_geom.distance(minus_pt)
                if minus_d > plus_d:
                    label_nx, label_ny = -label_nx, -label_ny
            except Exception:
                pass

        # The text block is vertically centered on an anchor pushed only slightly outside the
        # polygon (base_offset), which is smaller than the gap between the two stacked lines - so
        # the first line ends up further from the edge (outside) and the second line crosses back
        # to the inside. Which line that first slot actually is depends on both the readability
        # flip on `ang` (keeps text right-side-up) and the interior-avoidance flips on
        # (label_nx, label_ny) above - two independent flips, so a fixed line order isn't reliably
        # outside/inside the same way on every edge. Instead, work out which physical direction
        # the first line's slot actually lands in on THIS edge, and order distance/bearing so
        # distance always lands inside and bearing always lands outside.
        first_line_dir = (
            -math.sin(math.radians(ang)),
            math.cos(math.radians(ang)),
        )
        first_line_is_outside = (first_line_dir[0] * label_nx + first_line_dir[1] * label_ny) > 0
        bearing_line = format_bearing_dms(bearing)
        distance_line = f"{dist:.2f}m"
        label_text = f"{bearing_line}\n{distance_line}" if first_line_is_outside else f"{distance_line}\n{bearing_line}"

        place_text(
            mx,
            my,
            label_text,
            font_size=bearing_size if bearing_size else int(7.0 * font_scale),
            color=boundary_color,
            rotation=ang,
            weight="normal",
            scale_w=0.02,
            scale_h=0.025,
            normal=(label_nx, label_ny),
            normal_offset_mult=label_offset_mult,
            line_spacing=label_line_spacing,
            allow_center=not fence_edge,
            font_family=bearing_font,
        )

    return skipped, placed_boxes


def draw_skipped_table(ax, entries, font_scale=1.0, poly=None):
    if not entries:
        return

    max_rows = 8
    entries = entries[:max_rows]

    header = ["From", "To", "Bearing", "Dist (m)"]
    cell_text = [
        [
            e["from"],
            e["to"],
            format_bearing_dms(e["bearing"]),
            f"{e['distance']:.2f}",
        ]
        for e in entries
    ]

    table_w, table_h = 0.36, 0.18
    # A fixed corner can land right on top of the boundary/points depending on the plot's shape
    # and orientation - so pick whichever of the 4 axes corners has the least (ideally zero)
    # overlap with the plot's own footprint, rather than always defaulting to bottom-right.
    candidates = [
        (0.62, 0.02), (0.02, 0.02), (0.62, 0.80), (0.02, 0.80),
    ]
    bbox_xy = candidates[0]
    if poly is not None:
        try:
            xmin, xmax = ax.get_xlim()
            ymin, ymax = ax.get_ylim()
            x_span = max(xmax - xmin, 1e-9)
            y_span = max(ymax - ymin, 1e-9)
            poly_minx, poly_miny, poly_maxx, poly_maxy = poly.bounds
            frac_minx = (poly_minx - xmin) / x_span
            frac_maxx = (poly_maxx - xmin) / x_span
            frac_miny = (poly_miny - ymin) / y_span
            frac_maxy = (poly_maxy - ymin) / y_span
            margin = 0.02

            def overlap_area(cx, cy):
                ox0, ox1 = max(cx, frac_minx - margin), min(cx + table_w, frac_maxx + margin)
                oy0, oy1 = max(cy, frac_miny - margin), min(cy + table_h, frac_maxy + margin)
                return max(0.0, ox1 - ox0) * max(0.0, oy1 - oy0)

            bbox_xy = min(candidates, key=lambda c: overlap_area(*c))
        except Exception:
            bbox_xy = candidates[0]

    table = ax.table(
        cellText=cell_text,
        colLabels=header,
        cellLoc="center",
        colLoc="center",
        bbox=[bbox_xy[0], bbox_xy[1], table_w, table_h],
    )
    table.auto_set_font_size(False)
    table.set_fontsize(max(5, int(6 * font_scale)))
    for (row, col), cell in table.get_celld().items():
        if row == 0:
            cell.set_text_props(weight="bold")
        cell.set_linewidth(0.5)


def _iter_line_geometries(geom):
    if geom is None or geom.is_empty:
        return
    gtype = getattr(geom, "geom_type", "")
    if gtype in ("LineString", "LinearRing"):
        yield LineString(list(geom.coords))
        return
    if gtype == "Polygon":
        yield geom.exterior
        for ring in geom.interiors:
            yield ring
        return
    if hasattr(geom, "geoms"):
        for part in geom.geoms:
            yield from _iter_line_geometries(part)


def _iter_polygons(geom):
    if geom is None or getattr(geom, "is_empty", False):
        return
    gtype = getattr(geom, "geom_type", "")
    if gtype == "Polygon":
        yield geom
        return
    if hasattr(geom, "geoms"):
        for part in geom.geoms:
            yield from _iter_polygons(part)


def _hatch_pass(ax, poly, minx, miny, maxx, maxy, spacing, direction, color, font_scale, hatch_lw=None):
    """Draw one set of parallel scan lines (horizontal/vertical/diagonal) clipped to poly."""
    if direction == "vertical":
        x = minx + spacing
        while x < maxx:
            scan = LineString([(x, miny - spacing), (x, maxy + spacing)])
            clipped = poly.intersection(scan)
            for segment in _iter_line_geometries(clipped):
                try:
                    x_vals, y_vals = segment.xy
                    ax.plot(x_vals, y_vals, color=color, lw=hatch_lw if hatch_lw is not None else 0.7 * font_scale, zorder=7.5)
                except Exception:
                    continue
            x += spacing
    elif direction == "diagonal":
        # 45-degree lines of the form y = x + c; perpendicular spacing between
        # adjacent lines is spacing, so c must step by spacing * sqrt(2).
        pad = max(maxx - minx, maxy - miny) + spacing
        c_min = (miny - pad) - (maxx + pad)
        c_max = (maxy + pad) - (minx - pad)
        step = spacing * math.sqrt(2)
        c = c_min
        while c < c_max:
            t0, t1 = minx - pad, maxx + pad
            scan = LineString([(t0, t0 + c), (t1, t1 + c)])
            clipped = poly.intersection(scan)
            for segment in _iter_line_geometries(clipped):
                try:
                    x_vals, y_vals = segment.xy
                    ax.plot(x_vals, y_vals, color=color, lw=hatch_lw if hatch_lw is not None else 0.7 * font_scale, zorder=7.5)
                except Exception:
                    continue
            c += step
    else:  # horizontal (default)
        y = miny + spacing
        while y < maxy:
            scan = LineString([(minx - spacing, y), (maxx + spacing, y)])
            clipped = poly.intersection(scan)
            for segment in _iter_line_geometries(clipped):
                try:
                    x_vals, y_vals = segment.xy
                    ax.plot(x_vals, y_vals, color=color, lw=hatch_lw if hatch_lw is not None else 0.7 * font_scale, zorder=7.5)
                except Exception:
                    continue
            y += spacing


def draw_building_hatch(
    ax,
    building_geoms,
    display_epsg: int,
    scale_ratio: int,
    font_scale=1.0,
    color: str = "black",
    hatch_type: str = "diagonal",
):
    """
    Draw sparse hatch lines clipped to building polygons, similar to common
    survey-plan building symbols. `hatch_type` is one of "horizontal" (default),
    "vertical", "diagonal", or "cross" (horizontal + vertical passes combined).
    """
    if not building_geoms:
        return
    normalized_hatch_type = str(hatch_type or "diagonal").strip().lower()
    if normalized_hatch_type not in ("horizontal", "vertical", "diagonal", "cross"):
        normalized_hatch_type = "diagonal"
    directions = ["horizontal", "vertical"] if normalized_hatch_type == "cross" else [normalized_hatch_type]
    # Hatch spacing in map units derived from paper mm and scale ratio.
    # Example: 3.5 mm on paper => 3.5m at 1:1000, 7m at 1:2000.
    hatch_spacing_mm = 3.5
    base_spacing = max(0.6, (hatch_spacing_mm / 1000.0) * max(scale_ratio, 100))
    hatch_lw = scaled_line_weight(0.15, font_scale, scale_ratio)
    for geom in building_geoms:
        try:
            projected = gpd.GeoSeries([geom], crs="EPSG:4326").to_crs(epsg=display_epsg).iloc[0]
        except Exception:
            continue
        for poly in _iter_polygons(projected):
            try:
                minx, miny, maxx, maxy = poly.bounds
                if maxy <= miny:
                    continue
                height = maxy - miny
                # Keep spacing scale-driven, but shrink for very small buildings so hatch is still visible.
                spacing = min(base_spacing, max(0.4, height / 4.0))
                for direction in directions:
                    _hatch_pass(ax, poly, minx, miny, maxx, maxy, spacing, direction, color, font_scale, hatch_lw=hatch_lw)
            except Exception:
                continue


def _draw_fence_line(ax, line_geom, scale_ratio: int, font_scale=1.0):
    if line_geom is None or line_geom.is_empty:
        return
    try:
        x_vals, y_vals = line_geom.xy
    except Exception:
        return

    length = getattr(line_geom, "length", 0.0) or 0.0
    if length <= 0:
        ax.plot(x_vals, y_vals, color="black", lw=0.65 * font_scale, zorder=7)
        return

    # Compact fence symbol so it fits cleanly between bearing and distance text.
    step_span = max(1.8, (6.2 / 1000.0) * max(scale_ratio, 100))
    tooth_amp = max(0.7, (1.6 / 1000.0) * max(scale_ratio, 100))
    probe = max(0.4, step_span * 0.2)
    if length < step_span * 1.2:
        ax.plot(x_vals, y_vals, color="black", lw=0.65 * font_scale, zorder=7)
        return

    points_x = []
    points_y = []
    d = 0.0
    while d < length:
        d1 = min(length, d + step_span * 0.35)
        d2 = min(length, d + step_span * 0.70)
        try:
            p0 = line_geom.interpolate(d)
            p1 = line_geom.interpolate(d1)
            p2 = line_geom.interpolate(d2)
            p_prev = line_geom.interpolate(max(0.0, d1 - probe))
            p_next = line_geom.interpolate(min(length, d1 + probe))
        except Exception:
            d += step_span
            continue

        tx = p_next.x - p_prev.x
        ty = p_next.y - p_prev.y
        mag = math.hypot(tx, ty)
        if mag == 0:
            d += step_span
            continue
        tx /= mag
        ty /= mag
        nx = -ty
        ny = tx
        side = 1.0

        # Build a small rectangular "tooth" segment (stair-step look).
        b0x, b0y = p0.x, p0.y
        b1x, b1y = p1.x, p1.y
        t1x, t1y = b1x + side * nx * tooth_amp, b1y + side * ny * tooth_amp
        t2x, t2y = p2.x + side * nx * tooth_amp, p2.y + side * ny * tooth_amp
        b2x, b2y = p2.x, p2.y

        if not points_x:
            points_x.append(b0x)
            points_y.append(b0y)
        else:
            points_x.append(b0x)
            points_y.append(b0y)

        points_x.extend([b1x, t1x, t2x, b2x])
        points_y.extend([b1y, t1y, t2y, b2y])

        d += step_span

    if len(points_x) < 2:
        ax.plot(x_vals, y_vals, color="black", lw=0.8 * font_scale, zorder=7)
        return

    # Ensure the jagged fence reaches the line end.
    try:
        pend = line_geom.interpolate(length)
        points_x.append(pend.x)
        points_y.append(pend.y)
    except Exception:
        pass

    ax.plot(points_x, points_y, color="black", lw=0.65 * font_scale, zorder=7)


def draw_fences(ax, fence_geoms, display_epsg: int, scale_ratio: int, font_scale=1.0):
    if not fence_geoms:
        return
    for geom in fence_geoms:
        try:
            projected = gpd.GeoSeries([geom], crs="EPSG:4326").to_crs(epsg=display_epsg).iloc[0]
        except Exception:
            continue
        for line_part in _iter_line_geometries(projected):
            _draw_fence_line(ax, line_part, scale_ratio=scale_ratio, font_scale=font_scale)


def build_fence_avoid_geom(fence_geoms, display_epsg: int, scale_ratio: int):
    """
    Build a buffered projected fence geometry used to offset bearing/distance labels
    when they sit on fenced boundary edges.
    """
    if not fence_geoms:
        return None
    # One-sided buffer: match fence teeth side used in _draw_fence_line (side=+1).
    buffer_m = max(1.0, (2.2 / 1000.0) * max(scale_ratio, 100))
    buffered_parts = []
    for geom in fence_geoms:
        try:
            projected = gpd.GeoSeries([geom], crs="EPSG:4326").to_crs(epsg=display_epsg).iloc[0]
        except Exception:
            continue
        for line_part in _iter_line_geometries(projected):
            try:
                tooth_side = line_part.buffer(buffer_m, cap_style=2, join_style=2, single_sided=True)
                if tooth_side is not None and not tooth_side.is_empty:
                    buffered_parts.append(tooth_side)
            except Exception:
                continue
    if not buffered_parts:
        return None
    merged = buffered_parts[0]
    for part in buffered_parts[1:]:
        try:
            merged = merged.union(part)
        except Exception:
            continue
    return merged


def _feature_override_replaces_native(candidate_geom, override_geom, feature_type: str, tol_deg: float = 0.00001) -> bool:
    """Whether an override truly replaces an existing feature in WGS84 geometry space.

    This is intentionally stricter than a plain ``intersects`` test. Roads, rivers, and fences
    frequently touch or cross at junctions, and a name-only override must not erase neighboring
    segments that merely share one point. Coverage is measured against the candidate feature's own
    size, so only a near-total overlap counts as a replacement.
    """
    normalized = str(feature_type or "").strip().lower()
    try:
        buffered = override_geom.buffer(tol_deg)
        if normalized in ("road", "river", "fence"):
            total = max(getattr(candidate_geom, "length", 0.0), 1e-9)
            uncovered = candidate_geom.difference(buffered)
            return getattr(uncovered, "length", 0.0) < total * 0.1
        if normalized in ("building",):
            total = max(getattr(candidate_geom, "area", 0.0), 1e-12)
            uncovered = candidate_geom.difference(buffered)
            return getattr(uncovered, "area", 0.0) < total * 0.1
        return candidate_geom.distance(override_geom) < tol_deg
    except Exception:
        return candidate_geom.intersects(override_geom)


def _resolve_override_names(overrides, feature_type: str):
    """Returns [(geom, name)] for named `feature_type` overrides (roads/rivers), resolved with
    the same last-override-wins-by-intersecting-geometry semantics apply_overrides already uses
    for the geometry itself: each override in turn drops any earlier entry whose geometry it
    intersects before adding its own. Without this, a later override that clears or renames a
    road (e.g. from the Road Names panel) would leave the earlier, now-stale name still drawn,
    since a naive "collect every override with a name" pass has no notion of supersession.
    """
    resolved: list = []
    for ov in overrides:
        if ov.get("feature_type") != feature_type:
            continue
        geom = ov.get("geom")
        action = ov.get("action")
        if geom is None or action not in ("add", "update", "delete"):
            continue
        resolved = [
            (g, n)
            for (g, n) in resolved
            if not _feature_override_replaces_native(g, geom, feature_type)
        ]
        if action in ("add", "update"):
            resolved.append((geom, str(ov.get("name") or "").strip()))
    return [(g, n) for (g, n) in resolved if n]


def _road_segment_replaced(geom_wgs84, override_geoms_wgs84, display_epsg: int, tol_m: float = 0.75) -> bool:
    """Whether `geom_wgs84` (a base road row) is one of the segments a "delete"/"update" override
    actually replaces, rather than just a different, neighboring road that happens to touch or
    cross it at a junction. A plain `.intersects()` test can't tell those apart - two distinct
    roads meeting at a junction legitimately intersect at that single point, so using intersects
    alone would wrongly drop the *other* road too whenever a named road happened to join one.
    Instead this checks how much of the row's own length lies within a small buffer of the
    override geometry: a segment the override actually replaces is buffer-covered almost along its
    whole length, while a merely-crossing road only has a single point (~0 length) inside it.
    """
    if not override_geoms_wgs84:
        return False
    try:
        row_proj = gpd.GeoSeries([geom_wgs84], crs="EPSG:4326").to_crs(epsg=display_epsg).iloc[0]
        total_len = max(row_proj.length, 1e-6)
    except Exception:
        return any(geom_wgs84.intersects(dg) for dg in override_geoms_wgs84)
    for dg in override_geoms_wgs84:
        try:
            dg_proj = gpd.GeoSeries([dg], crs="EPSG:4326").to_crs(epsg=display_epsg).iloc[0]
            uncovered = row_proj.difference(dg_proj.buffer(tol_m))
            uncovered_len = getattr(uncovered, "length", 0.0)
            if uncovered_len < total_len * 0.1:
                return True
        except Exception:
            if geom_wgs84.intersects(dg):
                return True
    return False


def _fetch_live_road_geoms(db, plot_id: int) -> list:
    """Roads from the live `lines` table, using the same query the general template's final
    render and the /plots/{id}/features/geojson endpoint (used by the Road Names panel) already
    use. Keeping road geometry sourced from one place across the naming panel, feature overrides,
    and every template's render avoids seam/gap artifacts where a named override's geometry
    (built from this same live query) doesn't line up with a differently-sourced base road
    segment (e.g. the older, separately-snapshotted `detected_features` rows).
    """
    rows = db.execute(text("""
        WITH roads AS (
            SELECT
                CASE
                    WHEN ST_SRID(r.geom) = 4326 THEN r.geom
                    WHEN ST_SRID(r.geom) = 0 THEN ST_SetSRID(r.geom, 4326)
                    ELSE ST_Transform(r.geom, 4326)
                END AS geom
            FROM lines r
            WHERE r.highway IS NOT NULL
        )
        SELECT roads.geom AS geom
        FROM roads
        JOIN plot_buffers b ON b.plot_id = :plot_id
        WHERE ST_Intersects(roads.geom, b.geom)
    """), {"plot_id": plot_id}).fetchall()
    geoms = []
    for row in rows:
        try:
            geoms.append(wkb.loads(row.geom))
        except Exception:
            continue

    # This depends on a plot_buffers row existing for the plot (written by
    # _run_plot_feature_detection). Some plots never got one - older plots, or subdivision child
    # lots that inherit detected_features straight from the parent without re-running full
    # detection - so fall back to whatever was captured into detected_features at creation time
    # rather than silently rendering with no roads at all.
    if not geoms:
        fallback_rows = db.execute(text("""
            SELECT geom FROM detected_features WHERE plot_id = :plot_id AND feature_type = 'road'
        """), {"plot_id": plot_id}).fetchall()
        for row in fallback_rows:
            try:
                geoms.append(wkb.loads(row.geom))
            except Exception:
                continue
    return geoms


def _collect_road_edge_lines(centerline_geom, half_width_m: float):
    """
    Build open road edges (left/right offsets) from a centerline geometry.
    This avoids closed rectangular end-caps from polygon-boundary rendering.
    """
    edges = []
    if centerline_geom is None or getattr(centerline_geom, "is_empty", True):
        return edges
    for line_part in _iter_line_geometries(centerline_geom):
        try:
            if getattr(line_part, "length", 0.0) <= 0:
                continue
            # Use round joins so overlaps/intersections look continuous.
            left = line_part.parallel_offset(half_width_m, "left", join_style=1)
            right = line_part.parallel_offset(half_width_m, "right", join_style=1)
            for off in (left, right):
                for seg in _iter_line_geometries(off):
                    if seg is None or getattr(seg, "is_empty", True):
                        continue
                    edges.append(seg)
        except Exception:
            continue
    # Merge contiguous edge segments so overlaps connect visually.
    if edges:
        try:
            merged = linemerge(unary_union(edges))
            merged_edges = [seg for seg in _iter_line_geometries(merged) if seg is not None and not getattr(seg, "is_empty", True)]
            if merged_edges:
                return merged_edges
        except Exception:
            pass
    return edges


def _collect_connected_road_edge_lines(road_geoms_with_width, snap_tol_m: float = 1.0):
    """
    Build connected road-edge lines where only non-overlap (dangling) ends are open:
    - overlapping/intersection zones stay connected
    - only true dangling ends are opened by trimming cap segments
    """
    line_parts = []
    for item in road_geoms_with_width or []:
        if not item or len(item) < 2:
            continue
        geom, half_width = item[0], item[1]
        if geom is None or getattr(geom, "is_empty", True):
            continue
        try:
            hw = max(0.5, float(half_width or 1.0))
        except Exception:
            hw = 1.0
        for seg in _iter_line_geometries(geom):
            if seg is None or getattr(seg, "is_empty", True):
                continue
            if getattr(seg, "length", 0.0) <= 0:
                continue
            line_parts.append((seg, hw))

    if not line_parts:
        return []

    edge_lines = []
    try:
        center_network = unary_union([seg for seg, _ in line_parts])
    except Exception:
        center_network = None

    snapped_parts = []
    for seg, hw in line_parts:
        try:
            snapped = snap(seg, center_network, snap_tol_m) if center_network is not None else seg
        except Exception:
            snapped = seg
        snapped_parts.append((snapped, hw))

    # Build merged road surface with flat caps; this keeps overlap joins connected.
    road_polys = []
    for seg, hw in snapped_parts:
        try:
            poly = seg.buffer(hw, cap_style=2, join_style=1)
            if poly is not None and not poly.is_empty:
                road_polys.append(poly)
        except Exception:
            continue
    if not road_polys:
        return []

    try:
        merged_surface = unary_union(road_polys)
    except Exception:
        merged_surface = road_polys[0]
        for p in road_polys[1:]:
            try:
                merged_surface = merged_surface.union(p)
            except Exception:
                continue

    boundary = getattr(merged_surface, "boundary", None)
    if boundary is None or getattr(boundary, "is_empty", True):
        return []
    edge_lines = [seg for seg in _iter_line_geometries(boundary) if seg is not None and not getattr(seg, "is_empty", True)]
    if not edge_lines:
        return []

    # Detect dangling centerline nodes (degree==1) and open only those ends.
    endpoint_tol = max(0.2, snap_tol_m * 0.25)
    endpoint_counts = {}
    endpoint_coords = {}
    try:
        merged_center = linemerge(unary_union([seg for seg, _ in snapped_parts]))
    except Exception:
        merged_center = unary_union([seg for seg, _ in snapped_parts])
    for seg in _iter_line_geometries(merged_center):
        if seg is None or getattr(seg, "is_empty", True):
            continue
        coords = list(seg.coords)
        if len(coords) < 2:
            continue
        for pt in (coords[0], coords[-1]):
            key = (int(round(pt[0] / endpoint_tol)), int(round(pt[1] / endpoint_tol)))
            endpoint_counts[key] = endpoint_counts.get(key, 0) + 1
            endpoint_coords[key] = (pt[0], pt[1])

    dangling_points = [Point(xy) for k, xy in endpoint_coords.items() if endpoint_counts.get(k, 0) == 1]
    if dangling_points:
        half_widths = [hw for _, hw in snapped_parts if hw is not None]
        mean_hw = (sum(half_widths) / len(half_widths)) if half_widths else 1.0
        cut_radius = max(0.45, min(2.5, mean_hw * 0.9))
        try:
            cut_geom = unary_union([pt.buffer(cut_radius) for pt in dangling_points])
            opened_edges = []
            for seg in edge_lines:
                try:
                    diff = seg.difference(cut_geom)
                except Exception:
                    diff = seg
                for part in _iter_line_geometries(diff):
                    if part is None or getattr(part, "is_empty", True):
                        continue
                    if getattr(part, "length", 0.0) < max(0.8, cut_radius * 0.6):
                        continue
                    opened_edges.append(part)
            if opened_edges:
                edge_lines = opened_edges
        except Exception:
            pass

    # Final snap/merge pass for cleaner joins.
    try:
        edge_network = unary_union(edge_lines)
        fine_tol = max(0.2, snap_tol_m * 0.35)
        snapped_edges = [snap(seg, edge_network, fine_tol) for seg in edge_lines]
        merged_edges = linemerge(unary_union(snapped_edges))
        final_edges = [seg for seg in _iter_line_geometries(merged_edges) if seg is not None and not getattr(seg, "is_empty", True)]
        if final_edges:
            return final_edges
    except Exception:
        pass
    return edge_lines


def _draw_road_tick_symbols(ax, line_geom, color: str, lw: float, scale_ratio=None) -> None:
    """Small perpendicular cross-ties at regular intervals along a road line - the "symbols" half
    of the dashed-with-symbols road style, distinguishing it from a plain dashed line the same way
    real plans mark an unpaved/proposed road differently from a paved one."""
    length = getattr(line_geom, "length", 0.0) or 0.0
    if length <= 0:
        return
    ratio = max(scale_ratio or 1000, 100)
    step = max(2.0, (8.0 / 1000.0) * ratio)
    tick_len = max(0.6, (1.4 / 1000.0) * ratio)
    probe = max(0.3, step * 0.15)
    d = step / 2.0
    while d < length:
        try:
            p = line_geom.interpolate(d)
            p_prev = line_geom.interpolate(max(0.0, d - probe))
            p_next = line_geom.interpolate(min(length, d + probe))
        except Exception:
            d += step
            continue
        tx, ty = p_next.x - p_prev.x, p_next.y - p_prev.y
        mag = math.hypot(tx, ty)
        if mag == 0:
            d += step
            continue
        nx, ny = -ty / mag, tx / mag
        ax.plot(
            [p.x - nx * tick_len / 2.0, p.x + nx * tick_len / 2.0],
            [p.y - ny * tick_len / 2.0, p.y + ny * tick_len / 2.0],
            color=color,
            lw=max(0.6, lw * 0.9),
            zorder=6,
            solid_capstyle="butt",
        )
        d += step


def _draw_road_edges(
    ax, edge_lines, font_scale=1.0, color: str = "black", linestyle="-", scale_ratio=None,
    road_style: str = "",
):
    if not edge_lines:
        return
    if scale_ratio:
        # Drop dangling stub segments too short to read as anything but a stray mark at this
        # plan's scale (e.g. a 3m sliver on a 1:5000 plan) - the same generalization real
        # cartographic products apply, rather than drawing every geometric fragment identically
        # regardless of scale.
        edge_lines = [
            seg for seg in edge_lines
            if feature_visible_at_scale(getattr(seg, "length", 0.0), scale_ratio, min_paper_mm=1.5)
        ]
        if not edge_lines:
            return
    lw = scaled_line_weight(0.3, font_scale, scale_ratio)
    draw_lines = edge_lines
    try:
        network = unary_union(edge_lines)
        snap_tol = max(0.5, 0.6 * max(1.0, float(font_scale)))
        snapped = [snap(seg, network, snap_tol) for seg in edge_lines]
        merged = linemerge(unary_union(snapped))
        merged_lines = [seg for seg in _iter_line_geometries(merged) if seg is not None and not getattr(seg, "is_empty", True)]
        if merged_lines:
            draw_lines = merged_lines
    except Exception:
        draw_lines = edge_lines

    # An explicit road_style choice overrides whichever linestyle this template would otherwise
    # use by default (solid for general/Adamawa, a plain dash for cadastral/FCT) - leaving
    # linestyle untouched when road_style is unset keeps every template's existing default intact.
    resolved_linestyle = linestyle
    draw_symbols = False
    if road_style == "solid":
        resolved_linestyle = "-"
    elif road_style == "dashed_symbol":
        resolved_linestyle = (0, (6, 4))
        draw_symbols = True

    for seg in draw_lines:
        try:
            x_vals, y_vals = seg.xy
            ax.plot(
                x_vals,
                y_vals,
                color=color,
                lw=lw,
                linestyle=resolved_linestyle,
                zorder=6,
                solid_joinstyle="round",
                solid_capstyle="round",
            )
            if draw_symbols:
                _draw_road_tick_symbols(ax, seg, color, lw, scale_ratio=scale_ratio)
        except Exception:
            continue


def _draw_names_along_path(
    ax,
    name_geom_pairs,
    color: str = "black",
    font_scale: float = 1.0,
    base_fontsize: float = 6.5,
    repeat_spacing_factor: float = 12.0,
    zorder: int = 10,
    halo: bool = True,
    skip_point_fn=None,
) -> None:
    """Draws each (geometry, name) pair's name text following the geometry's own direction,
    matching how road/river names read on real cadastral plans. A short line gets one centered
    label; a long or curved one gets the label repeated at intervals, each instance locally
    rotated to the path's tangent there, so the name reads naturally along bends instead of being
    confined to a single point. Placement always uses a sub-segment long enough to hold the whole
    label so it never spills past a junction or a line's end. A white halo keeps the text legible
    when a road/river crosses building hatch or another line.
    """
    if not name_geom_pairs:
        return
    fig = ax.figure
    try:
        fig_w_in, _ = fig.get_size_inches()
        axes_w_frac = max(1e-6, ax.get_position().width)
        xlim = ax.get_xlim()
        data_per_in = abs(xlim[1] - xlim[0]) / max(1e-6, (fig_w_in * axes_w_frac))
    except Exception:
        data_per_in = 1.0

    seen_pairs = set()
    for geom, name in name_geom_pairs:
        label = str(name or "").strip()
        if not label:
            continue
        try:
            geom_key = geom.wkb_hex
        except Exception:
            try:
                geom_key = geom.wkt
            except Exception:
                geom_key = f"{getattr(geom, 'geom_type', 'geom')}:{round(float(getattr(geom, 'length', 0.0) or 0.0), 3)}"
        pair_key = (label.casefold(), geom_key)
        if pair_key in seen_pairs:
            continue
        try:
            merged = linemerge(geom) if geom.geom_type == "MultiLineString" else geom
        except Exception:
            merged = geom
        parts = [seg for seg in _iter_line_geometries(merged) if seg is not None and not getattr(seg, "is_empty", True)]
        if not parts:
            continue
        parts.sort(key=lambda s: s.length, reverse=True)

        fontsize = max(5, int(base_fontsize * font_scale))
        char_w_m = (fontsize / 72.0) * 0.62 * data_per_in
        text_w_m = max(2.0, char_w_m * len(label))
        min_len_for_label = text_w_m * 1.4
        spacing = max(text_w_m * repeat_spacing_factor, min_len_for_label * 1.5)

        placed_any = False
        for seg in parts:
            seg_len = seg.length
            if seg_len < min_len_for_label:
                continue
            n_labels = max(1, int(seg_len // spacing))
            half_pad = min(0.45, (min_len_for_label / 2.0) / seg_len)
            for i in range(n_labels):
                frac = min(max((i + 0.5) / n_labels, half_pad), 1 - half_pad)
                mid = seg.interpolate(frac, normalized=True)
                p1 = seg.interpolate(max(0.0, frac - 0.02), normalized=True)
                p2 = seg.interpolate(min(1.0, frac + 0.02), normalized=True)
                angle = math.degrees(math.atan2(p2.y - p1.y, p2.x - p1.x))
                if angle < -90 or angle > 90:
                    angle += 180
                if skip_point_fn is not None and skip_point_fn(mid.x, mid.y, label):
                    continue
                text_kwargs = dict(
                    fontsize=fontsize, color=color, ha="center", va="center",
                    rotation=angle, weight="normal", zorder=zorder,
                )
                if halo:
                    text_kwargs["path_effects"] = [patheffects.withStroke(linewidth=2.0 * font_scale, foreground="white")]
                ax.text(mid.x, mid.y, label, **text_kwargs)
                placed_any = True
        if placed_any:
            seen_pairs.add(pair_key)


def _safe_text(value, fallback=""):
    text_value = str(value).strip() if value is not None else ""
    return text_value if text_value else fallback


def _normalize_scale_label(scale_text: str) -> str:
    raw = str(scale_text or "").strip().replace(" ", "")
    if not raw:
        return "1:2500"
    if ":" in raw:
        left, right = raw.split(":", 1)
        left = left or "1"
        right = right or "2500"
        return f"{left}:{right}"
    return f"1:{raw}"


def _normalize_scale_label_adamawa(scale_text: str) -> str:
    try:
        ratio = parse_scale_ratio(scale_text)
    except Exception:
        ratio = 2500
    return f"1: {int(ratio)}"


def _resolve_adamawa_origin_text(coordinate_system: str, display_epsg: int) -> str:
    cs = str(coordinate_system or "").strip().lower()
    if cs.startswith("utm_"):
        try:
            zone = int(cs.split("_")[1][0:2] if len(cs.split("_")) > 1 else cs.replace("utm_", ""))
        except Exception:
            zone = None
        if zone:
            return f"ORIGIN:- WGS 84 UTM ZONE {zone}N"
    if cs.startswith("minna_"):
        try:
            zone = int(cs.split("_")[1])
        except Exception:
            zone = None
        if zone:
            return f"ORIGIN:- MINNA DATUM UTM ZONE {zone}N"
    if cs == "wgs84":
        if 32600 < int(display_epsg or 0) < 32700:
            return f"ORIGIN:- WGS 84 UTM ZONE {int(display_epsg) - 32600}N"
        if 32700 < int(display_epsg or 0) < 32800:
            return f"ORIGIN:- WGS 84 UTM ZONE {int(display_epsg) - 32700}S"
        return "ORIGIN:- WGS84 (Lat/Lon)"
    return DEFAULT_ADAMAWA_ORIGIN_TEXT


def _draw_center_text_with_bold_suffix(
    fig,
    y: float,
    prefix: str,
    suffix: str,
    fontsize: float,
    fontfamily: str = ADAMAWA_FONT_FAMILY,
    color: str = "black",
):
    prefix = str(prefix or "")
    suffix = str(suffix or "")
    if not suffix:
        fig.text(
            0.5,
            y,
            prefix,
            ha="center",
            va="center",
            fontsize=fontsize,
            fontfamily=fontfamily,
            color=color,
        )
        return

    try:
        renderer = fig.canvas.get_renderer()
    except Exception:
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()

    fp_norm = FontProperties(size=fontsize, weight="normal", family=fontfamily)
    fp_bold = FontProperties(size=fontsize, weight="bold", family=fontfamily)
    try:
        w_prefix, _, _ = renderer.get_text_width_height_descent(prefix, fp_norm, ismath=False)
    except Exception:
        w_prefix = len(prefix) * fontsize * 0.58
    try:
        w_suffix, _, _ = renderer.get_text_width_height_descent(suffix, fp_bold, ismath=False)
    except Exception:
        w_suffix = len(suffix) * fontsize * 0.62

    fig_w = max(float(fig.bbox.width), 1.0)
    total_w_frac = (float(w_prefix) + float(w_suffix)) / fig_w
    x_start = 0.5 - (total_w_frac / 2.0)
    x_suffix = x_start + (float(w_prefix) / fig_w)

    fig.text(
        x_start,
        y,
        prefix,
        ha="left",
        va="center",
        fontsize=fontsize,
        fontfamily=fontfamily,
        weight="normal",
        color=color,
    )
    fig.text(
        x_suffix,
        y,
        suffix,
        ha="left",
        va="center",
        fontsize=fontsize,
        fontfamily=fontfamily,
        weight="bold",
        color=color,
    )


def _build_segment_rows(poly, station_names=None):
    coords, labels = _clockwise_ring_coords_and_labels(poly, station_names=station_names)
    if len(coords) < 2:
        return []
    rows = []
    for idx in range(len(coords) - 1):
        p1 = Point(coords[idx])
        p2 = Point(coords[idx + 1])
        from_label = str(labels[idx % len(labels)] or "").strip() if labels else ""
        to_label = str(labels[(idx + 1) % len(labels)] or "").strip() if labels else ""
        bearing = calculate_bearing_deg(p1, p2)
        rows.append({
            "from": from_label,
            "bearing": format_bearing_dms(bearing),
            "length": f"{p1.distance(p2):.2f}m",
            "to": to_label,
        })
    return rows


def _draw_adamawa_header(
    fig,
    rof_no: str,
    owner_name: str,
    location_text: str,
    lga_text: str,
    scale_text: str,
    authority_title: str,
    authority_date_text: str,
    font_scale=1.0,
    text_color: str = "black",
):
    fig.add_artist(patches.Rectangle((0.03, 0.03), 0.94, 0.94, transform=fig.transFigure, fill=False, lw=1.2))
    y = 0.945
    scale_y = y - 0.098
    authority_y = y - 0.123
    authority_date_y = y - 0.140
    _draw_center_text_with_bold_suffix(
        fig,
        y=y,
        prefix="R of O ",
        suffix=_safe_text(rof_no, "-"),
        fontsize=max(8, int(9 * font_scale)),
        fontfamily=ADAMAWA_FONT_FAMILY,
        color=text_color,
    )
    _draw_center_text_with_bold_suffix(
        fig,
        y=y - 0.022,
        prefix="SURVEY PLAN OF LAND BELONGING TO ",
        suffix=_safe_text(owner_name).upper(),
        fontsize=max(8, int(8 * font_scale)),
        fontfamily=ADAMAWA_FONT_FAMILY,
        color=text_color,
    )
    fig.text(
        0.5,
        y - 0.043,
        "AT",
        ha="center",
        va="center",
        fontsize=max(8, int(8 * font_scale)),
        fontfamily=ADAMAWA_FONT_FAMILY,
        color=text_color,
    )
    fig.text(
        0.5,
        y - 0.063,
        _safe_text(location_text).upper(),
        ha="center",
        va="center",
        fontsize=max(8, int(8 * font_scale)),
        fontfamily=ADAMAWA_FONT_FAMILY,
        color=text_color,
    )
    fig.text(
        0.5,
        y - 0.084,
        _safe_text(lga_text).upper(),
        ha="center",
        va="center",
        fontsize=max(8, int(8 * font_scale)),
        fontfamily=ADAMAWA_FONT_FAMILY,
        color=text_color,
    )
    fig.text(
        0.5,
        scale_y,
        f"SCALE:- {_normalize_scale_label_adamawa(scale_text)}",
        ha="center",
        va="center",
        fontsize=max(8, int(8 * font_scale)),
        fontfamily=ADAMAWA_FONT_FAMILY,
        color=text_color,
    )
    fig.text(
        0.5,
        authority_y,
        _safe_text(authority_title, DEFAULT_ADAMAWA_AUTHORITY_TITLE),
        ha="center",
        va="center",
        fontsize=max(8, int(8 * font_scale)),
        fontfamily=ADAMAWA_FONT_FAMILY,
        color=text_color,
    )
    fig.text(
        0.5,
        authority_date_y,
        _safe_text(authority_date_text, DEFAULT_ADAMAWA_AUTHORITY_DATE),
        ha="center",
        va="center",
        fontsize=max(7, int(7 * font_scale)),
        fontfamily=ADAMAWA_FONT_FAMILY,
        color=text_color,
    )


def _draw_adamawa_coordinate_labels(
    ax, font_scale=1.0, color: str = ADAMAWA_BLUE,
    grid_font: str | None = None, grid_size: int | None = None,
):
    xmin, xmax = ax.get_xlim()
    ymin, ymax = ax.get_ylim()
    left_e = f"{int(round(xmin))}mE"
    right_e = f"{int(round(xmax))}mE"
    top_n = f"{int(round(ymax))}mN"
    bottom_n = f"{int(round(ymin))}mN"
    fs = grid_size if grid_size else max(6, int(6 * font_scale))
    family = grid_font or ADAMAWA_FONT_FAMILY

    ax.text(0.0, 1.012, left_e, color=color, fontsize=fs, ha="left", va="bottom", transform=ax.transAxes, fontfamily=family)
    ax.text(1.0, 1.012, right_e, color=color, fontsize=fs, ha="right", va="bottom", transform=ax.transAxes, fontfamily=family)
    ax.text(0.0, -0.018, left_e, color=color, fontsize=fs, ha="left", va="top", transform=ax.transAxes, fontfamily=family)
    ax.text(1.0, -0.018, right_e, color=color, fontsize=fs, ha="right", va="top", transform=ax.transAxes, fontfamily=family)

    ax.text(-0.018, 1.0, top_n, color=color, fontsize=fs, ha="right", va="top", rotation=90, transform=ax.transAxes, fontfamily=family)
    ax.text(1.018, 1.0, top_n, color=color, fontsize=fs, ha="left", va="top", rotation=90, transform=ax.transAxes, fontfamily=family)
    ax.text(-0.018, 0.0, bottom_n, color=color, fontsize=fs, ha="right", va="bottom", rotation=90, transform=ax.transAxes, fontfamily=family)
    ax.text(1.018, 0.0, bottom_n, color=color, fontsize=fs, ha="left", va="bottom", rotation=90, transform=ax.transAxes, fontfamily=family)


def _draw_adamawa_map_frame(ax, font_scale=1.0):
    frame_color = ADAMAWA_BLUE
    frame_lw = max(0.9, 1.0 * font_scale)
    # Explicit frame lines (not spines) so they still render when axis is off.
    ax.add_line(mlines.Line2D([0, 1], [1, 1], transform=ax.transAxes, color=frame_color, lw=frame_lw, zorder=29, clip_on=False))
    ax.add_line(mlines.Line2D([0, 1], [0, 0], transform=ax.transAxes, color=frame_color, lw=frame_lw, zorder=29, clip_on=False))
    ax.add_line(mlines.Line2D([0, 0], [0, 1], transform=ax.transAxes, color=frame_color, lw=frame_lw, zorder=29, clip_on=False))
    ax.add_line(mlines.Line2D([1, 1], [0, 1], transform=ax.transAxes, color=frame_color, lw=frame_lw, zorder=29, clip_on=False))
    # Adamawa corner cross markers (same style seen in the reference template).
    corner_tick = 0.018
    for cx, cy in ((0, 0), (1, 0), (0, 1), (1, 1)):
        ax.add_line(
            mlines.Line2D(
                [cx - corner_tick, cx + corner_tick],
                [cy, cy],
                transform=ax.transAxes,
                color=frame_color,
                lw=frame_lw,
                zorder=30,
                clip_on=False,
            )
        )
        ax.add_line(
            mlines.Line2D(
                [cx, cx],
                [cy - corner_tick, cy + corner_tick],
                transform=ax.transAxes,
                color=frame_color,
                lw=frame_lw,
                zorder=30,
                clip_on=False,
            )
        )


def _draw_adamawa_north_arrow(
    ax,
    font_scale=1.0,
    style: str = "one_side_stem",
    color: str = "black",
):
    # Reuse the same north-arrow style/color logic used by the general template, but resolve
    # "blue" to the same navy already used for this template's grid frame, coordinate numbering,
    # and header/footer text (ADAMAWA_BLUE) instead of the general template's plain "blue".
    add_north_arrow(ax, font_scale=font_scale, style=style, color=color, blue_hex=ADAMAWA_BLUE)


def _draw_adamawa_bottom_blocks(
    fig,
    segment_rows,
    control_point_name: str,
    northing_text: str,
    easting_text: str,
    elevation_text: str,
    origin_text: str,
    topo_sheet_text: str,
    computation_no: str,
    cadastral_sheet_no: str,
    plan_no: str,
    scale_text: str,
    surveyed_by_text: str,
    disclaimer_text: str,
    font_scale=1.0,
    text_color: str = "black",
    grid_color: str | None = None,
):
    grid_color = grid_color or ADAMAWA_BLUE
    # Match Adamawa sample: compact footer with uniform text size.
    footer_font = max(5, int(round(5.2 * font_scale)))
    line_gap = 0.0095
    left_x = 0.06
    y = 0.182

    cp_name = _safe_text(control_point_name, "CONTROL POINT").replace("\n", " ").strip().upper()
    cp_name_match = re.match(r"^([A-Z]{2,})[-_/ ]?(\d+)$", cp_name)
    if cp_name_match:
        cp_name = f"{cp_name_match.group(1)}/{cp_name_match.group(2)}"
    origin_display = _safe_text(origin_text, DEFAULT_ADAMAWA_ORIGIN_TEXT).strip().upper()
    if not origin_display.startswith("ORIGIN"):
        origin_display = f"ORIGIN:- {origin_display}"
    else:
        origin_display = re.sub(r"^ORIGIN\s*[:\-]*\s*", "ORIGIN:- ", origin_display)

    def _with_prefix(prefix: str, value: str) -> str:
        raw = _safe_text(value, "-").strip()
        if not raw or raw == "-":
            return f"{prefix} -"
        upper = raw.upper()
        if upper.startswith(f"{prefix} "):
            return raw
        if upper.startswith(prefix):
            return f"{prefix} {raw[len(prefix):].strip()}"
        return f"{prefix} {raw}"

    fig.text(left_x, y, f"UTM CO-ORDINATE OF {cp_name}", fontsize=footer_font, color=grid_color, fontfamily=ADAMAWA_FONT_FAMILY, weight="normal")
    y -= line_gap
    fig.text(left_x, y, _with_prefix("N", northing_text), fontsize=footer_font, color=grid_color, fontfamily=ADAMAWA_FONT_FAMILY)
    y -= line_gap
    fig.text(left_x, y, _with_prefix("E", easting_text), fontsize=footer_font, color=grid_color, fontfamily=ADAMAWA_FONT_FAMILY)
    y -= line_gap
    fig.text(left_x, y, _with_prefix("Z", elevation_text), fontsize=footer_font, color=grid_color, fontfamily=ADAMAWA_FONT_FAMILY)
    y -= line_gap
    fig.text(left_x, y, origin_display, fontsize=footer_font, color=grid_color, fontfamily=ADAMAWA_FONT_FAMILY)
    y -= line_gap
    fig.text(left_x, y, _safe_text(topo_sheet_text, DEFAULT_ADAMAWA_TOPO_SHEET_TEXT).upper(), fontsize=footer_font, color=text_color, fontfamily=ADAMAWA_FONT_FAMILY)
    y -= (line_gap + 0.002)
    fig.text(left_x, y, DEFAULT_ADAMAWA_CHECKED_BY_TEXT, fontsize=footer_font, color=text_color, fontfamily=ADAMAWA_FONT_FAMILY)
    y -= (line_gap + 0.002)
    fig.text(left_x, y, DEFAULT_ADAMAWA_PASSED_BY_TEXT, fontsize=footer_font, color=text_color, fontfamily=ADAMAWA_FONT_FAMILY)
    y -= line_gap
    fig.text(left_x, y, DEFAULT_ADAMAWA_COPYRIGHT_TEXT, fontsize=footer_font, color=text_color, fontfamily=ADAMAWA_FONT_FAMILY)

    # Computation/plan block with angular curly style like the Adamawa sample.
    comp_label_y = 0.079
    plan_label_y = 0.063
    fig.text(0.06, comp_label_y, "COMPUTATION", fontsize=footer_font, color=text_color, fontfamily=ADAMAWA_FONT_FAMILY)
    fig.text(0.195, comp_label_y, "NO", fontsize=footer_font, color=text_color, fontfamily=ADAMAWA_FONT_FAMILY)
    fig.text(0.06, plan_label_y, "PLAN", fontsize=footer_font, color=text_color, fontfamily=ADAMAWA_FONT_FAMILY)
    fig.text(0.195, plan_label_y, "NO", fontsize=footer_font, color=text_color, fontfamily=ADAMAWA_FONT_FAMILY)

    top_y = comp_label_y + 0.002
    bottom_y = plan_label_y + 0.002
    brace_left = 0.235
    brace_trunk = 0.268
    brace_tip = 0.281
    mid_y = (top_y + bottom_y) / 2.0
    notch_half = 0.0035
    lw = 0.9
    fig.add_artist(mlines.Line2D(
        [brace_left, brace_trunk, brace_trunk, brace_tip, brace_trunk, brace_trunk, brace_left],
        [top_y, top_y, mid_y + notch_half, mid_y, mid_y - notch_half, bottom_y, bottom_y],
        transform=fig.transFigure,
        color="black",
        lw=lw,
    ))

    comp_display = _safe_text(computation_no, _safe_text(plan_no, "-"))
    comp_mid_y = (top_y + bottom_y) / 2.0
    fig.text(0.292, comp_mid_y, comp_display, fontsize=footer_font, color=text_color, fontfamily=ADAMAWA_FONT_FAMILY, va="center")
    fig.text(0.40, comp_mid_y, f"CADASTRAL SHEET NO. {_safe_text(cadastral_sheet_no, '-')}", fontsize=footer_font, color=text_color, fontfamily=ADAMAWA_FONT_FAMILY, va="center")
    fig.text(0.50, 0.053, DEFAULT_ADAMAWA_PREPARED_BY_TEXT, fontsize=footer_font, color=text_color, fontfamily=ADAMAWA_FONT_FAMILY, ha="center", va="center")

    table_ax = fig.add_axes([0.58, 0.110, 0.36, 0.090])
    table_ax.axis("off")
    header = ["FROM", "BEARING", "LENGTH", "TO"]
    all_rows = [[r["from"], r["bearing"], r["length"], r["to"]] for r in segment_rows]
    # Keep the table readable and non-overlapping for polygons with many vertices.
    # For overflow, show a compact summary row instead of shrinking text into collisions.
    # Keep footer table stable with larger text sizes.
    max_visible_rows = 8
    if len(all_rows) > max_visible_rows:
        keep_rows = max(1, max_visible_rows - 1)
        hidden_count = len(all_rows) - keep_rows
        rows = all_rows[:keep_rows] + [["...", "", "", f"+{hidden_count} more"]]
    else:
        rows = all_rows
    table = table_ax.table(
        cellText=rows,
        colLabels=header,
        colWidths=[0.24, 0.27, 0.23, 0.26],
        cellLoc="center",
        colLoc="center",
        bbox=[0, 0, 1, 1],
    )
    table.auto_set_font_size(False)
    table_font = footer_font
    table.set_fontsize(table_font)
    for (row_idx, col_idx), cell in table.get_celld().items():
        cell.set_linewidth(0.8 if row_idx == 0 else 0.5)
        if row_idx == 0:
            cell.set_text_props(weight="normal", fontfamily=ADAMAWA_FONT_FAMILY, color=text_color)
        else:
            cell.set_text_props(fontfamily=ADAMAWA_FONT_FAMILY, color=text_color)
    # Enforce even row height to prevent any accidental text clipping/overlap.
    total_rows = len(rows) + 1  # include header
    row_height = 1.0 / max(1, total_rows)
    for ridx in range(total_rows):
        for cidx in range(len(header)):
            cell = table.get_celld().get((ridx, cidx))
            if cell is not None:
                cell.set_height(row_height)

    # Draw right-note text in a dedicated note block below the table.
    right_note_ax = fig.add_axes([0.58, 0.032, 0.36, 0.066])
    right_note_ax.axis("off")
    note_font = footer_font
    disclaimer_line = _safe_text(disclaimer_text, DEFAULT_ADAMAWA_DISCLAIMER_TEXT).strip()
    surveyed_line = _safe_text(surveyed_by_text, "").strip()

    # Wrap full disclaimer/surveyed lines to the available note width without truncation.
    disclaimer_lines = _wrap_figure_text(
        fig,
        disclaimer_line,
        width_fig=0.36,
        fontsize=note_font,
        fontfamily=ADAMAWA_FONT_FAMILY,
    )
    surveyed_lines = _wrap_figure_text(
        fig,
        surveyed_line,
        width_fig=0.36,
        fontsize=note_font,
        fontfamily=ADAMAWA_FONT_FAMILY,
    ) if surveyed_line else []
    note_lines = disclaimer_lines + surveyed_lines
    note_text = "\n".join(note_lines) if note_lines else ""
    right_note_ax.text(
        0.0,
        1.0,
        note_text,
        fontsize=note_font,
        fontfamily=ADAMAWA_FONT_FAMILY,
        color=text_color,
        ha="left",
        va="top",
        linespacing=1.18,
        clip_on=True,
    )


def _render_plot_map_layout_adamawa(
    db,
    plot_id: int,
    output_path: str,
    title_text: str,
    location_text: str,
    lga_text: str,
    scale_text: str,
    surveyor_name: str,
    surveyor_rank: str,
    paper_size: str = "A4",
    station_names=None,
    coordinate_system: str = "wgs84",
    epsg_code: int = 4326,
    north_arrow_style: str = "one_side_stem",
    north_arrow_color: str = "black",
    beacon_style: str = "cross",
    road_width_m: float | None = None,
    road_width_override_m: float | None = None,
    adamawa_rof_no: str = "",
    adamawa_owner_name: str = "",
    adamawa_authority_title: str = DEFAULT_ADAMAWA_AUTHORITY_TITLE,
    adamawa_authority_date_text: str = DEFAULT_ADAMAWA_AUTHORITY_DATE,
    adamawa_control_point_name: str = "",
    adamawa_northing: str = "",
    adamawa_easting: str = "",
    adamawa_elevation: str = "",
    adamawa_origin_text: str = DEFAULT_ADAMAWA_ORIGIN_TEXT,
    adamawa_topo_sheet_text: str = DEFAULT_ADAMAWA_TOPO_SHEET_TEXT,
    adamawa_computation_no: str = "",
    adamawa_cadastral_sheet_no: str = "",
    adamawa_plan_no: str = "",
    adamawa_surveyed_by_text: str = "",
    adamawa_disclaimer_text: str = DEFAULT_ADAMAWA_DISCLAIMER_TEXT,
    preview_mode: bool = False,
    boundary_color: str | None = None,
    grid_color: str | None = None,
    text_color: str | None = None,
    road_color: str | None = None,
    river_color: str | None = None,
    building_color: str | None = None,
    building_hatch_type: str | None = None,
    road_style: str | None = None,
    title_font: str | None = None,
    title_size: int | None = None,
    grid_font: str | None = None,
    grid_size: int | None = None,
    station_font: str | None = None,
    station_size: int | None = None,
    bearing_font: str | None = None,
    bearing_size: int | None = None,
    area_font: str | None = None,
    area_size: int | None = None,
    measurement_polygon=None,
    measurement_area_m2: float | None = None,
):
    # None means "not overridden" - fall back to this template's own established defaults
    # (which differ slightly from the general template's, e.g. its navy grid/coordinate color)
    # so omitting these params leaves existing renders looking exactly as they do today.
    boundary_color = boundary_color or "red"
    grid_color = grid_color or ADAMAWA_BLUE
    text_color = text_color or "black"
    road_color = road_color or "black"
    river_color = river_color or "#10a3df"
    building_color = building_color or "black"
    building_hatch_type = building_hatch_type or "diagonal"
    road_style = road_style or ""
    plot_wkb = db.execute(text("SELECT geom FROM plots WHERE id=:id"), {"id": plot_id}).scalar()
    if not plot_wkb:
        raise ValueError("Plot not found")

    rows = db.execute(
        text("SELECT geom, feature_type FROM detected_features WHERE plot_id=:id"),
        {"id": plot_id},
    ).fetchall()
    override_rows = db.execute(
        text("""
            SELECT feature_type, action, name, width_m, ST_AsGeoJSON(geom) AS geojson
            FROM plot_feature_overrides
            WHERE plot_id = :id
        """),
        {"id": plot_id},
    ).fetchall()

    plot_geom = wkb.loads(plot_wkb)
    buildings, rivers, fences = [], [], []
    for r in rows:
        g = wkb.loads(r.geom)
        if r.feature_type == "building":
            buildings.append(g)
        elif r.feature_type == "river":
            rivers.append(g)
        elif r.feature_type == "fence":
            fences.append(g)
    # Roads come from the live `lines` table (same query the naming panel and general template
    # use) rather than the detected_features snapshot, so a named override's geometry always
    # lines up with the base road drawn here - no seam where a name was added.
    detected_roads = _fetch_live_road_geoms(db, plot_id)

    overrides = []
    import json
    for r in override_rows:
        geom = None
        if r.geojson:
            try:
                geom = shape(json.loads(r.geojson))
            except Exception:
                geom = None
        overrides.append({
            "feature_type": r.feature_type,
            "action": r.action,
            "name": r.name,
            "width_m": r.width_m if hasattr(r, "width_m") else None,
            "geom": geom,
        })

    def apply_overrides(base_list, feature_type: str):
        result = list(base_list)
        added = []
        delete_geoms = []
        use_coverage_match = feature_type in ("road", "river", "fence")
        for ov in overrides:
            if ov["feature_type"] != feature_type:
                continue
            geom = ov["geom"]
            if geom is None:
                continue
            try:
                if hasattr(geom, "is_valid") and not geom.is_valid:
                    geom = geom.buffer(0)
            except Exception:
                pass
            if ov["action"] in ("delete", "update"):
                if use_coverage_match:
                    result = [g for g in result if not _feature_override_replaces_native(g, geom, feature_type)]
                else:
                    result = [g for g in result if not g.intersects(geom)]
                delete_geoms.append(geom)
            if ov["action"] in ("add", "update"):
                result.append(geom)
                added.append(geom)
        if delete_geoms:
            if use_coverage_match:
                added = [
                    g for g in added
                    if not any(_feature_override_replaces_native(g, dg, feature_type) for dg in delete_geoms)
                ]
            else:
                added = [g for g in added if not any(g.intersects(dg) for dg in delete_geoms)]
        return result, added

    buildings, added_buildings = apply_overrides(buildings, "building")
    rivers, _ = apply_overrides(rivers, "river")
    fences, _ = apply_overrides(fences, "fence")
    roads_for_draw, _road_added_overrides = apply_overrides(detected_roads, "road")
    road_add_named_overrides = _resolve_override_names(overrides, "road")
    river_add_named_overrides = _resolve_override_names(overrides, "river")

    display_epsg = epsg_code
    if coordinate_system == "wgs84" or epsg_code == 4326:
        centroid = plot_geom.centroid
        utm_zone = int((centroid.x + 180) / 6) + 1
        hemisphere = "north" if centroid.y >= 0 else "south"
        display_epsg = 32600 + utm_zone if hemisphere == "north" else 32700 + utm_zone

    if measurement_polygon is not None:
        poly = measurement_polygon
        if not poly.is_valid:
            poly = poly.buffer(0)
        gdf_plot = gpd.GeoDataFrame(geometry=[poly], crs=f"EPSG:{display_epsg}")
    else:
        gdf_plot = gpd.GeoDataFrame(geometry=[plot_geom], crs="EPSG:4326").to_crs(epsg=display_epsg)
        poly = gdf_plot.geometry.iloc[0]
        if not poly.is_valid:
            poly = poly.buffer(0)
            gdf_plot = gpd.GeoDataFrame(geometry=[poly], crs=f"EPSG:{display_epsg}")
    area_m2 = float(measurement_area_m2) if measurement_area_m2 is not None else float(poly.area)

    paper_config = get_paper_config(paper_size)
    fig_width = paper_config["width"]
    fig_height = paper_config["height"]
    font_scale = paper_config["scale"]
    dpi = 150 if preview_mode else 200

    fig = plt.figure(figsize=(fig_width, fig_height), dpi=dpi)
    _ = FigureCanvas(fig)
    # Reserve extra room for the dense Adamawa footer so footer texts never collide.
    map_left, map_bottom, map_width, map_height = 0.08, 0.225, 0.84, 0.555
    ax = fig.add_axes([map_left, map_bottom, map_width, map_height])

    resolved_scale_text, scale_ratio = resolve_scale_text_and_ratio(
        scale_text,
        poly,
        fig_width * map_width,
        fig_height * map_height,
    )
    rof_no = _safe_text(adamawa_rof_no, f"{plot_id}")
    owner_text = _safe_text(adamawa_owner_name, title_text)
    _draw_adamawa_header(
        fig,
        rof_no=rof_no,
        owner_name=owner_text,
        location_text=location_text,
        lga_text=lga_text,
        scale_text=resolved_scale_text,
        authority_title=_safe_text(adamawa_authority_title, DEFAULT_ADAMAWA_AUTHORITY_TITLE),
        authority_date_text=_safe_text(adamawa_authority_date_text, DEFAULT_ADAMAWA_AUTHORITY_DATE),
        font_scale=font_scale,
        text_color=text_color,
    )

    apply_true_scale(ax, poly, scale_ratio, fig_width * map_width, fig_height * map_height)
    target_xlim = ax.get_xlim()
    target_ylim = ax.get_ylim()
    from shapely.geometry import box
    extent_poly = box(target_xlim[0], target_ylim[0], target_xlim[1], target_ylim[1])

    # Drop river segments too short to read as anything but a stray mark at this plan's scale.
    visible_rivers = filter_features_by_scale(rivers, display_epsg, scale_ratio, min_paper_mm=2.0)
    if visible_rivers:
        gpd.GeoDataFrame(geometry=visible_rivers, crs="EPSG:4326").to_crs(epsg=display_epsg).plot(
            ax=ax, color=river_color, lw=scaled_line_weight(0.3, font_scale, scale_ratio), zorder=5
        )

    road_edge_lines = []
    road_geom_width = []
    road_label_features = []
    road_snap_tol = max(1.0, (5.0 / 1000.0) * scale_ratio)
    for geom in roads_for_draw:
        if geom is None:
            continue
        try:
            gdf_line = gpd.GeoSeries([geom], crs="EPSG:4326").to_crs(epsg=display_epsg)
            line_proj = gdf_line.iloc[0]
        except Exception:
            continue
        expanded_frame = extent_poly.buffer(road_snap_tol)
        clipped = line_proj.intersection(expanded_frame)
        if clipped.is_empty:
            continue
        snapped_clipped = snap(clipped, extent_poly.boundary, road_snap_tol)
        try:
            half_w = max(1.0, (road_width_m or 3.0) / 2.0)
            road_geom_width.append((snapped_clipped, half_w))
        except Exception:
            continue

    # Label manually-added roads in Adamawa template too.
    for geom, name in road_add_named_overrides:
        try:
            gdf_line = gpd.GeoSeries([geom], crs="EPSG:4326").to_crs(epsg=display_epsg)
            line_proj = gdf_line.iloc[0]
        except Exception:
            continue
        expanded_frame = extent_poly.buffer(road_snap_tol)
        clipped = line_proj.intersection(expanded_frame)
        if clipped.is_empty:
            continue
        snapped_clipped = snap(clipped, extent_poly.boundary, road_snap_tol)
        road_label_features.append((snapped_clipped, name))

    river_label_features = []
    for geom, name in river_add_named_overrides:
        try:
            gdf_line = gpd.GeoSeries([geom], crs="EPSG:4326").to_crs(epsg=display_epsg)
            line_proj = gdf_line.iloc[0]
        except Exception:
            continue
        expanded_frame = extent_poly.buffer(road_snap_tol)
        clipped = line_proj.intersection(expanded_frame)
        if clipped.is_empty:
            continue
        snapped_clipped = snap(clipped, extent_poly.boundary, road_snap_tol)
        river_label_features.append((snapped_clipped, name))

    road_edge_lines = _collect_connected_road_edge_lines(road_geom_width, snap_tol_m=road_snap_tol)
    _draw_road_edges(ax, road_edge_lines, font_scale=font_scale, color=road_color, scale_ratio=scale_ratio, road_style=road_style)

    min_road_label_len = max(2.0, (10.0 / 1000.0) * scale_ratio)
    _draw_names_along_path(
        ax,
        [(g, n) for g, n in road_label_features if g.length > min_road_label_len],
        color=road_color, font_scale=font_scale, base_fontsize=6.0,
    )
    _draw_names_along_path(
        ax,
        [(g, n) for g, n in river_label_features if g.length > min_road_label_len],
        color=river_color, font_scale=font_scale, base_fontsize=6.0,
    )

    all_buildings = []
    if buildings:
        all_buildings.extend(buildings)
    if added_buildings:
        all_buildings.extend(added_buildings)
    # Skip real-world-tiny structures (a shed, a kiosk) that wouldn't render legibly at this
    # plan's scale - drawing every detected speck identically at 1:500 and 1:10000 alike isn't
    # how an accurate survey plan generalizes.
    visible_buildings = filter_features_by_scale(all_buildings, display_epsg, scale_ratio, min_paper_mm=2.0)
    if visible_buildings:
        draw_building_hatch(
            ax, visible_buildings, display_epsg, scale_ratio=scale_ratio, font_scale=font_scale,
            color=building_color, hatch_type=building_hatch_type,
        )
        gpd.GeoDataFrame(geometry=visible_buildings, crs="EPSG:4326").to_crs(epsg=display_epsg).plot(
            ax=ax, facecolor="none", edgecolor=building_color, lw=scaled_line_weight(0.2, font_scale, scale_ratio), zorder=8
        )
    if fences:
        draw_fences(ax, fences, display_epsg, scale_ratio=scale_ratio, font_scale=font_scale)
    fence_avoid_geom = build_fence_avoid_geom(fences, display_epsg=display_epsg, scale_ratio=scale_ratio)

    gdf_plot.plot(ax=ax, facecolor="none", edgecolor=boundary_color, lw=1.1 * font_scale, zorder=20)
    ax.set_xlim(target_xlim)
    ax.set_ylim(target_ylim)

    major = nice_grid_step(max(ax.get_xlim()[1] - ax.get_xlim()[0], ax.get_ylim()[1] - ax.get_ylim()[0]))
    draw_grid(ax, poly, major / 5.0, major, font_scale, full_grid=False, edge_ticks=False, color=grid_color)

    annotate_vertices(
        ax,
        poly,
        plot_id,
        station_names=station_names,
        font_scale=font_scale,
        min_label_length_m=0.0,
        avoid_geom=fence_avoid_geom,
        scale_ratio=scale_ratio,
        boundary_poly=poly,
        beacon_style=beacon_style,
        text_color=text_color,
        boundary_color=boundary_color,
        station_font=station_font,
        station_size=station_size,
        bearing_font=bearing_font,
        bearing_size=bearing_size,
    )
    area_label_point = None
    try:
        # Prefer an interior visual center for label placement.
        from shapely.ops import polylabel as _polylabel
        area_label_point = _polylabel(poly, tolerance=1.0)
    except Exception:
        area_label_point = None
    if area_label_point is None or area_label_point.is_empty:
        try:
            area_label_point = poly.centroid
        except Exception:
            area_label_point = None
    if area_label_point is None or area_label_point.is_empty:
        area_label_point = poly.representative_point()
    ax.text(
        area_label_point.x,
        area_label_point.y,
        format_area_display(area_m2),
        color="red",
        fontsize=area_size if area_size else max(7, int(7 * font_scale)),
        ha="center",
        va="center",
        zorder=26,
        **({"fontfamily": area_font} if area_font else {}),
    )

    _draw_adamawa_map_frame(ax, font_scale=font_scale)
    _draw_adamawa_coordinate_labels(ax, font_scale=font_scale, color=grid_color, grid_font=grid_font, grid_size=grid_size)
    _draw_adamawa_north_arrow(
        ax,
        font_scale=font_scale,
        style=north_arrow_style,
        color=north_arrow_color,
    )

    segment_rows = _build_segment_rows(poly, station_names=station_names)
    first_coords = list(poly.exterior.coords)[0]
    control_point_name = str((station_names or ["A"])[0])
    northing_value = f"{first_coords[1]:.3f}m"
    easting_value = f"{first_coords[0]:.3f}m"
    if len(first_coords) >= 3 and first_coords[2] is not None:
        try:
            elevation_value = f"{float(first_coords[2]):.3f}m"
        except Exception:
            elevation_value = "-"
    else:
        elevation_value = "-"
    origin_line = _resolve_adamawa_origin_text(coordinate_system, display_epsg)
    surveyed_line = f"Surveyed by {_safe_text(surveyor_name, '-')}"
    _draw_adamawa_bottom_blocks(
        fig,
        segment_rows=segment_rows,
        control_point_name=control_point_name,
        northing_text=northing_value,
        easting_text=easting_value,
        elevation_text=elevation_value,
        origin_text=origin_line,
        topo_sheet_text=_safe_text(adamawa_topo_sheet_text, DEFAULT_ADAMAWA_TOPO_SHEET_TEXT),
        computation_no=rof_no,
        cadastral_sheet_no=_safe_text(adamawa_cadastral_sheet_no, "-"),
        plan_no=rof_no,
        scale_text=scale_text,
        surveyed_by_text=surveyed_line,
        disclaimer_text=_safe_text(adamawa_disclaimer_text, DEFAULT_ADAMAWA_DISCLAIMER_TEXT),
        font_scale=font_scale,
        text_color=text_color,
        grid_color=grid_color,
    )

    ax.set_aspect("equal")
    ax.axis("off")
    fig.canvas.draw()
    fig.savefig(output_path, dpi=dpi)
    plt.close(fig)

# ======================
# South-South Nigeria cadastral template (Akwa Ibom / Rivers / Cross River)
# ======================
# One shared renderer used by all three states - "same template, separate selectable identity"
# per state, matching a real licensed-surveyor plan format common to that region: a formal
# title block (applicant/road/locality/LGA/state), a subdivided bar scale, blue coordinate-system
# and datum text, a red boundary with per-corner beacon references and bearing/distance labels,
# and a three-column certified-true-copy footer with the surveying firm's details.

CADASTRAL_FONT_FAMILY = "DejaVu Serif"
CADASTRAL_BLUE = "#1a3fa0"
DEFAULT_CADASTRAL_DATUM_TEXT = "MINNA DATUM"
CADASTRAL_STATE_LABELS = {
    "akwa_ibom_osg": "AKWA IBOM STATE",
    "rivers_osg": "RIVERS STATE",
    "cross_river_osg": "CROSS RIVER STATE",
}


def _resolve_cadastral_coordinate_system_text(coordinate_system: str, display_epsg: int) -> str:
    epsg = int(display_epsg or 0)
    if 32600 < epsg < 32700:
        return f"UTM ( ZONE {epsg - 32600} )"
    if 32700 < epsg < 32800:
        return f"UTM ( ZONE {epsg - 32700} )"
    return "UTM"


def _draw_cadastral_scale_bar(fig, cx: float, top_y: float, scale_text: str, font_scale: float = 1.0) -> float:
    """A center-zero bar scale with a subdivided left extension (e.g. "5  2.5  0     5      10m"),
    matching the convention used on real Nigerian cadastral plans. Returns the y-coordinate just
    below the bar's number labels so callers can keep stacking header content beneath it.
    """
    ratio = parse_scale_ratio(scale_text)
    candidates = [1, 2, 2.5, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 2500, 5000]
    target_m = max(1.0, ratio * 0.018)
    unit = min(candidates, key=lambda c: abs(c - target_m))
    half_unit = unit / 2.0
    half_w = 0.085
    bar_h = 0.007
    y0 = top_y - bar_h
    lw = 0.8 * font_scale

    def seg(x0, w, face):
        fig.add_artist(patches.Rectangle(
            (x0, y0), w, bar_h, transform=fig.transFigure,
            facecolor=face, edgecolor="black", lw=lw,
        ))

    seg(cx - half_w, half_w / 2.0, "white")
    seg(cx - half_w / 2.0, half_w / 2.0, "black")
    seg(cx, half_w, "black")
    seg(cx + half_w, half_w, "white")

    tick_h = 0.0045
    for tx in (cx - half_w, cx - half_w / 2.0, cx, cx + half_w, cx + 2 * half_w):
        fig.add_artist(mlines.Line2D(
            [tx, tx], [y0, y0 + bar_h + tick_h], transform=fig.transFigure,
            color="black", lw=lw,
        ))

    label_y = y0 - 0.009
    fs = max(5, int(6 * font_scale))
    for x, label in (
        (cx - half_w, f"{unit:g}"),
        (cx - half_w / 2.0, f"{half_unit:g}"),
        (cx, "0"),
        (cx + half_w, f"{unit:g}"),
        (cx + 2 * half_w, f"{2 * unit:g}m"),
    ):
        fig.text(x, label_y, label, ha="center", va="top", fontsize=fs, fontfamily=CADASTRAL_FONT_FAMILY)
    return label_y - 0.008


def _draw_cadastral_header(
    fig,
    plan_no: str,
    applicant_name: str,
    road_name: str,
    area_name: str,
    lga_text: str,
    state_label: str,
    scale_text: str,
    coordinate_system_text: str,
    datum_text: str,
    area_m2: float,
    font_scale: float = 1.0,
    text_color: str = "black",
) -> None:
    fig.add_artist(patches.Rectangle((0.03, 0.03), 0.94, 0.94, transform=fig.transFigure, fill=False, lw=1.2))

    box_w, box_h = 0.30, 0.05
    box_x, box_y = 0.045, 0.965 - box_h
    fig.add_artist(patches.Rectangle((box_x, box_y), box_w, box_h, transform=fig.transFigure, fill=False, lw=0.9))
    fig.text(
        box_x + box_w / 2.0, box_y + box_h / 2.0, f"PLAN NO: {_safe_text(plan_no, '-')}",
        ha="center", va="center", fontsize=max(7, int(7.5 * font_scale)), weight="bold",
        fontfamily=CADASTRAL_FONT_FAMILY, color=text_color,
    )

    cx = 0.685
    y = 0.94
    fs = max(6, int(7.6 * font_scale))
    line_h = 0.0145

    def line(text_value, bold=False, gap=1.0, size_mult=1.0, color=None):
        nonlocal y
        fig.text(
            cx, y, text_value, ha="center", va="center",
            fontsize=max(6, int(fs * size_mult)), weight=("bold" if bold else "normal"),
            fontfamily=CADASTRAL_FONT_FAMILY, color=color or text_color,
        )
        y -= line_h * gap

    line("PLAN SHEWING LANDED PROPERTY", bold=True)
    line("OF", gap=0.85)
    line(_safe_text(applicant_name, "-").upper(), bold=True, size_mult=1.05)
    line("ALONG", gap=0.85)
    line(_safe_text(road_name, "-").upper())
    area_name_clean = _safe_text(area_name)
    if area_name_clean:
        line(area_name_clean.upper())
    line(_safe_text(lga_text, "-").upper())
    line(_safe_text(state_label, "-").upper())
    y -= line_h * 0.2
    line(f"SCALE:  {_normalize_scale_label_adamawa(scale_text)}")
    y -= line_h * 0.3

    y = _draw_cadastral_scale_bar(fig, cx, y, scale_text, font_scale=font_scale)
    y -= line_h * 0.2

    line(f"COORDINATE SYSTEM : {_safe_text(coordinate_system_text, '-').upper()}", color=CADASTRAL_BLUE)
    line(f"DATUM / ORIGIN : {_safe_text(datum_text, DEFAULT_CADASTRAL_DATUM_TEXT).upper()}", color=CADASTRAL_BLUE)
    line(f"AREA: {format_area_display(area_m2)}", color="red")


def _draw_cadastral_footer(
    fig,
    plan_no: str,
    certification_date: str,
    surveyor_name: str,
    surveyor_credential: str,
    firm_block_text: str,
    state_label: str,
    font_scale: float = 1.0,
    text_color: str = "black",
) -> None:
    footer_top = 0.195
    footer_bottom = 0.035
    col1_x0, col1_x1 = 0.045, 0.30
    col2_x1 = 0.55
    col3_x1 = 0.965

    for x in (col1_x0, col1_x1, col2_x1, col3_x1):
        fig.add_artist(mlines.Line2D(
            [x, x], [footer_bottom, footer_top], transform=fig.transFigure,
            color="black", lw=0.9 * font_scale,
        ))
    for y_line in (footer_top, footer_bottom):
        fig.add_artist(mlines.Line2D(
            [col1_x0, col3_x1], [y_line, y_line], transform=fig.transFigure,
            color="black", lw=0.9 * font_scale,
        ))

    fs = max(6, int(7 * font_scale))
    mid_y = (footer_top + footer_bottom) / 2.0
    fig.text((col1_x0 + col1_x1) / 2.0, mid_y + 0.018, "PLAN NO:", ha="center", va="center",
              fontsize=fs, weight="bold", fontfamily=CADASTRAL_FONT_FAMILY, color=text_color)
    fig.text((col1_x0 + col1_x1) / 2.0, mid_y - 0.018, _safe_text(plan_no, "-"), ha="center", va="center",
              fontsize=fs, weight="bold", fontfamily=CADASTRAL_FONT_FAMILY, color=text_color)

    credential = _safe_text(surveyor_credential)
    surveyor_line = _safe_text(surveyor_name, "-").upper() + (f", {credential}" if credential else "")
    firm_lines = [ln.strip() for ln in _safe_text(firm_block_text).splitlines() if ln.strip()]

    right_lines = [
        ("CERTIFIED TRUE COPY OF ORIGINAL PLAN", True),
        (f"MADE BY ME ON {_safe_text(certification_date, '-')}", True),
        ("", False),
        (surveyor_line, True),
        ("SURVEYOR", False),
    ]
    for fl in firm_lines:
        right_lines.append((fl.upper(), False))
    state_clean = _safe_text(state_label)
    if state_clean:
        right_lines.append((f"{state_clean.upper()}.", False))

    right_cx = (col2_x1 + col3_x1) / 2.0
    # Line height adapts to how many lines the firm block actually needs (it's free text, so a
    # long address/contact block shouldn't be allowed to run past the sheet's outer frame) -
    # capped so a short block still looks properly spaced rather than oddly sparse.
    content_top = footer_top - 0.018
    content_bottom = footer_bottom + 0.012
    line_h = min(0.0155, (content_top - content_bottom) / max(1, len(right_lines)))
    line_h = max(0.009, line_h)
    y = content_top
    for txt, bold in right_lines:
        if not txt:
            y -= line_h * 0.85
            continue
        line_fs = fs if line_h >= 0.012 else max(5, int(fs * 0.85))
        fig.text(right_cx, y, txt, ha="center", va="center", fontsize=line_fs,
                  weight=("bold" if bold else "normal"), fontfamily=CADASTRAL_FONT_FAMILY, color=text_color)
        y -= line_h


def _draw_cadastral_coordinate_labels(
    ax, easting_m: float, northing_m: float, font_scale: float = 1.0, color: str = CADASTRAL_BLUE,
) -> None:
    """Draws the Easting label along the map's left edge and the Northing label at the lower-left
    corner - standalone reference text, not tied to any station point or the north arrow, so it
    never overlaps a beacon marker regardless of where that vertex happens to sit.
    """
    fs = max(6, int(6.5 * font_scale))
    ax.text(-0.022, 0.5, f"{easting_m:.3f}m E.", color=color, fontsize=fs, ha="right", va="center",
             rotation=90, transform=ax.transAxes, fontfamily=CADASTRAL_FONT_FAMILY)
    ax.text(0.06, -0.03, f"{northing_m:.3f}m N.", color=color, fontsize=fs, ha="left", va="top",
             transform=ax.transAxes, fontfamily=CADASTRAL_FONT_FAMILY)


def _draw_cadastral_corner_reference_labels(
    ax,
    font_scale: float = 1.0,
    color: str = CADASTRAL_BLUE,
    grid_font: str | None = None,
    grid_size: int | None = None,
) -> None:
    """Draw 4 corner reference crosses with compact Easting / Northing callouts.

    The South-South state templates use only corner reference crosses. Instead of mixing
    vertical and horizontal labels at the same anchor point, each corner gets a compact
    two-line callout placed just inside the frame to keep the coordinates legible.
    """
    xmin, xmax = ax.get_xlim()
    ymin, ymax = ax.get_ylim()
    family = grid_font or CADASTRAL_FONT_FAMILY
    fs = grid_size if grid_size else max(5, int(5.8 * font_scale))
    inset_x = 0.028
    inset_y = 0.028

    corners = (
        {"x": xmin, "y": ymax, "tx": inset_x, "ty": 1.0 - inset_y, "ha": "left", "va": "top"},
        {"x": xmax, "y": ymax, "tx": 1.0 - inset_x, "ty": 1.0 - inset_y, "ha": "right", "va": "top"},
        {"x": xmin, "y": ymin, "tx": inset_x, "ty": inset_y, "ha": "left", "va": "bottom"},
        {"x": xmax, "y": ymin, "tx": 1.0 - inset_x, "ty": inset_y, "ha": "right", "va": "bottom"},
    )
    for corner in corners:
        label = f"E {corner['x']:.3f}m\nN {corner['y']:.3f}m"
        ax.text(
            corner["tx"],
            corner["ty"],
            label,
            transform=ax.transAxes,
            ha=corner["ha"],
            va=corner["va"],
            fontsize=fs,
            color=color,
            fontfamily=family,
            linespacing=1.1,
            clip_on=False,
            zorder=7,
            bbox=dict(boxstyle="round,pad=0.12", facecolor="white", edgecolor="none", alpha=0.72),
        )


def _pick_cadastral_reference_points(
    poly: Polygon,
    span_x: float,
    span_y: float,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Pick the South-South cadastral reference corners.

    These templates read best when the main blue reference originates from the lower-left parcel
    corner: one vertical guide up to the U.N. arrow, one horizontal guide from the left frame to
    that same corner, and a separate right-side horizontal guide from the lower-right frontage
    corner. That matches the sample sheets more closely than a full crosshair or an upper-left
    anchor.
    """
    coords = list(poly.exterior.coords[:-1])
    if not coords:
        c = (poly.centroid.x, poly.centroid.y)
        return c, c
    min_x = min(pt[0] for pt in coords)
    min_y = min(pt[1] for pt in coords)
    span_x = max(float(span_x), 1.0)
    span_y = max(float(span_y), 1.0)

    def _norm(pt: tuple[float, float]) -> tuple[float, float]:
        return (
            (pt[0] - min_x) / span_x,
            (pt[1] - min_y) / span_y,
        )

    south_tol = max(1.0, span_y * 0.34)
    west_tol = max(1.0, span_x * 0.34)
    south_west_band = [
        pt for pt in coords
        if pt[0] <= (min_x + west_tol) or pt[1] <= (min_y + south_tol)
    ]
    if not south_west_band:
        south_west_band = coords

    lower_left = min(
        south_west_band,
        key=lambda pt: (
            (_norm(pt)[0] * 0.68) + (_norm(pt)[1] * 0.32),
            _norm(pt)[0],
            _norm(pt)[1],
        ),
    )

    frontage_band = [pt for pt in coords if pt[1] <= (min_y + south_tol)]
    if not frontage_band:
        frontage_band = coords
    lower_right = max(
        frontage_band,
        key=lambda pt: (
            (_norm(pt)[0] * 0.76) - (_norm(pt)[1] * 0.24),
            pt[0],
        ),
    )
    if lower_right == lower_left and len(coords) > 1:
        alternatives = [pt for pt in frontage_band if pt != lower_left]
        if not alternatives:
            alternatives = [pt for pt in coords if pt != lower_left]
        if alternatives:
            lower_right = max(
                alternatives,
                key=lambda pt: (
                    (_norm(pt)[0] * 0.76) - (_norm(pt)[1] * 0.24),
                    pt[0],
                ),
            )
    return lower_left, lower_right


def _draw_cadastral_reference_guide(
    fig,
    ax,
    lower_left_point: tuple[float, float],
    lower_right_point: tuple[float, float],
    font_scale: float = 1.0,
    color: str = CADASTRAL_BLUE,
    grid_font: str | None = None,
    grid_size: int | None = None,
    arrow_bottom_fig_y: float | None = None,
) -> None:
    """Draw the South-South single-reference blue guide.

    This matches the sample cadastral sheets used in Akwa Ibom, Cross River, and Rivers: a short
    vertical blue tick, and a single straight horizontal reference at the lower-left beacon's
    elevation running the full width of the frame - the left segment (frame to that beacon) and
    the right/frontage segment (the lower-right beacon to the frame) share that same y so they
    read as one continuous line rather than two segments at different heights.

    The vertical tick starts immediately below the U.N. north arrow marker (`arrow_bottom_fig_y`,
    the arrow's own stem-bottom in figure coordinates - see its computation in
    `_render_plot_map_layout_cadastral`) rather than reaching up to the printed border - the arrow
    marker already reads as the top/north reference, so a second line also touching the border
    independently of it was redundant. Falls back to the border itself if that figure-y isn't
    supplied (kept optional so any other future caller without an arrow still gets a sensible top).

    The horizontal segments still reach all the way to the printed black border rectangle, not
    just the edge of the plotting area - that rectangle is drawn separately in figure coordinates
    (see `_draw_cadastral_header`'s `Rectangle((0.03, 0.03), 0.94, 0.94, transform=fig.transFigure)`),
    inset further from the page than the axes' own data limits, so reaching it means converting
    those figure-space margins into this axes' current data coordinates rather than using
    `ax.get_xlim()`/`get_ylim()` directly - those only reach the plotting area's edge, which sits
    visibly short of the actual border line.
    """
    frame_margin, frame_far_edge = 0.03, 0.97

    def _fig_x_to_data(fig_x: float) -> float:
        display_pt = fig.transFigure.transform((fig_x, 0.5))
        return float(ax.transData.inverted().transform(display_pt)[0])

    def _fig_y_to_data(fig_y: float) -> float:
        display_pt = fig.transFigure.transform((0.5, fig_y))
        return float(ax.transData.inverted().transform(display_pt)[1])

    border_left_x = _fig_x_to_data(frame_margin)
    border_right_x = _fig_x_to_data(frame_far_edge)
    vertical_top = _fig_y_to_data(arrow_bottom_fig_y if arrow_bottom_fig_y is not None else frame_far_edge)

    xmin, xmax = ax.get_xlim()
    ymin, ymax = ax.get_ylim()
    span_x = max(abs(xmax - xmin), 1.0)
    span_y = max(abs(ymax - ymin), 1.0)
    lower_left_x, lower_left_y = lower_left_point
    lower_right_x, _lower_right_y = lower_right_point
    vertical_x, vertical_y = lower_left_point
    family = grid_font or CADASTRAL_FONT_FAMILY
    fs = grid_size if grid_size else max(6, int(6.2 * font_scale))
    line_lw = max(0.9, 1.0 * font_scale)
    stroke = [patheffects.withStroke(linewidth=line_lw * 2.1, foreground="white", alpha=0.92)]

    def _guide_line(x_data: list[float], y_data: list[float]) -> None:
        ax.add_line(
            mlines.Line2D(
                x_data,
                y_data,
                color=color,
                lw=line_lw,
                zorder=6,
                clip_on=False,
                path_effects=stroke,
            )
        )

    # Keep every guide segment's near end just short of the beacon it's referencing - a reference
    # line, not a mark that runs into/through the station point's own symbol and label. 1.2% read
    # as touching at real plot scale, so this needs to be a clearly visible gap, not a token one.
    station_gap_x = span_x * 0.035
    station_gap_y = span_y * 0.035

    # Short tick down from the arrow (vertical_top, set above) rather than a full-height line -
    # capped at the beacon's own height so it still just meets the point on a short/wide parcel
    # instead of overshooting past it. If the beacon happens to sit inside that short zone, back
    # off by the same gap instead of touching it.
    vertical_bottom = max(vertical_y, vertical_top - span_y * 0.14)
    if vertical_bottom <= vertical_y:
        vertical_bottom = vertical_y + station_gap_y
    _guide_line([vertical_x, vertical_x], [vertical_bottom, vertical_top])

    # A matching short dash directly opposite the top tick, at the same easting - mirroring its
    # "short, capped short of the beacon" treatment. Starts just above the footer table's own top
    # edge (_draw_cadastral_footer's footer_top = 0.195), not the true page border - the footer
    # box sits between there and the border, so reaching all the way down to the border ran the
    # line straight through the "PLAN NO" box.
    footer_top_fig_y = 0.195
    guide_bottom_y = _fig_y_to_data(footer_top_fig_y + 0.015)
    bottom_dash_top = min(vertical_y, guide_bottom_y + span_y * 0.14)
    if bottom_dash_top >= vertical_y:
        bottom_dash_top = vertical_y - station_gap_y
    _guide_line([vertical_x, vertical_x], [guide_bottom_y, bottom_dash_top])

    easting_x = vertical_x - span_x * 0.008
    easting_y = (vertical_bottom + vertical_top) / 2.0
    northing_x = border_left_x + span_x * 0.02
    northing_y = lower_left_y + span_y * 0.004
    northing_label = f"{lower_left_y:.3f}m N."

    # The left guide line should stop right where the northing number's text ends, not keep
    # running on as a bare line with nothing above it all the way toward the beacon - measure the
    # label's actual rendered width (real glyph metrics via the renderer, not a length guess) and
    # cap the line there, still never crossing the beacon-approach gap already established above.
    try:
        try:
            renderer = fig.canvas.get_renderer()
        except Exception:
            fig.canvas.draw()
            renderer = fig.canvas.get_renderer()
        fp = FontProperties(family=family, size=fs)
        label_px_width, _label_px_height, _descent = renderer.get_text_width_height_descent(
            northing_label, fp, ismath=False
        )
        anchor_display = ax.transData.transform((northing_x, northing_y))
        label_end_display = (anchor_display[0] + label_px_width, anchor_display[1])
        label_end_x = float(ax.transData.inverted().transform(label_end_display)[0])
        left_line_end_x = min(label_end_x + span_x * 0.006, lower_left_x - station_gap_x)
    except Exception:
        left_line_end_x = lower_left_x - station_gap_x

    # Both horizontal segments sit on the lower-left beacon's elevation, not each beacon's own -
    # the reference should read as one straight border line, not two strokes at different heights.
    # The left segment stops at the northing label's end (see above). The right/frontage segment
    # has no label of its own, so it mirrors the vertical line's treatment instead: a short tick
    # from the border rather than a long run all the way to the beacon, capped at the beacon's own
    # position (with the same gap) if that beacon happens to already sit within the short zone.
    _guide_line([border_left_x, left_line_end_x], [lower_left_y, lower_left_y])
    if lower_right_x < xmax:
        right_line_start_x = max(border_right_x - span_x * 0.14, lower_right_x + station_gap_x)
        _guide_line([right_line_start_x, border_right_x], [lower_left_y, lower_left_y])

    ax.text(
        easting_x,
        easting_y,
        f"{vertical_x:.3f}m E.",
        color=color,
        fontsize=fs,
        ha="right",
        va="center",
        rotation=90,
        zorder=7,
        clip_on=False,
        fontfamily=family,
        path_effects=stroke,
    )
    ax.text(
        northing_x,
        northing_y,
        f"{lower_left_y:.3f}m N.",
        color=color,
        fontsize=fs,
        ha="left",
        va="bottom",
        zorder=7,
        clip_on=False,
        fontfamily=family,
        path_effects=stroke,
    )


def _draw_cadastral_frontage_road(ax, poly, road_name: str, font_scale: float = 1.0, color: str = "black") -> None:
    """Dashed frontage-road reference line just outside the boundary's lowest edge, labeled with
    the applicant's road/street name - the same "OLD ORON ROAD"-style annotation seen on the
    reference template, for the common case where the actual road isn't in OSM/detected road data.
    """
    coords = list(poly.exterior.coords)
    if len(coords) < 2:
        return
    best_edge = None
    best_mid_y = None
    for i in range(len(coords) - 1):
        p1, p2 = coords[i], coords[i + 1]
        mid_y = (p1[1] + p2[1]) / 2.0
        if best_mid_y is None or mid_y < best_mid_y:
            best_mid_y = mid_y
            best_edge = (p1, p2)
    if best_edge is None:
        return
    (x1, y1), (x2, y2) = best_edge
    seg_len = math.hypot(x2 - x1, y2 - y1) or 1.0
    nx, ny = -(y2 - y1) / seg_len, (x2 - x1) / seg_len
    cx, cy = poly.centroid.x, poly.centroid.y
    mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    dist_pos = (mx + nx * 10 - cx) ** 2 + (my + ny * 10 - cy) ** 2
    dist_neg = (mx - nx * 10 - cx) ** 2 + (my - ny * 10 - cy) ** 2
    if dist_pos < dist_neg:
        nx, ny = -nx, -ny
    offset = max(3.0, seg_len * 0.06)
    dx, dy = (x2 - x1) / seg_len, (y2 - y1) / seg_len
    ext = seg_len * 0.15
    ox1, oy1 = x1 + nx * offset - dx * ext, y1 + ny * offset - dy * ext
    ox2, oy2 = x2 + nx * offset + dx * ext, y2 + ny * offset + dy * ext
    ax.add_line(mlines.Line2D(
        [ox1, ox2], [oy1, oy2], color=color, lw=1.0 * font_scale, linestyle=(0, (6, 4)), zorder=9,
        path_effects=[patheffects.withStroke(linewidth=2.2 * font_scale, foreground="white")],
    ))
    label_x, label_y = (ox1 + ox2) / 2.0, (oy1 + oy2) / 2.0 + ny * (offset * 0.5)
    ax.text(
        label_x, label_y, _safe_text(road_name).upper(), color=color, fontsize=max(6, int(7 * font_scale)),
        ha="center", va="center", weight="bold", zorder=10,
        path_effects=[patheffects.withStroke(linewidth=2.5, foreground="white")],
    )


def _render_plot_map_layout_cadastral(
    db,
    plot_id: int,
    output_path: str,
    title_text: str,
    location_text: str,
    lga_text: str,
    state_text: str,
    scale_text: str,
    surveyor_name: str,
    surveyor_rank: str,
    paper_size: str = "A4",
    station_names=None,
    coordinate_system: str = "wgs84",
    epsg_code: int = 4326,
    north_arrow_style: str = "one_side_stem",
    north_arrow_color: str = "black",
    beacon_style: str = "cross",
    road_width_m: float | None = None,
    road_width_override_m: float | None = None,
    cadastral_plan_no: str = "",
    cadastral_area_name: str = "",
    cadastral_datum_text: str = "",
    cadastral_firm_block_text: str = "",
    state_label: str = "STATE",
    preview_mode: bool = False,
    boundary_color: str | None = None,
    grid_color: str | None = None,
    text_color: str | None = None,
    road_color: str | None = None,
    river_color: str | None = None,
    building_color: str | None = None,
    building_hatch_type: str | None = None,
    road_style: str | None = None,
    title_font: str | None = None,
    title_size: int | None = None,
    grid_font: str | None = None,
    grid_size: int | None = None,
    station_font: str | None = None,
    station_size: int | None = None,
    bearing_font: str | None = None,
    bearing_size: int | None = None,
    area_font: str | None = None,
    area_size: int | None = None,
    measurement_polygon=None,
    measurement_area_m2: float | None = None,
):
    boundary_color = boundary_color or "red"
    grid_color = grid_color or CADASTRAL_BLUE
    text_color = text_color or "black"
    road_color = road_color or "black"
    river_color = river_color or "#10a3df"
    building_color = building_color or "black"
    building_hatch_type = building_hatch_type or "diagonal"
    road_style = road_style or ""

    plot_wkb = db.execute(text("SELECT geom FROM plots WHERE id=:id"), {"id": plot_id}).scalar()
    if not plot_wkb:
        raise ValueError("Plot not found")

    rows = db.execute(
        text("SELECT geom, feature_type FROM detected_features WHERE plot_id=:id"),
        {"id": plot_id},
    ).fetchall()
    override_rows = db.execute(
        text("""
            SELECT feature_type, action, name, width_m, ST_AsGeoJSON(geom) AS geojson
            FROM plot_feature_overrides
            WHERE plot_id = :id
        """),
        {"id": plot_id},
    ).fetchall()

    plot_geom = wkb.loads(plot_wkb)
    buildings, rivers, fences = [], [], []
    for r in rows:
        g = wkb.loads(r.geom)
        if r.feature_type == "building":
            buildings.append(g)
        elif r.feature_type == "river":
            rivers.append(g)
        elif r.feature_type == "fence":
            fences.append(g)
    # Roads come from the live `lines` table (same query the naming panel and general template
    # use) rather than the detected_features snapshot, so a named override's geometry always
    # lines up with the base road drawn here - no seam where a name was added.
    detected_roads = _fetch_live_road_geoms(db, plot_id)

    overrides = []
    import json
    for r in override_rows:
        geom = None
        if r.geojson:
            try:
                geom = shape(json.loads(r.geojson))
            except Exception:
                geom = None
        overrides.append({
            "feature_type": r.feature_type,
            "action": r.action,
            "name": r.name,
            "geom": geom,
        })

    def apply_overrides(base_list, feature_type: str):
        result = list(base_list)
        added = []
        delete_geoms = []
        use_coverage_match = feature_type in ("road", "river", "fence")
        for ov in overrides:
            if ov["feature_type"] != feature_type:
                continue
            geom = ov["geom"]
            if geom is None:
                continue
            try:
                if hasattr(geom, "is_valid") and not geom.is_valid:
                    geom = geom.buffer(0)
            except Exception:
                pass
            if ov["action"] in ("delete", "update"):
                if use_coverage_match:
                    result = [g for g in result if not _feature_override_replaces_native(g, geom, feature_type)]
                else:
                    result = [g for g in result if not g.intersects(geom)]
                delete_geoms.append(geom)
            if ov["action"] in ("add", "update"):
                result.append(geom)
                added.append(geom)
        if delete_geoms:
            if use_coverage_match:
                added = [
                    g for g in added
                    if not any(_feature_override_replaces_native(g, dg, feature_type) for dg in delete_geoms)
                ]
            else:
                added = [g for g in added if not any(g.intersects(dg) for dg in delete_geoms)]
        return result, added

    buildings, added_buildings = apply_overrides(buildings, "building")
    rivers, _ = apply_overrides(rivers, "river")
    fences, _ = apply_overrides(fences, "fence")
    roads_for_draw, _ = apply_overrides(detected_roads, "road")
    road_add_named_overrides = _resolve_override_names(overrides, "road")
    river_add_named_overrides = _resolve_override_names(overrides, "river")

    display_epsg = epsg_code
    if coordinate_system == "wgs84" or epsg_code == 4326:
        centroid = plot_geom.centroid
        utm_zone = int((centroid.x + 180) / 6) + 1
        hemisphere = "north" if centroid.y >= 0 else "south"
        display_epsg = 32600 + utm_zone if hemisphere == "north" else 32700 + utm_zone

    if measurement_polygon is not None:
        poly = measurement_polygon
        if not poly.is_valid:
            poly = poly.buffer(0)
        gdf_plot = gpd.GeoDataFrame(geometry=[poly], crs=f"EPSG:{display_epsg}")
    else:
        gdf_plot = gpd.GeoDataFrame(geometry=[plot_geom], crs="EPSG:4326").to_crs(epsg=display_epsg)
        poly = gdf_plot.geometry.iloc[0]
        if not poly.is_valid:
            poly = poly.buffer(0)
            gdf_plot = gpd.GeoDataFrame(geometry=[poly], crs=f"EPSG:{display_epsg}")
    area_m2 = float(measurement_area_m2) if measurement_area_m2 is not None else float(poly.area)

    paper_config = get_paper_config(paper_size)
    fig_width = paper_config["width"]
    fig_height = paper_config["height"]
    font_scale = paper_config["scale"]
    dpi = 150 if preview_mode else 200

    fig = plt.figure(figsize=(fig_width, fig_height), dpi=dpi)
    _ = FigureCanvas(fig)
    # Extra clearance above the footer (vs. Adamawa's margins) - vertex/bearing labels near the
    # bottom edge of a busy plot can get pushed outward by annotate_vertices' collision-avoidance
    # placement, and this template's footer is denser (3 columns) so there's less room to spare.
    map_left, map_bottom, map_width, map_height = 0.08, 0.27, 0.84, 0.455
    ax = fig.add_axes([map_left, map_bottom, map_width, map_height])

    plan_no_value = _safe_text(cadastral_plan_no, f"{plot_id}")
    resolved_state_label = _safe_text(state_text) or state_label
    coordinate_system_text = _resolve_cadastral_coordinate_system_text(coordinate_system, display_epsg)

    resolved_scale_text, scale_ratio = resolve_scale_text_and_ratio(
        scale_text,
        poly,
        fig_width * map_width,
        fig_height * map_height,
    )

    _draw_cadastral_header(
        fig,
        plan_no=plan_no_value,
        applicant_name=title_text,
        road_name=location_text,
        area_name=cadastral_area_name,
        lga_text=lga_text,
        state_label=resolved_state_label,
        scale_text=resolved_scale_text,
        coordinate_system_text=coordinate_system_text,
        datum_text=cadastral_datum_text,
        area_m2=float(area_m2 or 0),
        font_scale=font_scale,
        text_color=text_color,
    )

    apply_true_scale(ax, poly, scale_ratio, fig_width * map_width, fig_height * map_height)
    target_xlim = ax.get_xlim()
    target_ylim = ax.get_ylim()
    from shapely.geometry import box
    extent_poly = box(target_xlim[0], target_ylim[0], target_xlim[1], target_ylim[1])

    # Drop river segments too short to read as anything but a stray mark at this plan's scale.
    visible_rivers = filter_features_by_scale(rivers, display_epsg, scale_ratio, min_paper_mm=2.0)
    if visible_rivers:
        gpd.GeoDataFrame(geometry=visible_rivers, crs="EPSG:4326").to_crs(epsg=display_epsg).plot(
            ax=ax, color=river_color, lw=scaled_line_weight(0.3, font_scale, scale_ratio), zorder=5
        )

    road_geom_width = []
    road_snap_tol = max(1.0, (5.0 / 1000.0) * scale_ratio)
    for geom in roads_for_draw:
        if geom is None:
            continue
        try:
            gdf_line = gpd.GeoSeries([geom], crs="EPSG:4326").to_crs(epsg=display_epsg)
            line_proj = gdf_line.iloc[0]
        except Exception:
            continue
        expanded_frame = extent_poly.buffer(road_snap_tol)
        clipped = line_proj.intersection(expanded_frame)
        if clipped.is_empty:
            continue
        snapped_clipped = snap(clipped, extent_poly.boundary, road_snap_tol)
        try:
            half_w = max(1.0, (road_width_m or 3.0) / 2.0)
            road_geom_width.append((snapped_clipped, half_w))
        except Exception:
            continue
    road_edge_lines = _collect_connected_road_edge_lines(road_geom_width, snap_tol_m=road_snap_tol)
    _draw_road_edges(ax, road_edge_lines, font_scale=font_scale, color=road_color, linestyle=(0, (6, 4)), scale_ratio=scale_ratio, road_style=road_style)

    if _safe_text(location_text):
        try:
            _draw_cadastral_frontage_road(ax, poly, location_text, font_scale=font_scale, color=road_color)
        except Exception:
            pass

    def _project_clip_named(geom, name):
        try:
            gdf_line = gpd.GeoSeries([geom], crs="EPSG:4326").to_crs(epsg=display_epsg)
            line_proj = gdf_line.iloc[0]
        except Exception:
            return None
        expanded_frame = extent_poly.buffer(road_snap_tol)
        clipped = line_proj.intersection(expanded_frame)
        if clipped.is_empty:
            return None
        return snap(clipped, extent_poly.boundary, road_snap_tol), name

    min_named_len = max(2.0, (10.0 / 1000.0) * scale_ratio)
    road_label_features = [r for r in (_project_clip_named(g, n) for g, n in road_add_named_overrides) if r and r[0].length > min_named_len]
    river_label_features = [r for r in (_project_clip_named(g, n) for g, n in river_add_named_overrides) if r and r[0].length > min_named_len]
    _draw_names_along_path(ax, road_label_features, color=road_color, font_scale=font_scale, base_fontsize=6.0)
    _draw_names_along_path(ax, river_label_features, color=river_color, font_scale=font_scale, base_fontsize=6.0)

    all_buildings = []
    if buildings:
        all_buildings.extend(buildings)
    if added_buildings:
        all_buildings.extend(added_buildings)
    # Skip real-world-tiny structures (a shed, a kiosk) that wouldn't render legibly at this
    # plan's scale - drawing every detected speck identically at 1:500 and 1:10000 alike isn't
    # how an accurate survey plan generalizes.
    visible_buildings = filter_features_by_scale(all_buildings, display_epsg, scale_ratio, min_paper_mm=2.0)
    if visible_buildings:
        draw_building_hatch(
            ax, visible_buildings, display_epsg, scale_ratio=scale_ratio, font_scale=font_scale,
            color=building_color, hatch_type=building_hatch_type,
        )
        gpd.GeoDataFrame(geometry=visible_buildings, crs="EPSG:4326").to_crs(epsg=display_epsg).plot(
            ax=ax, facecolor="none", edgecolor=building_color, lw=scaled_line_weight(0.2, font_scale, scale_ratio), zorder=8
        )
    if fences:
        draw_fences(ax, fences, display_epsg, scale_ratio=scale_ratio, font_scale=font_scale)
    fence_avoid_geom = build_fence_avoid_geom(fences, display_epsg=display_epsg, scale_ratio=scale_ratio)

    # Beacon/bearing labels should steer clear of building hatch and road lines too, not just
    # fences - a label placed directly on top of dense building hatching reads as cluttered even
    # though it still renders above it, so fold buffered buildings/roads into the same
    # collision-avoidance geometry annotate_vertices already uses for fences.
    label_avoid_parts = [g for g in (fence_avoid_geom,) if g is not None]
    if all_buildings:
        try:
            buildings_buffer_m = max(1.0, (2.0 / 1000.0) * scale_ratio)
            buildings_proj = gpd.GeoSeries(all_buildings, crs="EPSG:4326").to_crs(epsg=display_epsg)
            buildings_avoid = unary_union(list(buildings_proj.buffer(buildings_buffer_m)))
            if buildings_avoid is not None and not buildings_avoid.is_empty:
                label_avoid_parts.append(buildings_avoid)
        except Exception:
            pass
    if road_edge_lines:
        try:
            road_buffer_m = max(1.0, (3.0 / 1000.0) * scale_ratio)
            roads_avoid = unary_union([seg.buffer(road_buffer_m) for seg in road_edge_lines])
            if roads_avoid is not None and not roads_avoid.is_empty:
                label_avoid_parts.append(roads_avoid)
        except Exception:
            pass
    label_avoid_geom = unary_union(label_avoid_parts) if label_avoid_parts else None

    gdf_plot.plot(ax=ax, facecolor="none", edgecolor=boundary_color, lw=1.1 * font_scale, zorder=20)
    ax.set_xlim(target_xlim)
    ax.set_ylim(target_ylim)

    annotate_vertices(
        ax,
        poly,
        plot_id,
        station_names=station_names,
        font_scale=font_scale,
        min_label_length_m=0.0,
        avoid_geom=label_avoid_geom,
        scale_ratio=scale_ratio,
        boundary_poly=poly,
        beacon_style=beacon_style,
        text_color=text_color,
        boundary_color=boundary_color,
        station_font=station_font,
        station_size=station_size,
        bearing_font=bearing_font,
        bearing_size=bearing_size,
    )

    span_x = max(abs(target_xlim[1] - target_xlim[0]), 1.0)
    span_y = max(abs(target_ylim[1] - target_ylim[0]), 1.0)
    lower_left_ref, lower_right_ref = _pick_cadastral_reference_points(poly, span_x, span_y)

    # The Akwa Ibom / Rivers / Cross River cadastral sheets use a dedicated reference guide that
    # sits slightly inside the left sheet edge, not on the parcel itself. Older saved drafts may
    # still carry another arrow style, so force the proper U.N. marker whenever this cadastral
    # renderer is active.
    guide_easting = lower_left_ref[0]
    top_northing = target_ylim[1] - span_y * 0.035

    guide_fig_x, _ = fig.transFigure.inverted().transform(
        ax.transData.transform((guide_easting, top_northing))
    )

    # The un_marker's text-baseline anchor is only part of its footprint: the pennant/loop extends
    # further UP from it by ~1.70x its own "size", and the stem extends further DOWN by ~1.04x
    # (see add_north_arrow's un_marker branch - unit = size*1.70/830, loop/pennant reach svg_top_y,
    # stem reaches svg_bottom_y). The old fixed "+0.06 above the plotting area" was applied to the
    # baseline without accounting for that downward stem reach, leaving well under 1% of the page
    # between the stem's actual bottom and the plot's own top edge for this template's map-area
    # size (map_bottom=0.27, map_height=0.455) - reading as the marker crowding straight into the
    # parcel/road lines near the top. Derive the anchor from the marker's real size so the stem
    # clears the plot with a real margin, while still keeping the pennant clear of the header above.
    arrow_font_scale = max(font_scale * 1.62, font_scale + 0.30)
    arrow_size = 0.032 * max(0.8, arrow_font_scale)
    arrow_upward_reach = arrow_size * 1.70
    arrow_downward_reach = arrow_size * 1.045
    header_bottom_y = 0.915
    plot_top_y = ax.get_position().y1
    min_anchor_y = plot_top_y + 0.03 + arrow_downward_reach
    max_anchor_y = header_bottom_y - 0.015 - arrow_upward_reach
    arrow_anchor_y = min(min_anchor_y, max_anchor_y)

    add_north_arrow(
        ax,
        font_scale=arrow_font_scale,
        style="un_marker",
        color=north_arrow_color,
        anchor_x=guide_fig_x,
        anchor_y=arrow_anchor_y,
        blue_hex=grid_color,
    )

    _draw_cadastral_reference_guide(
        fig,
        ax,
        lower_left_point=lower_left_ref,
        lower_right_point=lower_right_ref,
        font_scale=font_scale,
        color=grid_color,
        grid_font=grid_font,
        grid_size=grid_size,
        arrow_bottom_fig_y=arrow_anchor_y - arrow_downward_reach,
    )

    certification_date = datetime.now().strftime("%d / %m / %Y")
    _draw_cadastral_footer(
        fig,
        plan_no=plan_no_value,
        certification_date=certification_date,
        surveyor_name=surveyor_name,
        surveyor_credential=surveyor_rank,
        firm_block_text=cadastral_firm_block_text,
        state_label=resolved_state_label,
        font_scale=font_scale,
        text_color=text_color,
    )

    ax.set_aspect("equal")
    ax.axis("off")
    fig.canvas.draw()
    fig.savefig(output_path, dpi=dpi)
    plt.close(fig)

# ======================
# FCT Abuja cadastral template
# ======================
# A visually distinct government-issue style: gray-filled parcel, black boundary/bearing text
# (not red), a formal "LAND GRANTED TO" title block with a director's signature line, a
# beacon-to-beacon distance/bearing schedule table, and a NOTE box with the reference beacon's
# coordinates - matching a real FCT Abuja land-grant plan rather than the South-South states'
# cadastral layout.

FCT_FONT_FAMILY = "DejaVu Sans"


def _draw_fct_header(
    fig, applicant_name: str, file_no: str, district: str, cadastral_zone: str, plot_no: str,
    font_scale: float = 1.0, text_color: str = "black", title_prefix: str = "SURVEY PLAN FOR",
) -> None:
    fig.add_artist(patches.Rectangle((0.03, 0.03), 0.94, 0.94, transform=fig.transFigure, fill=False, lw=1.2))

    cx = 0.5
    y = 0.955
    fs = max(7, int(9 * font_scale))
    line_h = 0.021

    def line(text_value: str, bold: bool = True, size_mult: float = 1.0, gap: float = 1.0) -> None:
        nonlocal y
        fig.text(
            cx, y, text_value, ha="center", va="center", fontsize=max(6, int(fs * size_mult)),
            weight=("bold" if bold else "normal"), fontfamily=FCT_FONT_FAMILY, color=text_color,
        )
        y -= line_h * gap

    line(f"{_safe_text(title_prefix, 'SURVEY PLAN FOR').upper()} {_safe_text(applicant_name, '-').upper()}", size_mult=1.05)
    line(f"FILE NO: {_safe_text(file_no, '-').upper()}", bold=False)
    line(f"DISTRICT: {_safe_text(district, '-').upper()}", bold=False)
    line(f"CADASTRAL ZONE: {_safe_text(cadastral_zone, '-').upper()}", bold=False)
    line(f"PLOT NO: {_safe_text(plot_no, '-').upper()}", bold=False)

    y -= line_h * 0.9
    sig_w = 0.16
    fig.add_artist(mlines.Line2D(
        [cx - sig_w / 2, cx + sig_w / 2], [y, y], transform=fig.transFigure,
        color=text_color, lw=1.0 * font_scale,
    ))
    y -= line_h * 0.65
    fig.text(
        cx, y, "DIRECTOR OF SURVEYING AND MAPPING", ha="center", va="center",
        fontsize=max(6, int(fs * 0.72)), fontfamily=FCT_FONT_FAMILY, color=text_color,
    )


def _draw_fct_scale_schedule(fig, y_top: float, scale_text: str, font_scale: float = 1.0, text_color: str = "black") -> float:
    fs = max(7, int(8.5 * font_scale))
    fig.text(
        0.5, y_top, f"SCALE: {_normalize_scale_label_adamawa(scale_text)}", ha="center", va="top",
        fontsize=fs, weight="bold", fontfamily=FCT_FONT_FAMILY, color=text_color,
    )
    y2 = y_top - 0.022
    fig.text(
        0.5, y2, "SCHEDULE: AS DESCRIBED IN GRAPHICS ABOVE", ha="center", va="top",
        fontsize=max(6, int(fs * 0.78)), fontfamily=FCT_FONT_FAMILY, color=text_color,
    )
    return y2 - 0.02


def _compute_fct_beacon_schedule(poly, station_names=None) -> list:
    """[(from_label, to_label, distance_m, bearing_deg), ...] for each boundary edge, in the same
    clockwise order and station labeling annotate_vertices already draws on the map.
    """
    coords, labels = _clockwise_ring_coords_and_labels(poly, station_names=station_names)
    n = len(labels)
    rows = []
    for i in range(n):
        p1 = Point(coords[i])
        p2 = Point(coords[i + 1])
        bearing = calculate_bearing_deg(p1, p2)
        rows.append((labels[i], labels[(i + 1) % n], p1.distance(p2), bearing))
    return rows


def _draw_fct_beacon_table(
    fig, x0: float, y_top: float, y_bottom: float, rows: list, font_scale: float = 1.0, text_color: str = "black",
) -> None:
    fs = max(6, int(7 * font_scale))
    col1_x, col2_x, col3_x = x0, x0 + 0.21, x0 + 0.30
    for cx, label in ((col1_x, "BEACON No."), (col2_x, "DIST"), (col3_x, "BEARING")):
        fig.text(cx, y_top, label, weight="bold", fontsize=fs,
                  fontfamily=FCT_FONT_FAMILY, color=text_color, ha="left", va="top")
    y = y_top - 0.02
    row_h = max(0.013, min(0.02, (y - y_bottom) / max(1, len(rows))))
    line_fs = fs if row_h >= 0.016 else max(5, int(fs * 0.85))
    for frm, to, dist_m, bearing in rows:
        fig.text(col1_x, y, f"FROM {frm} TO {to}  =",
                  fontsize=line_fs, fontfamily=FCT_FONT_FAMILY, color=text_color, ha="left", va="top")
        fig.text(col2_x, y, f"{dist_m:.2f}m",
                  fontsize=line_fs, fontfamily=FCT_FONT_FAMILY, color=text_color, ha="left", va="top")
        fig.text(col3_x, y, f"AT {format_bearing_dms(bearing)}",
                  fontsize=line_fs, fontfamily=FCT_FONT_FAMILY, color=text_color, ha="left", va="top")
        y -= row_h


def _resolve_fct_coordinate_system_text(display_epsg: int) -> str:
    epsg = int(display_epsg or 0)
    if 32600 < epsg < 32700:
        return f"UTM ZONE {epsg - 32600}N"
    if 32700 < epsg < 32800:
        return f"UTM ZONE {epsg - 32700}S"
    return "UTM"


def _draw_fct_cadastral_map_table(
    fig, x0: float, y_top: float, scale_text: str, cadastral_map_ref: str,
    font_scale: float = 1.0, text_color: str = "black",
) -> None:
    fs = max(6, int(7 * font_scale))
    fig.text(
        x0, y_top, f"CADASTRAL MAP {_normalize_scale_label_adamawa(scale_text)}", weight="bold",
        fontsize=fs, fontfamily=FCT_FONT_FAMILY, color=text_color, ha="left", va="top",
    )
    table_top = y_top - 0.02
    row_h = 0.022
    col_w = 0.10
    table_bottom = table_top - row_h * 2
    for i in range(3):
        xline = x0 + i * col_w
        fig.add_artist(mlines.Line2D(
            [xline, xline], [table_bottom, table_top], transform=fig.transFigure,
            color=text_color, lw=0.8 * font_scale,
        ))
    for j in range(3):
        yline = table_top - j * row_h
        fig.add_artist(mlines.Line2D(
            [x0, x0 + col_w * 2], [yline, yline], transform=fig.transFigure,
            color=text_color, lw=0.8 * font_scale,
        ))
    cell_values = [["---", _safe_text(cadastral_map_ref, "---").upper()], ["---", "---"]]
    for r in range(2):
        for c in range(2):
            cx = x0 + col_w * c + col_w / 2.0
            cy = table_top - row_h * r - row_h / 2.0
            fig.text(cx, cy, cell_values[r][c], fontsize=fs, fontfamily=FCT_FONT_FAMILY,
                      color=text_color, ha="center", va="center")


def _draw_fct_note_box(
    fig, x0: float, y_top: float, first_station_name: str, fct_cadastral_zone: str,
    easting_m: float, northing_m: float, display_epsg: int, scale_text: str, cadastral_map_ref: str,
    font_scale: float = 1.0, text_color: str = "black",
) -> None:
    fs = max(6, int(7 * font_scale))
    y = y_top
    fig.text(x0, y, "NOTE:", weight="bold", fontsize=max(7, int(fs * 1.05)),
              fontfamily=FCT_FONT_FAMILY, color=text_color, ha="left", va="top")
    y -= 0.02
    short_name = _safe_text(first_station_name, "-").upper()
    zone_clean = _safe_text(fct_cadastral_zone)
    full_beacon_text = f"FCT {zone_clean} {short_name}".strip() if zone_clean else f"FCT {short_name}"
    lines = [
        f"FULL BEACON NUMBER {full_beacon_text}",
        f"COORDINATES OF {short_name}",
        f"N. {northing_m:,.2f}",
        f"E. {easting_m:,.2f}",
        f"COORDINATE SYSTEM {_resolve_fct_coordinate_system_text(display_epsg)}",
    ]
    for ln in lines:
        fig.text(x0, y, ln, fontsize=fs, fontfamily=FCT_FONT_FAMILY, color=text_color, ha="left", va="top")
        y -= 0.019
    y -= 0.012
    _draw_fct_cadastral_map_table(fig, x0, y, scale_text, cadastral_map_ref, font_scale=font_scale, text_color=text_color)


def _draw_fct_footer(
    fig, y: float, surveyor_name: str, surveyor_rank: str,
    font_scale: float = 1.0, text_color: str = "black",
) -> None:
    fs = max(6, int(7 * font_scale))
    credential = _safe_text(surveyor_rank)
    surveyor_line = f"SURVEYED BY: {_safe_text(surveyor_name, '-').upper()}" + (f", {credential}" if credential else "")
    date_text = datetime.now().strftime("%d %B %Y")
    fig.text(0.05, y, surveyor_line, fontsize=fs, fontfamily=FCT_FONT_FAMILY, color=text_color, ha="left", va="top")
    fig.text(
        0.05, y - 0.02, f"PREPARED ON {date_text}",
        fontsize=fs, fontfamily=FCT_FONT_FAMILY, color=text_color, ha="left", va="top",
    )


def _render_plot_map_layout_fct(
    db,
    plot_id: int,
    output_path: str,
    title_text: str,
    lga_text: str,
    state_text: str,
    scale_text: str,
    surveyor_name: str,
    surveyor_rank: str,
    paper_size: str = "A4",
    station_names=None,
    coordinate_system: str = "wgs84",
    epsg_code: int = 4326,
    north_arrow_style: str = "nn_arrow",
    north_arrow_color: str = "black",
    beacon_style: str = "cross",
    road_width_m: float | None = None,
    road_width_override_m: float | None = None,
    cadastral_plan_no: str = "",
    fct_file_no: str = "",
    fct_district: str = "",
    fct_cadastral_zone: str = "",
    fct_origin_beacon_text: str = "",
    fct_cadastral_map_ref: str = "",
    fct_title_prefix: str = "",
    preview_mode: bool = False,
    boundary_color: str | None = None,
    grid_color: str | None = None,
    text_color: str | None = None,
    road_color: str | None = None,
    river_color: str | None = None,
    building_color: str | None = None,
    building_hatch_type: str | None = None,
    road_style: str | None = None,
    title_font: str | None = None,
    title_size: int | None = None,
    grid_font: str | None = None,
    grid_size: int | None = None,
    station_font: str | None = None,
    station_size: int | None = None,
    bearing_font: str | None = None,
    bearing_size: int | None = None,
    area_font: str | None = None,
    area_size: int | None = None,
    measurement_polygon=None,
    measurement_area_m2: float | None = None,
):
    boundary_color = boundary_color or "red"
    text_color = text_color or "black"
    road_color = road_color or "black"
    river_color = river_color or "#10a3df"
    building_color = building_color or "black"
    building_hatch_type = building_hatch_type or "diagonal"
    road_style = road_style or ""
    # Bearing/distance labels follow the same boundary_color the Appearance panel's color picker
    # sets for the boundary line itself (red by default) - annotate_vertices' boundary_color param
    # drives text color independently of the actual boundary line, which is drawn separately below.
    bearing_text_color = boundary_color

    plot_wkb = db.execute(text("SELECT geom FROM plots WHERE id=:id"), {"id": plot_id}).scalar()
    if not plot_wkb:
        raise ValueError("Plot not found")

    rows = db.execute(
        text("SELECT geom, feature_type FROM detected_features WHERE plot_id=:id"),
        {"id": plot_id},
    ).fetchall()
    override_rows = db.execute(
        text("""
            SELECT feature_type, action, name, width_m, ST_AsGeoJSON(geom) AS geojson
            FROM plot_feature_overrides
            WHERE plot_id = :id
        """),
        {"id": plot_id},
    ).fetchall()

    plot_geom = wkb.loads(plot_wkb)
    buildings, rivers, fences = [], [], []
    for r in rows:
        g = wkb.loads(r.geom)
        if r.feature_type == "building":
            buildings.append(g)
        elif r.feature_type == "river":
            rivers.append(g)
        elif r.feature_type == "fence":
            fences.append(g)
    detected_roads = _fetch_live_road_geoms(db, plot_id)

    overrides = []
    import json
    for r in override_rows:
        geom = None
        if r.geojson:
            try:
                geom = shape(json.loads(r.geojson))
            except Exception:
                geom = None
        overrides.append({
            "feature_type": r.feature_type,
            "action": r.action,
            "name": r.name,
            "geom": geom,
        })

    def apply_overrides(base_list, feature_type: str):
        result = list(base_list)
        added = []
        delete_geoms = []
        use_coverage_match = feature_type in ("road", "river", "fence")
        for ov in overrides:
            if ov["feature_type"] != feature_type:
                continue
            geom = ov["geom"]
            if geom is None:
                continue
            try:
                if hasattr(geom, "is_valid") and not geom.is_valid:
                    geom = geom.buffer(0)
            except Exception:
                pass
            if ov["action"] in ("delete", "update"):
                if use_coverage_match:
                    result = [g for g in result if not _feature_override_replaces_native(g, geom, feature_type)]
                else:
                    result = [g for g in result if not g.intersects(geom)]
                delete_geoms.append(geom)
            if ov["action"] in ("add", "update"):
                result.append(geom)
                added.append(geom)
        if delete_geoms:
            if use_coverage_match:
                added = [
                    g for g in added
                    if not any(_feature_override_replaces_native(g, dg, feature_type) for dg in delete_geoms)
                ]
            else:
                added = [g for g in added if not any(g.intersects(dg) for dg in delete_geoms)]
        return result, added

    buildings, added_buildings = apply_overrides(buildings, "building")
    rivers, _ = apply_overrides(rivers, "river")
    fences, _ = apply_overrides(fences, "fence")
    roads_for_draw, _ = apply_overrides(detected_roads, "road")
    road_add_named_overrides = _resolve_override_names(overrides, "road")
    river_add_named_overrides = _resolve_override_names(overrides, "river")

    display_epsg = epsg_code
    if coordinate_system == "wgs84" or epsg_code == 4326:
        centroid = plot_geom.centroid
        utm_zone = int((centroid.x + 180) / 6) + 1
        hemisphere = "north" if centroid.y >= 0 else "south"
        display_epsg = 32600 + utm_zone if hemisphere == "north" else 32700 + utm_zone

    if measurement_polygon is not None:
        poly = measurement_polygon
        if not poly.is_valid:
            poly = poly.buffer(0)
        gdf_plot = gpd.GeoDataFrame(geometry=[poly], crs=f"EPSG:{display_epsg}")
    else:
        gdf_plot = gpd.GeoDataFrame(geometry=[plot_geom], crs="EPSG:4326").to_crs(epsg=display_epsg)
        poly = gdf_plot.geometry.iloc[0]
        if not poly.is_valid:
            poly = poly.buffer(0)
            gdf_plot = gpd.GeoDataFrame(geometry=[poly], crs=f"EPSG:{display_epsg}")
    area_m2 = float(measurement_area_m2) if measurement_area_m2 is not None else float(poly.area)

    paper_config = get_paper_config(paper_size)
    fig_width = paper_config["width"]
    fig_height = paper_config["height"]
    font_scale = paper_config["scale"]
    dpi = 150 if preview_mode else 200

    fig = plt.figure(figsize=(fig_width, fig_height), dpi=dpi)
    _ = FigureCanvas(fig)
    # Narrower than the other templates' maps - the reference plan reserves the right margin for
    # a large north arrow standing beside the parcel, not sitting above the header like the other
    # templates' arrows do.
    map_left, map_bottom, map_width, map_height = 0.08, 0.30, 0.66, 0.48
    ax = fig.add_axes([map_left, map_bottom, map_width, map_height])

    resolved_scale_text, scale_ratio = resolve_scale_text_and_ratio(
        scale_text,
        poly,
        fig_width * map_width,
        fig_height * map_height,
    )
    plot_no_value = _safe_text(cadastral_plan_no, f"{plot_id}")
    _draw_fct_header(
        fig, applicant_name=title_text, file_no=fct_file_no, district=fct_district,
        cadastral_zone=fct_cadastral_zone, plot_no=plot_no_value, font_scale=font_scale, text_color=text_color,
        title_prefix=fct_title_prefix or "SURVEY PLAN FOR",
    )

    apply_true_scale(ax, poly, scale_ratio, fig_width * map_width, fig_height * map_height)
    target_xlim = ax.get_xlim()
    target_ylim = ax.get_ylim()
    from shapely.geometry import box
    extent_poly = box(target_xlim[0], target_ylim[0], target_xlim[1], target_ylim[1])

    # Drop river segments too short to read as anything but a stray mark at this plan's scale.
    visible_rivers = filter_features_by_scale(rivers, display_epsg, scale_ratio, min_paper_mm=2.0)
    if visible_rivers:
        gpd.GeoDataFrame(geometry=visible_rivers, crs="EPSG:4326").to_crs(epsg=display_epsg).plot(
            ax=ax, color=river_color, lw=scaled_line_weight(0.3, font_scale, scale_ratio), zorder=5
        )

    road_geom_width = []
    road_snap_tol = max(1.0, (5.0 / 1000.0) * scale_ratio)
    for geom in roads_for_draw:
        if geom is None:
            continue
        try:
            gdf_line = gpd.GeoSeries([geom], crs="EPSG:4326").to_crs(epsg=display_epsg)
            line_proj = gdf_line.iloc[0]
        except Exception:
            continue
        expanded_frame = extent_poly.buffer(road_snap_tol)
        clipped = line_proj.intersection(expanded_frame)
        if clipped.is_empty:
            continue
        snapped_clipped = snap(clipped, extent_poly.boundary, road_snap_tol)
        try:
            half_w = max(1.0, (road_width_m or 3.0) / 2.0)
            road_geom_width.append((snapped_clipped, half_w))
        except Exception:
            continue
    road_edge_lines = _collect_connected_road_edge_lines(road_geom_width, snap_tol_m=road_snap_tol)
    # A road running right along the frontage would otherwise draw its near-side edge inside or
    # immediately alongside the actual property line - a near-duplicate dashed line right next to
    # the solid red boundary that reads as the boundary itself being broken/doubled. Only the
    # portion of a nearby road clearly outside the parcel (and a small buffer around it) is drawn.
    if road_edge_lines:
        boundary_exclusion = poly.buffer(max(1.0, (2.0 / 1000.0) * scale_ratio))
        clipped_edge_lines = []
        for line in road_edge_lines:
            # A road that actually crosses into/through the plot (not just running alongside or
            # near the boundary) is a genuine road crossing, not the "frontage duplicate line"
            # artifact this clip exists to fix - preserve it in full so real crossings still show,
            # rather than clipping every road edge that comes near the boundary indiscriminately.
            try:
                crosses_boundary = line.intersects(poly)
            except Exception:
                crosses_boundary = False
            if crosses_boundary:
                clipped_edge_lines.append(line)
                continue
            try:
                diff = line.difference(boundary_exclusion)
            except Exception:
                diff = line
            for part in _iter_line_geometries(diff):
                if part is not None and not getattr(part, "is_empty", True) and getattr(part, "length", 0.0) > 1.0:
                    clipped_edge_lines.append(part)
        road_edge_lines = clipped_edge_lines
    # The reference plan shows nearby roads as plain dashed reference lines, not solid double
    # lines - matching that convention here (Akwa Ibom/Rivers/Cross River do the same).
    _draw_road_edges(ax, road_edge_lines, font_scale=font_scale, color=road_color, linestyle=(0, (6, 4)), scale_ratio=scale_ratio, road_style=road_style)

    def _project_clip_named(geom, name):
        try:
            gdf_line = gpd.GeoSeries([geom], crs="EPSG:4326").to_crs(epsg=display_epsg)
            line_proj = gdf_line.iloc[0]
        except Exception:
            return None
        expanded_frame = extent_poly.buffer(road_snap_tol)
        clipped = line_proj.intersection(expanded_frame)
        if clipped.is_empty:
            return None
        return snap(clipped, extent_poly.boundary, road_snap_tol), name

    min_named_len = max(2.0, (10.0 / 1000.0) * scale_ratio)
    road_label_features = [r for r in (_project_clip_named(g, n) for g, n in road_add_named_overrides) if r and r[0].length > min_named_len]
    river_label_features = [r for r in (_project_clip_named(g, n) for g, n in river_add_named_overrides) if r and r[0].length > min_named_len]
    _draw_names_along_path(ax, road_label_features, color=road_color, font_scale=font_scale, base_fontsize=6.0)
    _draw_names_along_path(ax, river_label_features, color=river_color, font_scale=font_scale, base_fontsize=6.0)

    all_buildings = []
    if buildings:
        all_buildings.extend(buildings)
    if added_buildings:
        all_buildings.extend(added_buildings)
    # Skip real-world-tiny structures (a shed, a kiosk) that wouldn't render legibly at this
    # plan's scale - drawing every detected speck identically at 1:500 and 1:10000 alike isn't
    # how an accurate survey plan generalizes.
    visible_buildings = filter_features_by_scale(all_buildings, display_epsg, scale_ratio, min_paper_mm=2.0)
    if visible_buildings:
        draw_building_hatch(
            ax, visible_buildings, display_epsg, scale_ratio=scale_ratio, font_scale=font_scale,
            color=building_color, hatch_type=building_hatch_type,
        )
        gpd.GeoDataFrame(geometry=visible_buildings, crs="EPSG:4326").to_crs(epsg=display_epsg).plot(
            ax=ax, facecolor="none", edgecolor=building_color, lw=scaled_line_weight(0.2, font_scale, scale_ratio), zorder=8
        )
    if fences:
        draw_fences(ax, fences, display_epsg, scale_ratio=scale_ratio, font_scale=font_scale)
    fence_avoid_geom = build_fence_avoid_geom(fences, display_epsg=display_epsg, scale_ratio=scale_ratio)

    label_avoid_parts = [g for g in (fence_avoid_geom,) if g is not None]
    if all_buildings:
        try:
            buildings_buffer_m = max(1.0, (2.0 / 1000.0) * scale_ratio)
            buildings_proj = gpd.GeoSeries(all_buildings, crs="EPSG:4326").to_crs(epsg=display_epsg)
            buildings_avoid = unary_union(list(buildings_proj.buffer(buildings_buffer_m)))
            if buildings_avoid is not None and not buildings_avoid.is_empty:
                label_avoid_parts.append(buildings_avoid)
        except Exception:
            pass
    if road_edge_lines:
        try:
            road_buffer_m = max(1.0, (3.0 / 1000.0) * scale_ratio)
            roads_avoid = unary_union([seg.buffer(road_buffer_m) for seg in road_edge_lines])
            if roads_avoid is not None and not roads_avoid.is_empty:
                label_avoid_parts.append(roads_avoid)
        except Exception:
            pass
    label_avoid_geom = unary_union(label_avoid_parts) if label_avoid_parts else None

    # Gray-filled parcel (the reference plan's parcels are shaded, not left white) with the red
    # boundary outline on top - one draw call handles both.
    gdf_plot.plot(ax=ax, facecolor="none", edgecolor=boundary_color, lw=1.1 * font_scale, zorder=20)
    ax.set_xlim(target_xlim)
    ax.set_ylim(target_ylim)

    annotate_vertices(
        ax,
        poly,
        plot_id,
        station_names=station_names,
        font_scale=font_scale,
        min_label_length_m=0.0,
        avoid_geom=label_avoid_geom,
        scale_ratio=scale_ratio,
        boundary_poly=poly,
        beacon_style=beacon_style,
        text_color=text_color,
        boundary_color=bearing_text_color,
        station_font=station_font,
        station_size=station_size,
        bearing_font=bearing_font,
        bearing_size=bearing_size,
    )

    area_label_point = None
    try:
        from shapely.ops import polylabel as _polylabel
        area_label_point = _polylabel(poly, tolerance=1.0)
    except Exception:
        area_label_point = None
    if area_label_point is None or area_label_point.is_empty:
        try:
            area_label_point = poly.centroid
        except Exception:
            area_label_point = None
    if area_label_point is not None and not area_label_point.is_empty:
        # Below 1 hectare (10,000 sq m), express the area in square meters; at or above, switch
        # to hectares - the standard convention on Nigerian cadastral plans.
        area_text = f"{area_m2:,.2f} m²" if area_m2 < 10000 else f"{area_m2 / 10000.0:,.4f} Ha."
        ax.text(
            area_label_point.x, area_label_point.y - (target_ylim[1] - target_ylim[0]) * 0.02,
            f"{plot_no_value}\n{area_text}", ha="center", va="center",
            fontsize=max(6, int(8 * font_scale)), weight="bold", color=text_color,
            fontfamily=FCT_FONT_FAMILY, zorder=21, multialignment="center",
        )

    # The north arrow stands in the reserved right margin, beside the parcel, notably larger than
    # the other templates' arrows - matching the reference plan.
    axes_box = ax.get_position()
    arrow_x = min(0.92, axes_box.x1 + (0.985 - axes_box.x1) * 0.72)
    arrow_y = axes_box.y0 + axes_box.height * 0.30
    add_north_arrow(
        ax, font_scale=font_scale * 1.55, style=north_arrow_style, color=north_arrow_color,
        anchor_x=arrow_x, anchor_y=arrow_y,
    )

    ax.set_aspect("equal")
    ax.axis("off")
    fig.canvas.draw()

    first_coords = list(poly.exterior.coords)[0]
    first_station_name = str(station_names[0]).strip() if station_names and len(station_names) > 0 else "A"
    schedule_y = _draw_fct_scale_schedule(fig, 0.285, resolved_scale_text, font_scale=font_scale, text_color=text_color)

    beacon_rows = _compute_fct_beacon_schedule(poly, station_names=station_names)
    _draw_fct_beacon_table(fig, 0.05, schedule_y, 0.075, beacon_rows, font_scale=font_scale, text_color=text_color)
    _draw_fct_note_box(
        fig, 0.55, schedule_y,
        first_station_name=first_station_name, fct_cadastral_zone=fct_cadastral_zone,
        easting_m=first_coords[0], northing_m=first_coords[1], display_epsg=display_epsg,
        scale_text=resolved_scale_text, cadastral_map_ref=fct_cadastral_map_ref,
        font_scale=font_scale, text_color=text_color,
    )
    _draw_fct_footer(
        fig, 0.075, surveyor_name=surveyor_name, surveyor_rank=surveyor_rank,
        font_scale=font_scale, text_color=text_color,
    )

    fig.savefig(output_path, dpi=dpi)
    plt.close(fig)

# ======================
# Site Plan template
# ======================
# A compact single-page layout modeled on a real "Site Plan" reference document: a plain-language
# title block (no PLAN NO / coordinate-guide apparatus like the cadastral templates above), a
# small aerial-photo inset panel with the boundary overlaid, the vector boundary plot below it,
# and a footer showing the first beacon's UTM coordinate plus the surveyor's sign-off.

SITE_PLAN_FONT_FAMILY = "DejaVu Serif"


def _utm_zone_band_letter(lat_deg: float) -> str:
    """MGRS latitude band letter (e.g. the "P" in "zone 33P"). The cadastral templates only ever
    show a bare UTM zone number via `_resolve_cadastral_coordinate_system_text`; the Site Plan
    reference labels the zone with its band letter too, so this is new rather than reused.
    """
    bands = "CDEFGHJKLMNPQRSTUVWX"
    lat = float(lat_deg)
    if lat <= -80.0:
        return bands[0]
    if lat >= 84.0:
        return bands[-1]
    idx = int((lat + 80.0) // 8.0)
    idx = min(max(idx, 0), len(bands) - 1)
    return bands[idx]


def _format_site_plan_area(area_m2: float) -> str:
    """Compact "Area=2954.52Sqm" / "Area=1.2500Ha" style matching the reference document, using
    the same sub-1-hectare-switches-to-Sqm threshold as `format_area_display` elsewhere.
    """
    if area_m2 < 10000:
        return f"Area={area_m2:.2f}Sqm"
    return f"Area={area_m2 / 10000.0:.4f}Ha"


def _draw_site_plan_photo_inset(
    fig,
    rect: tuple[float, float, float, float],
    poly,
    display_epsg: int,
    boundary_color: str = "red",
    font_scale: float = 1.0,
    station_names=None,
    text_color: str = "black",
) -> None:
    """Small framed aerial-photo panel with the boundary overlaid in red - reuses the existing
    ArcGIS World Imagery fetch (`_try_add_arcgis_world_imagery`) plus the same Mapbox/Esri/OSM
    fallback chain used by the standalone orthophoto export, rather than reimplementing image
    fetching for this template. Always fetches at preview-grade quality (see below) regardless of
    the document's own preview/export mode, since this panel is too small to need export-grade
    imagery and that fetch is the slowest single step in producing a Site Plan.
    """
    x, y, w, h = rect
    ax_photo = fig.add_axes([x, y, w, h])
    fig_width, fig_height = fig.get_size_inches()
    # This is a small supplementary inset, not the document's main content - fetching it at the
    # full document dpi (up to export-quality 4096px-edge imagery) is far more data than a panel
    # this size can even show, and on a slow connection that's the single biggest thing standing
    # between clicking download and the file landing. Always request preview-grade imagery
    # (smaller max edge, no upscaling) at a capped effective dpi, regardless of the document's own
    # preview_mode - a small inset looks identical at print size either way.
    inset_fetch_dpi = 110
    inset_fetch_timeout = (4.0, 10.0)

    # The panel's own physical box is wide/short (matching the page layout), not square - fetching
    # a square-cropped bbox and letting set_aspect("equal") reconcile it afterward is what was
    # visibly stretching the imagery (ArcGIS was asked for a non-square pixel image over a square
    # bbox, distorting the source pixels themselves). Instead, pad the polygon's bounding box and
    # grow whichever axis is needed so the fetched bbox's own aspect ratio already matches the
    # panel's real physical aspect ratio - the imagery comes back undistorted, and the panel shows
    # more real-world width instead of empty margin either side.
    box_w_in = max(fig_width * w, 0.01)
    box_h_in = max(fig_height * h, 0.01)
    box_aspect = box_w_in / box_h_in
    minx, miny, maxx, maxy = poly.bounds
    cx, cy = (minx + maxx) / 2.0, (miny + maxy) / 2.0
    padded_w = max((maxx - minx) * 1.35, 1.0)
    padded_h = max((maxy - miny) * 1.35, 1.0)
    if padded_w / padded_h > box_aspect:
        padded_h = padded_w / box_aspect
    else:
        padded_w = padded_h * box_aspect
    target_xlim = (cx - padded_w / 2.0, cx + padded_w / 2.0)
    target_ylim = (cy - padded_h / 2.0, cy + padded_h / 2.0)
    ax_photo.set_xlim(target_xlim)
    ax_photo.set_ylim(target_ylim)
    basemap_loaded = _try_add_arcgis_world_imagery(
        ax_photo,
        target_xlim=target_xlim,
        target_ylim=target_ylim,
        axis_epsg=display_epsg,
        fig_width=fig_width,
        fig_height=fig_height,
        map_width_frac=w,
        map_height_frac=h,
        dpi=inset_fetch_dpi,
        preview_mode=True,
        timeout=inset_fetch_timeout,
    )
    axis_crs = f"EPSG:{display_epsg}"
    # A lower, fixed zoom (rather than scaling with the document's own preview/export mode) keeps
    # every fallback provider's tile count - and therefore fetch time - small and consistent.
    sat_zoom = 15
    if not basemap_loaded and MAPBOX_ACCESS_TOKEN:
        try:
            ctx.add_basemap(
                ax_photo, source=_mapbox_satellite_url(), crs=axis_crs, attribution=False,
                zoom=sat_zoom + 1, reset_extent=True, timeout=inset_fetch_timeout,
            )
            basemap_loaded = True
        except Exception:
            pass
    if not basemap_loaded:
        try:
            ctx.add_basemap(
                ax_photo, source=ctx.providers.Esri.WorldImagery, crs=axis_crs, attribution=False,
                zoom=sat_zoom, reset_extent=True, timeout=inset_fetch_timeout,
            )
            basemap_loaded = True
        except Exception:
            pass
    if not basemap_loaded:
        try:
            ctx.add_basemap(
                ax_photo, source=ctx.providers.OpenStreetMap.Mapnik, crs=axis_crs, attribution=False,
                zoom=sat_zoom, reset_extent=True, timeout=inset_fetch_timeout,
            )
        except Exception:
            pass

    ax_photo.set_xlim(target_xlim)
    ax_photo.set_ylim(target_ylim)
    coords, labels = _clockwise_ring_coords_and_labels(poly, station_names=station_names)
    xs, ys = zip(*coords)
    ax_photo.plot(xs, ys, color=boundary_color, lw=1.6 * font_scale, zorder=10)
    vertex_coords = coords[:-1] if len(coords) > 1 else coords
    label_offset = min(padded_w, padded_h) * 0.02
    for (vx, vy), label in zip(vertex_coords, labels):
        ax_photo.scatter([vx], [vy], s=16 * max(0.8, font_scale), marker="s", color=boundary_color, zorder=11)
        ax_photo.text(
            vx + label_offset, vy + label_offset, _safe_text(label, "-"),
            fontsize=max(6, int(6.5 * font_scale)), color=text_color, weight="bold",
            ha="left", va="bottom", zorder=12,
            path_effects=[patheffects.withStroke(linewidth=2.2, foreground="white")],
        )
    ax_photo.set_aspect("equal")
    ax_photo.set_xticks([])
    ax_photo.set_yticks([])
    for spine in ax_photo.spines.values():
        spine.set_edgecolor("black")
        spine.set_linewidth(1.1 * font_scale)


def _draw_site_plan_header(
    fig,
    applicant_name: str,
    location_text: str,
    lga_text: str,
    state_text: str,
    area_m2: float,
    font_scale: float = 1.0,
    text_color: str = "black",
    title_font: str | None = None,
    title_size: int | None = None,
    area_font: str | None = None,
    area_size: int | None = None,
) -> float:
    """Draws the reference document's title block: a single wrapped sentence ("SITE PLAN IN
    RESPECT OF ..., LOCATED AT ..., ... LOCAL GOVERNMENT AREA, ... STATE") followed by an
    underlined italic red area line. Returns the y-coordinate just below the block so the caller
    can position the photo inset beneath it.
    """
    fig.add_artist(patches.Rectangle((0.03, 0.03), 0.94, 0.94, transform=fig.transFigure, fill=False, lw=1.2))

    sentence = (
        f"SITE PLAN IN RESPECT OF {_safe_text(applicant_name, '-').upper()}, "
        f"LOCATED AT {_safe_text(location_text, '-').upper()}, "
        f"{_safe_text(lga_text, '-').upper()} LOCAL GOVERNMENT AREA, "
        f"{_safe_text(state_text, '-').upper()} STATE"
    )
    fs = title_size if title_size else max(8, int(10 * font_scale))
    family = title_font or SITE_PLAN_FONT_FAMILY
    lines = _wrap_figure_text(fig, sentence, width_fig=0.86, fontsize=fs, fontweight="bold", fontfamily=family) or [sentence]

    y = 0.955
    line_h = 0.024
    for line_text in lines:
        fig.text(
            0.5, y, line_text, ha="center", va="center", fontsize=fs, weight="bold",
            fontfamily=family, color=text_color,
        )
        y -= line_h

    y -= 0.006
    area_text = _format_site_plan_area(area_m2)
    area_fs = area_size if area_size else max(8, int(9.5 * font_scale))
    area_family = area_font or SITE_PLAN_FONT_FAMILY
    fig.text(
        0.5, y, area_text, ha="center", va="center", fontsize=area_fs, style="italic", weight="bold",
        fontfamily=area_family, color="red",
    )
    try:
        renderer = fig.canvas.get_renderer()
    except Exception:
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
    fp = FontProperties(size=area_fs, weight="bold", style="italic", family=area_family)
    try:
        text_w_px, _, _ = renderer.get_text_width_height_descent(area_text, fp, ismath=False)
        fig_w_px = max(float(fig.bbox.width), 1.0)
        half_w = (text_w_px / fig_w_px) / 2.0
    except Exception:
        half_w = 0.09
    underline_y = y - 0.012
    fig.add_artist(mlines.Line2D(
        [0.5 - half_w, 0.5 + half_w], [underline_y, underline_y], transform=fig.transFigure,
        color="red", lw=0.9 * font_scale,
    ))
    return underline_y - 0.02


def _draw_site_plan_footer(
    fig,
    first_easting_m: float,
    first_northing_m: float,
    first_station_name: str,
    display_epsg: int,
    utm_band_letter: str,
    surveyor_name: str,
    surveyor_rank: str,
    scale_text: str,
    font_scale: float = 1.0,
    text_color: str = "black",
    grid_color: str = CADASTRAL_BLUE,
) -> None:
    if 32600 < display_epsg < 32700:
        zone_number = display_epsg - 32600
    elif 32700 < display_epsg < 32800:
        zone_number = display_epsg - 32700
    else:
        zone_number = 0

    y_top = 0.155
    fs = max(7, int(8 * font_scale))
    fig.text(0.06, y_top, "WGS 84", fontsize=fs, weight="bold", color=grid_color, fontfamily=SITE_PLAN_FONT_FAMILY)
    fig.text(
        0.06, y_top - 0.022, f"UTM Coordinate (zone {zone_number}{utm_band_letter})",
        fontsize=fs, color=grid_color, fontfamily=SITE_PLAN_FONT_FAMILY,
    )
    fig.text(
        0.06, y_top - 0.044,
        f"{_safe_text(first_station_name, 'TP1')}:{first_easting_m:010.2f}Em,{first_northing_m:010.2f}Nm",
        fontsize=fs, color=grid_color, fontfamily=SITE_PLAN_FONT_FAMILY,
    )

    fig.text(
        0.06, y_top - 0.075, f"Surveyed by: {_safe_text(surveyor_name, '-')}"
        + (f", {_safe_text(surveyor_rank)}" if _safe_text(surveyor_rank) else ""),
        fontsize=fs, color=text_color, fontfamily=SITE_PLAN_FONT_FAMILY,
    )
    fig.text(0.06, y_top - 0.097, "Sign:____________________", fontsize=fs, color=text_color, fontfamily=SITE_PLAN_FONT_FAMILY)

    fig.text(
        0.94, y_top, f"Scale {_normalize_scale_label_adamawa(scale_text)}",
        fontsize=fs, ha="right", color=text_color, fontfamily=SITE_PLAN_FONT_FAMILY,
    )


def _render_plot_map_layout_site_plan(
    db,
    plot_id: int,
    output_path: str,
    title_text: str,
    location_text: str,
    lga_text: str,
    state_text: str,
    scale_text: str,
    surveyor_name: str,
    surveyor_rank: str,
    paper_size: str = "A4",
    station_names=None,
    coordinate_system: str = "wgs84",
    epsg_code: int = 4326,
    north_arrow_style: str = "one_side_stem",
    north_arrow_color: str = "black",
    beacon_style: str = "cross",
    road_width_m: float | None = None,
    road_width_override_m: float | None = None,
    preview_mode: bool = False,
    boundary_color: str | None = None,
    grid_color: str | None = None,
    text_color: str | None = None,
    road_color: str | None = None,
    river_color: str | None = None,
    building_color: str | None = None,
    building_hatch_type: str | None = None,
    road_style: str | None = None,
    title_font: str | None = None,
    title_size: int | None = None,
    station_font: str | None = None,
    station_size: int | None = None,
    bearing_font: str | None = None,
    bearing_size: int | None = None,
    area_font: str | None = None,
    area_size: int | None = None,
    measurement_polygon=None,
    measurement_area_m2: float | None = None,
):
    boundary_color = boundary_color or "red"
    grid_color = grid_color or CADASTRAL_BLUE
    text_color = text_color or "black"
    road_color = road_color or "black"
    river_color = river_color or "#10a3df"
    building_color = building_color or "black"
    building_hatch_type = building_hatch_type or "diagonal"
    road_style = road_style or ""

    plot_wkb = db.execute(text("SELECT geom FROM plots WHERE id=:id"), {"id": plot_id}).scalar()
    if not plot_wkb:
        raise ValueError("Plot not found")

    rows = db.execute(
        text("SELECT geom, feature_type FROM detected_features WHERE plot_id=:id"),
        {"id": plot_id},
    ).fetchall()
    override_rows = db.execute(
        text("""
            SELECT feature_type, action, name, width_m, ST_AsGeoJSON(geom) AS geojson
            FROM plot_feature_overrides
            WHERE plot_id = :id
        """),
        {"id": plot_id},
    ).fetchall()

    plot_geom = wkb.loads(plot_wkb)
    buildings, rivers, fences = [], [], []
    for r in rows:
        g = wkb.loads(r.geom)
        if r.feature_type == "building":
            buildings.append(g)
        elif r.feature_type == "river":
            rivers.append(g)
        elif r.feature_type == "fence":
            fences.append(g)
    detected_roads = _fetch_live_road_geoms(db, plot_id)

    overrides = []
    import json
    for r in override_rows:
        geom = None
        if r.geojson:
            try:
                geom = shape(json.loads(r.geojson))
            except Exception:
                geom = None
        overrides.append({"feature_type": r.feature_type, "action": r.action, "name": r.name, "geom": geom})

    def apply_overrides(base_list, feature_type: str):
        result = list(base_list)
        added = []
        delete_geoms = []
        use_coverage_match = feature_type in ("road", "river", "fence")
        for ov in overrides:
            if ov["feature_type"] != feature_type:
                continue
            geom = ov["geom"]
            if geom is None:
                continue
            try:
                if hasattr(geom, "is_valid") and not geom.is_valid:
                    geom = geom.buffer(0)
            except Exception:
                pass
            if ov["action"] in ("delete", "update"):
                if use_coverage_match:
                    result = [g for g in result if not _feature_override_replaces_native(g, geom, feature_type)]
                else:
                    result = [g for g in result if not g.intersects(geom)]
                delete_geoms.append(geom)
            if ov["action"] in ("add", "update"):
                result.append(geom)
                added.append(geom)
        if delete_geoms:
            if use_coverage_match:
                added = [
                    g for g in added
                    if not any(_feature_override_replaces_native(g, dg, feature_type) for dg in delete_geoms)
                ]
            else:
                added = [g for g in added if not any(g.intersects(dg) for dg in delete_geoms)]
        return result, added

    buildings, added_buildings = apply_overrides(buildings, "building")
    rivers, _ = apply_overrides(rivers, "river")
    fences, _ = apply_overrides(fences, "fence")
    roads_for_draw, _ = apply_overrides(detected_roads, "road")
    road_add_named_overrides = _resolve_override_names(overrides, "road")
    river_add_named_overrides = _resolve_override_names(overrides, "river")

    display_epsg = epsg_code
    if coordinate_system == "wgs84" or epsg_code == 4326:
        centroid = plot_geom.centroid
        utm_zone = int((centroid.x + 180) / 6) + 1
        hemisphere = "north" if centroid.y >= 0 else "south"
        display_epsg = 32600 + utm_zone if hemisphere == "north" else 32700 + utm_zone

    if measurement_polygon is not None:
        poly = measurement_polygon
        if not poly.is_valid:
            poly = poly.buffer(0)
        gdf_plot = gpd.GeoDataFrame(geometry=[poly], crs=f"EPSG:{display_epsg}")
    else:
        gdf_plot = gpd.GeoDataFrame(geometry=[plot_geom], crs="EPSG:4326").to_crs(epsg=display_epsg)
        poly = gdf_plot.geometry.iloc[0]
        if not poly.is_valid:
            poly = poly.buffer(0)
            gdf_plot = gpd.GeoDataFrame(geometry=[poly], crs=f"EPSG:{display_epsg}")
    area_m2 = float(measurement_area_m2) if measurement_area_m2 is not None else float(poly.area)

    vertex_count = max(0, len(list(poly.exterior.coords)) - 1)
    if not station_names:
        station_names = [f"TP{i + 1}" for i in range(vertex_count)]

    paper_config = get_paper_config(paper_size)
    fig_width = paper_config["width"]
    fig_height = paper_config["height"]
    font_scale = paper_config["scale"]
    dpi = 150 if preview_mode else 200

    fig = plt.figure(figsize=(fig_width, fig_height), dpi=dpi)
    _ = FigureCanvas(fig)

    header_bottom_y = _draw_site_plan_header(
        fig,
        applicant_name=title_text,
        location_text=location_text,
        lga_text=lga_text,
        state_text=state_text,
        area_m2=float(area_m2 or 0),
        font_scale=font_scale,
        text_color=text_color,
        title_font=title_font,
        title_size=title_size,
        area_font=area_font,
        area_size=area_size,
    )

    map_left, map_width = 0.08, 0.84
    footer_top_y = 0.19
    map_bottom = footer_top_y
    gap = 0.02
    # The photo gets priority on space here - the scale-fit safety net below (compute_fit_scale_ratio
    # fallback) guarantees the vector map's true-scale render can never exceed whatever box it's
    # given, so a small map_height floor no longer risks annotate_vertices' bearing/distance labels
    # (plain ax.text calls, unclipped by default) spilling past the axes into the footer.
    min_map_height = 0.28
    photo_w = map_width
    photo_x = map_left
    photo_h = max(0.16, min(0.45, header_bottom_y - gap - min_map_height - gap - map_bottom))
    photo_y = header_bottom_y - photo_h
    _draw_site_plan_photo_inset(
        fig,
        rect=(photo_x, photo_y, photo_w, photo_h),
        poly=poly,
        display_epsg=display_epsg,
        boundary_color=boundary_color,
        font_scale=font_scale,
        station_names=station_names,
        text_color=text_color,
    )

    map_top = photo_y - gap
    map_height = max(min_map_height, map_top - map_bottom)
    ax = fig.add_axes([map_left, map_bottom, map_width, map_height])

    resolved_scale_text, scale_ratio = resolve_scale_text_and_ratio(
        scale_text, poly, fig_width * map_width, fig_height * map_height,
    )
    # Safety net: an explicit user-chosen scale (e.g. matching a reference document's stated
    # "1:550") has no relationship to how much of the page site_plan's smaller map area actually
    # has left after the photo panel above it - if that scale's real-world footprint wouldn't fit
    # the box, silently widen to whatever scale does fit (same fallback compute_fit_scale_ratio
    # already provides for "auto"), so annotate_vertices' labels can never be pushed outside the
    # axes regardless of how the photo/map space is split.
    minx, miny, maxx, maxy = poly.bounds
    inch_to_m = 0.0254
    capacity_w_m = (fig_width * map_width) * inch_to_m * scale_ratio
    capacity_h_m = (fig_height * map_height) * inch_to_m * scale_ratio
    if (maxx - minx) > capacity_w_m * 0.92 or (maxy - miny) > capacity_h_m * 0.92:
        fit_ratio = compute_fit_scale_ratio(poly, fig_width * map_width, fig_height * map_height)
        if fit_ratio > scale_ratio:
            scale_ratio = fit_ratio
            resolved_scale_text = f"1 : {fit_ratio}"

    apply_true_scale(ax, poly, scale_ratio, fig_width * map_width, fig_height * map_height)
    target_xlim = ax.get_xlim()
    target_ylim = ax.get_ylim()
    from shapely.geometry import box
    extent_poly = box(target_xlim[0], target_ylim[0], target_xlim[1], target_ylim[1])

    visible_rivers = filter_features_by_scale(rivers, display_epsg, scale_ratio, min_paper_mm=2.0)
    if visible_rivers:
        gpd.GeoDataFrame(geometry=visible_rivers, crs="EPSG:4326").to_crs(epsg=display_epsg).plot(
            ax=ax, color=river_color, lw=scaled_line_weight(0.3, font_scale, scale_ratio), zorder=5
        )

    road_geom_width = []
    road_snap_tol = max(1.0, (5.0 / 1000.0) * scale_ratio)
    for geom in roads_for_draw:
        if geom is None:
            continue
        try:
            gdf_line = gpd.GeoSeries([geom], crs="EPSG:4326").to_crs(epsg=display_epsg)
            line_proj = gdf_line.iloc[0]
        except Exception:
            continue
        expanded_frame = extent_poly.buffer(road_snap_tol)
        clipped = line_proj.intersection(expanded_frame)
        if clipped.is_empty:
            continue
        snapped_clipped = snap(clipped, extent_poly.boundary, road_snap_tol)
        try:
            half_w = max(1.0, (road_width_m or 3.0) / 2.0)
            road_geom_width.append((snapped_clipped, half_w))
        except Exception:
            continue
    road_edge_lines = _collect_connected_road_edge_lines(road_geom_width, snap_tol_m=road_snap_tol)
    _draw_road_edges(ax, road_edge_lines, font_scale=font_scale, color=road_color, linestyle=(0, (6, 4)), scale_ratio=scale_ratio, road_style=road_style)

    if _safe_text(location_text):
        try:
            _draw_cadastral_frontage_road(ax, poly, location_text, font_scale=font_scale, color=road_color)
        except Exception:
            pass

    def _project_clip_named(geom, name):
        try:
            gdf_line = gpd.GeoSeries([geom], crs="EPSG:4326").to_crs(epsg=display_epsg)
            line_proj = gdf_line.iloc[0]
        except Exception:
            return None
        expanded_frame = extent_poly.buffer(road_snap_tol)
        clipped = line_proj.intersection(expanded_frame)
        if clipped.is_empty:
            return None
        return snap(clipped, extent_poly.boundary, road_snap_tol), name

    min_named_len = max(2.0, (10.0 / 1000.0) * scale_ratio)
    road_label_features = [r for r in (_project_clip_named(g, n) for g, n in road_add_named_overrides) if r and r[0].length > min_named_len]
    river_label_features = [r for r in (_project_clip_named(g, n) for g, n in river_add_named_overrides) if r and r[0].length > min_named_len]
    _draw_names_along_path(ax, road_label_features, color=road_color, font_scale=font_scale, base_fontsize=6.0)
    _draw_names_along_path(ax, river_label_features, color=river_color, font_scale=font_scale, base_fontsize=6.0)

    all_buildings = []
    if buildings:
        all_buildings.extend(buildings)
    if added_buildings:
        all_buildings.extend(added_buildings)
    visible_buildings = filter_features_by_scale(all_buildings, display_epsg, scale_ratio, min_paper_mm=2.0)
    if visible_buildings:
        draw_building_hatch(
            ax, visible_buildings, display_epsg, scale_ratio=scale_ratio, font_scale=font_scale,
            color=building_color, hatch_type=building_hatch_type,
        )
        gpd.GeoDataFrame(geometry=visible_buildings, crs="EPSG:4326").to_crs(epsg=display_epsg).plot(
            ax=ax, facecolor="none", edgecolor=building_color, lw=scaled_line_weight(0.2, font_scale, scale_ratio), zorder=8
        )
    if fences:
        draw_fences(ax, fences, display_epsg, scale_ratio=scale_ratio, font_scale=font_scale)
    fence_avoid_geom = build_fence_avoid_geom(fences, display_epsg=display_epsg, scale_ratio=scale_ratio)

    label_avoid_parts = [g for g in (fence_avoid_geom,) if g is not None]
    if all_buildings:
        try:
            buildings_buffer_m = max(1.0, (2.0 / 1000.0) * scale_ratio)
            buildings_proj = gpd.GeoSeries(all_buildings, crs="EPSG:4326").to_crs(epsg=display_epsg)
            buildings_avoid = unary_union(list(buildings_proj.buffer(buildings_buffer_m)))
            if buildings_avoid is not None and not buildings_avoid.is_empty:
                label_avoid_parts.append(buildings_avoid)
        except Exception:
            pass
    if road_edge_lines:
        try:
            road_buffer_m = max(1.0, (3.0 / 1000.0) * scale_ratio)
            roads_avoid = unary_union([seg.buffer(road_buffer_m) for seg in road_edge_lines])
            if roads_avoid is not None and not roads_avoid.is_empty:
                label_avoid_parts.append(roads_avoid)
        except Exception:
            pass
    label_avoid_geom = unary_union(label_avoid_parts) if label_avoid_parts else None

    gdf_plot.plot(ax=ax, facecolor="none", edgecolor=boundary_color, lw=1.1 * font_scale, zorder=20)
    ax.set_xlim(target_xlim)
    ax.set_ylim(target_ylim)

    annotate_vertices(
        ax, poly, plot_id, station_names=station_names, font_scale=font_scale, min_label_length_m=0.0,
        avoid_geom=label_avoid_geom, scale_ratio=scale_ratio, boundary_poly=poly, beacon_style=beacon_style,
        text_color=text_color, boundary_color=boundary_color, station_font=station_font, station_size=station_size,
        bearing_font=bearing_font, bearing_size=bearing_size,
    )

    axes_box = ax.get_position()
    # A touch right of the map/photo panels' shared right edge (both are map_width wide) so the
    # arrow clears the photo panel's frame border instead of sitting right on top of it.
    arrow_x = min(0.965, axes_box.x1 + 0.025)
    arrow_y = min(0.93, axes_box.y1 + 0.045)
    add_north_arrow(
        ax, font_scale=font_scale * 1.1, style=north_arrow_style, color=north_arrow_color,
        anchor_x=arrow_x, anchor_y=arrow_y, blue_hex=grid_color,
    )

    ax.set_aspect("equal")
    ax.axis("off")
    fig.canvas.draw()

    first_coords = list(poly.exterior.coords)[0]
    first_station_name = str(station_names[0]).strip() if station_names else "TP1"
    try:
        first_point_wgs84 = gpd.GeoSeries(
            [Point(first_coords[0], first_coords[1])], crs=f"EPSG:{display_epsg}"
        ).to_crs(epsg=4326).iloc[0]
        first_lat = first_point_wgs84.y
    except Exception:
        first_lat = plot_geom.centroid.y
    utm_band_letter = _utm_zone_band_letter(first_lat)

    _draw_site_plan_footer(
        fig,
        first_easting_m=first_coords[0],
        first_northing_m=first_coords[1],
        first_station_name=first_station_name,
        display_epsg=display_epsg,
        utm_band_letter=utm_band_letter,
        surveyor_name=surveyor_name,
        surveyor_rank=surveyor_rank,
        scale_text=resolved_scale_text,
        font_scale=font_scale,
        text_color=text_color,
        grid_color=grid_color,
    )

    fig.savefig(output_path, dpi=dpi)
    plt.close(fig)


# ======================
# Main Renderer Function
# ======================

def render_plot_map_layout(
    db,
    plot_id: int,
    output_path: str,
    title_text: str = "SURVEY PLAN",
    location_text: str = "LOC",
    lga_text: str = "LGA",
    state_text: str = "STATE",
    scale_text: str = "1 : 1000",
    crs_footer_text: str = "ORIGIN: WGS84",
    source_footer_text: str = "SOURCE: LandCheck",
    surveyor_name: str = "SURV",
    surveyor_rank: str = "RANK",
    certification_statement: str | None = None,
    station_names=None,
    coordinate_system: str = "wgs84",
    epsg_code: int = 4326,
    paper_size: str = "A4",
    north_arrow_style: str = "one_side_stem",
    north_arrow_color: str = "black",
    beacon_style: str = "cross",
    road_width_m: float | None = None,
    road_width_override_m: float | None = None,
    preview_mode: bool = False,
    template_name: str = "general",
    adamawa_rof_no: str = "",
    adamawa_owner_name: str = "",
    adamawa_authority_title: str = DEFAULT_ADAMAWA_AUTHORITY_TITLE,
    adamawa_authority_date_text: str = DEFAULT_ADAMAWA_AUTHORITY_DATE,
    adamawa_control_point_name: str = "",
    adamawa_northing: str = "",
    adamawa_easting: str = "",
    adamawa_elevation: str = "",
    adamawa_origin_text: str = DEFAULT_ADAMAWA_ORIGIN_TEXT,
    adamawa_topo_sheet_text: str = DEFAULT_ADAMAWA_TOPO_SHEET_TEXT,
    adamawa_computation_no: str = "",
    adamawa_cadastral_sheet_no: str = "",
    adamawa_plan_no: str = "",
    adamawa_surveyed_by_text: str = "",
    adamawa_disclaimer_text: str = DEFAULT_ADAMAWA_DISCLAIMER_TEXT,
    cadastral_plan_no: str = "",
    cadastral_area_name: str = "",
    cadastral_datum_text: str = "",
    cadastral_firm_block_text: str = "",
    fct_file_no: str = "",
    fct_district: str = "",
    fct_cadastral_zone: str = "",
    fct_origin_beacon_text: str = "",
    fct_cadastral_map_ref: str = "",
    fct_title_prefix: str = "",
    boundary_color: str | None = None,
    grid_color: str | None = None,
    text_color: str | None = None,
    road_color: str | None = None,
    river_color: str | None = None,
    building_color: str | None = None,
    building_hatch_type: str | None = None,
    road_style: str | None = None,
    title_font: str | None = None,
    title_size: int | None = None,
    grid_font: str | None = None,
    grid_size: int | None = None,
    station_font: str | None = None,
    station_size: int | None = None,
    bearing_font: str | None = None,
    bearing_size: int | None = None,
    area_font: str | None = None,
    area_size: int | None = None,
    measurement_polygon=None,
    measurement_area_m2: float | None = None,
):
    normalized_template = str(template_name or "general").strip().lower()
    if normalized_template in CADASTRAL_STATE_LABELS:
        _render_plot_map_layout_cadastral(
            db=db,
            plot_id=plot_id,
            output_path=output_path,
            title_text=title_text,
            location_text=location_text,
            lga_text=lga_text,
            state_text=state_text,
            scale_text=scale_text,
            surveyor_name=surveyor_name,
            surveyor_rank=surveyor_rank,
            paper_size=paper_size,
            station_names=station_names,
            coordinate_system=coordinate_system,
            epsg_code=epsg_code,
            north_arrow_style=north_arrow_style,
            north_arrow_color=north_arrow_color,
            beacon_style=beacon_style,
            road_width_m=road_width_m,
            road_width_override_m=road_width_override_m,
            cadastral_plan_no=cadastral_plan_no,
            cadastral_area_name=cadastral_area_name,
            cadastral_datum_text=cadastral_datum_text,
            cadastral_firm_block_text=cadastral_firm_block_text,
            state_label=CADASTRAL_STATE_LABELS[normalized_template],
            preview_mode=preview_mode,
            boundary_color=boundary_color,
            grid_color=grid_color,
            text_color=text_color,
            road_color=road_color,
            river_color=river_color,
            building_color=building_color,
            building_hatch_type=building_hatch_type,
            road_style=road_style,
            title_font=title_font,
            title_size=title_size,
            grid_font=grid_font,
            grid_size=grid_size,
            station_font=station_font,
            station_size=station_size,
            bearing_font=bearing_font,
            bearing_size=bearing_size,
            area_font=area_font,
            area_size=area_size,
            measurement_polygon=measurement_polygon,
            measurement_area_m2=measurement_area_m2,
        )
        return

    if normalized_template == "fct_abuja_osg":
        _render_plot_map_layout_fct(
            db=db,
            plot_id=plot_id,
            output_path=output_path,
            title_text=title_text,
            lga_text=lga_text,
            state_text=state_text,
            scale_text=scale_text,
            surveyor_name=surveyor_name,
            surveyor_rank=surveyor_rank,
            paper_size=paper_size,
            station_names=station_names,
            coordinate_system=coordinate_system,
            epsg_code=epsg_code,
            north_arrow_style=north_arrow_style,
            north_arrow_color=north_arrow_color,
            beacon_style=beacon_style,
            road_width_m=road_width_m,
            road_width_override_m=road_width_override_m,
            cadastral_plan_no=cadastral_plan_no,
            fct_file_no=fct_file_no,
            fct_district=fct_district,
            fct_cadastral_zone=fct_cadastral_zone,
            fct_origin_beacon_text=fct_origin_beacon_text,
            fct_cadastral_map_ref=fct_cadastral_map_ref,
            fct_title_prefix=fct_title_prefix,
            preview_mode=preview_mode,
            boundary_color=boundary_color,
            grid_color=grid_color,
            text_color=text_color,
            road_color=road_color,
            river_color=river_color,
            building_color=building_color,
            building_hatch_type=building_hatch_type,
            road_style=road_style,
            title_font=title_font,
            title_size=title_size,
            grid_font=grid_font,
            grid_size=grid_size,
            station_font=station_font,
            station_size=station_size,
            bearing_font=bearing_font,
            bearing_size=bearing_size,
            area_font=area_font,
            area_size=area_size,
            measurement_polygon=measurement_polygon,
            measurement_area_m2=measurement_area_m2,
        )
        return

    if normalized_template == "adamawa_osg":
        _render_plot_map_layout_adamawa(
            db=db,
            plot_id=plot_id,
            output_path=output_path,
            title_text=title_text,
            location_text=location_text,
            lga_text=lga_text,
            scale_text=scale_text,
            surveyor_name=surveyor_name,
            surveyor_rank=surveyor_rank,
            paper_size=paper_size,
            station_names=station_names,
            coordinate_system=coordinate_system,
            epsg_code=epsg_code,
            north_arrow_style=north_arrow_style,
            north_arrow_color=north_arrow_color,
            beacon_style=beacon_style,
            road_width_m=road_width_m,
            road_width_override_m=road_width_override_m,
            adamawa_rof_no=adamawa_rof_no,
            adamawa_owner_name=adamawa_owner_name,
            adamawa_authority_title=adamawa_authority_title,
            adamawa_authority_date_text=adamawa_authority_date_text,
            adamawa_control_point_name=adamawa_control_point_name,
            adamawa_northing=adamawa_northing,
            adamawa_easting=adamawa_easting,
            adamawa_elevation=adamawa_elevation,
            adamawa_origin_text=adamawa_origin_text,
            adamawa_topo_sheet_text=adamawa_topo_sheet_text,
            adamawa_computation_no=adamawa_computation_no,
            adamawa_cadastral_sheet_no=adamawa_cadastral_sheet_no,
            adamawa_plan_no=adamawa_plan_no,
            adamawa_surveyed_by_text=adamawa_surveyed_by_text,
            adamawa_disclaimer_text=adamawa_disclaimer_text,
            preview_mode=preview_mode,
            boundary_color=boundary_color,
            grid_color=grid_color,
            text_color=text_color,
            road_color=road_color,
            river_color=river_color,
            building_color=building_color,
            building_hatch_type=building_hatch_type,
            road_style=road_style,
            title_font=title_font,
            title_size=title_size,
            grid_font=grid_font,
            grid_size=grid_size,
            station_font=station_font,
            station_size=station_size,
            bearing_font=bearing_font,
            bearing_size=bearing_size,
            area_font=area_font,
            area_size=area_size,
            measurement_polygon=measurement_polygon,
            measurement_area_m2=measurement_area_m2,
        )
        return

    if normalized_template == "site_plan":
        _render_plot_map_layout_site_plan(
            db=db,
            plot_id=plot_id,
            output_path=output_path,
            title_text=title_text,
            location_text=location_text,
            lga_text=lga_text,
            state_text=state_text,
            scale_text=scale_text,
            surveyor_name=surveyor_name,
            surveyor_rank=surveyor_rank,
            paper_size=paper_size,
            station_names=station_names,
            coordinate_system=coordinate_system,
            epsg_code=epsg_code,
            north_arrow_style=north_arrow_style,
            north_arrow_color=north_arrow_color,
            beacon_style=beacon_style,
            road_width_m=road_width_m,
            road_width_override_m=road_width_override_m,
            preview_mode=preview_mode,
            boundary_color=boundary_color,
            grid_color=grid_color,
            text_color=text_color,
            road_color=road_color,
            river_color=river_color,
            building_color=building_color,
            building_hatch_type=building_hatch_type,
            road_style=road_style,
            title_font=title_font,
            title_size=title_size,
            station_font=station_font,
            station_size=station_size,
            bearing_font=bearing_font,
            bearing_size=bearing_size,
            area_font=area_font,
            area_size=area_size,
            measurement_polygon=measurement_polygon,
            measurement_area_m2=measurement_area_m2,
        )
        return

    # None means "not overridden" - fall back to the general template's own established
    # defaults so omitting these params leaves existing renders looking exactly as they do today.
    boundary_color = boundary_color or "red"
    grid_color = grid_color or "blue"
    text_color = text_color or "black"
    road_color = road_color or "black"
    river_color = river_color or "blue"
    building_color = building_color or "black"
    building_hatch_type = building_hatch_type or "diagonal"
    road_style = road_style or ""

    plot_wkb = db.execute(text("SELECT geom FROM plots WHERE id=:id"), {"id": plot_id}).scalar()
    rows = db.execute(
        text("SELECT geom, feature_type FROM detected_features WHERE plot_id=:id"),
        {"id": plot_id},
    ).fetchall()
    override_rows = db.execute(
        text("""
            SELECT feature_type, action, name, width_m, ST_AsGeoJSON(geom) AS geojson
            FROM plot_feature_overrides
            WHERE plot_id = :id
        """),
        {"id": plot_id},
    ).fetchall()

    if not plot_wkb:
        raise ValueError("Plot not found")


    plot_geom = wkb.loads(plot_wkb)
    buildings, rivers, fences, detected_roads = [], [], [], []
    for r in rows:
        g = wkb.loads(r.geom)
        if r.feature_type == "building":
            buildings.append(g)
        elif r.feature_type == "river":
            rivers.append(g)
        elif r.feature_type == "fence":
            fences.append(g)
        elif r.feature_type == "road":
            detected_roads.append(g)

    overrides = []
    import json
    for r in override_rows:
        geom = None
        if r.geojson:
            try:
                geom = shape(json.loads(r.geojson))
            except Exception:
                geom = None
        overrides.append({
            "feature_type": r.feature_type,
            "action": r.action,
            "name": r.name,
            "width_m": r.width_m if hasattr(r, "width_m") else None,
            "geom": geom,
        })

    def apply_overrides(base_list, feature_type: str):
        result = list(base_list)
        added = []
        delete_geoms = []
        use_coverage_match = feature_type in ("road", "river", "fence")
        for ov in overrides:
            if ov["feature_type"] != feature_type:
                continue
            geom = ov["geom"]
            if geom is None:
                continue
            # Fix invalid polygons from user edits
            try:
                if hasattr(geom, "is_valid") and not geom.is_valid:
                    geom = geom.buffer(0)
            except Exception:
                pass
            if ov["action"] in ("delete", "update"):
                if use_coverage_match:
                    result = [g for g in result if not _feature_override_replaces_native(g, geom, feature_type)]
                else:
                    result = [g for g in result if not g.intersects(geom)]
                delete_geoms.append(geom)
            if ov["action"] in ("add", "update"):
                result.append(geom)
                added.append(geom)
        if delete_geoms:
            if use_coverage_match:
                added = [
                    g for g in added
                    if not any(_feature_override_replaces_native(g, dg, feature_type) for dg in delete_geoms)
                ]
            else:
                added = [g for g in added if not any(g.intersects(dg) for dg in delete_geoms)]
        return result, added

    buildings, added_buildings = apply_overrides(buildings, "building")
    rivers, added_rivers = apply_overrides(rivers, "river")
    fences, added_fences = apply_overrides(fences, "fence")
    roads_for_preview, _ = apply_overrides(detected_roads, "road")

    # Use user's selected coordinate system for rendering
    # If WGS84 selected, use appropriate UTM zone for projected display
    display_epsg = epsg_code
    if coordinate_system == "wgs84" or epsg_code == 4326:
        # Auto-detect UTM zone for display
        centroid = plot_geom.centroid
        utm_zone = int((centroid.x + 180) / 6) + 1
        hemisphere = "north" if centroid.y >= 0 else "south"
        display_epsg = 32600 + utm_zone if hemisphere == "north" else 32700 + utm_zone

    if measurement_polygon is not None:
        poly = measurement_polygon
        if not poly.is_valid:
            poly = poly.buffer(0)
        gdf_plot = gpd.GeoDataFrame(geometry=[poly], crs=f"EPSG:{display_epsg}")
    else:
        gdf_plot = gpd.GeoDataFrame(geometry=[plot_geom], crs="EPSG:4326").to_crs(epsg=display_epsg)
        poly = gdf_plot.geometry.iloc[0]

        # Fix invalid/self-intersecting polygons
        if not poly.is_valid:
            poly = poly.buffer(0)
            gdf_plot = gpd.GeoDataFrame(geometry=[poly], crs=f"EPSG:{display_epsg}")
    area_m2 = float(measurement_area_m2) if measurement_area_m2 is not None else float(poly.area)

    # Get paper configuration
    paper_config = get_paper_config(paper_size)
    fig_width = paper_config["width"]
    fig_height = paper_config["height"]
    font_scale = paper_config["scale"]

    # Preview renders can use lower DPI to return quickly while preserving export quality.
    if preview_mode:
        dpi = 120
    else:
        # Adjust DPI based on paper size (larger papers need lower DPI for reasonable file sizes)
        dpi = 200 if paper_size in ["A4", "A3"] else 150 if paper_size == "A2" else 100

    fig = plt.figure(figsize=(fig_width, fig_height), dpi=dpi)
    canvas_obj = FigureCanvas(fig)

    map_left, map_bottom, map_width, map_height = 0.10, 0.30, 0.80, 0.45
    ax = fig.add_axes([map_left, map_bottom, map_width, map_height])

    resolved_scale_text, scale_ratio = resolve_scale_text_and_ratio(
        scale_text,
        poly,
        fig_width * map_width,
        fig_height * map_height,
    )

    draw_sheet_frame(fig)
    draw_title_block(
        fig, title_text, plot_id, area_m2, resolved_scale_text, location_text, lga_text, state_text, font_scale,
        title_font=title_font, title_size=title_size, area_font=area_font, area_size=area_size,
        text_color=text_color,
    )
    draw_footer(fig, crs_footer_text, source_footer_text, surveyor_name, surveyor_rank, font_scale, text_color=text_color)

    apply_true_scale(ax, poly, scale_ratio, fig_width * map_width, fig_height * map_height)
    target_xlim = ax.get_xlim()
    target_ylim = ax.get_ylim()

    min_label_mm = 12
    min_label_length_m = (min_label_mm / 1000.0) * scale_ratio

    # flags for KEY (only show what exists)
    has_buildings = len(buildings) > 0
    has_rivers = len(rivers) > 0
    has_fences = len(fences) > 0

    # Drop river segments too short to read as anything but a stray mark at this plan's scale.
    visible_rivers = filter_features_by_scale(rivers, display_epsg, scale_ratio, min_paper_mm=2.0)
    if visible_rivers:
        gpd.GeoDataFrame(geometry=visible_rivers, crs="EPSG:4326").to_crs(epsg=display_epsg).plot(
            ax=ax, color=river_color, lw=scaled_line_weight(0.3, font_scale, scale_ratio), zorder=5
        )

    from shapely.geometry import box

    extent_poly = box(target_xlim[0], target_ylim[0], target_xlim[1], target_ylim[1])
    road_edge_lines = []
    road_geom_width = []
    road_label_features = []
    road_snap_tol = max(1.0, (5.0 / 1000.0) * scale_ratio)
    road_add_geoms = [
        ov for ov in overrides
        if ov["feature_type"] == "road" and ov["action"] in ("add", "update") and ov["geom"] is not None
    ]
    if preview_mode:
        for geom in roads_for_preview:
            try:
                gdf_line = gpd.GeoSeries([geom], crs="EPSG:4326").to_crs(epsg=display_epsg)
                line_proj = gdf_line.iloc[0]
            except Exception:
                continue
            expanded_frame = extent_poly.buffer(road_snap_tol)
            clipped = line_proj.intersection(expanded_frame)
            if clipped.is_empty:
                continue
            snapped_clipped = snap(clipped, extent_poly.boundary, road_snap_tol)
            try:
                half_w = max(1.0, (road_width_m or 3.0) / 2.0)
                road_geom_width.append((snapped_clipped, half_w))
            except Exception:
                continue
        # In edit/preview mode, include labels for manually added roads.
        for ov in road_add_geoms:
            name = str(ov.get("name") or "").strip()
            if not name:
                continue
            geom = ov.get("geom")
            if geom is None:
                continue
            try:
                gdf_line = gpd.GeoSeries([geom], crs="EPSG:4326").to_crs(epsg=display_epsg)
                line_proj = gdf_line.iloc[0]
            except Exception:
                continue
            expanded_frame = extent_poly.buffer(road_snap_tol)
            clipped = line_proj.intersection(expanded_frame)
            if clipped.is_empty:
                continue
            snapped_clipped = snap(clipped, extent_poly.boundary, road_snap_tol)
            road_label_features.append((snapped_clipped, name, "override"))
        road_edge_lines = _collect_connected_road_edge_lines(road_geom_width, snap_tol_m=road_snap_tol)
        has_roads = len(road_edge_lines) > 0
    else:
        # Draw roads with class-based real-world widths
        road_rows = db.execute(text("""
            WITH roads AS (
                SELECT
                    CASE
                        WHEN ST_SRID(r.geom) = 4326 THEN r.geom
                        WHEN ST_SRID(r.geom) = 0 THEN ST_SetSRID(r.geom, 4326)
                        ELSE ST_Transform(r.geom, 4326)
                    END AS geom,
                    r.highway,
                    r.name
                FROM lines r
                WHERE r.highway IS NOT NULL
            )
            SELECT roads.geom, roads.highway, roads.name
            FROM roads
            JOIN plot_buffers b ON b.plot_id = :plot_id
            WHERE ST_Intersects(roads.geom, b.geom)
        """), {"plot_id": plot_id}).fetchall()

        # A road covered by an "update" override must be skipped here too, not just "delete" -
        # otherwise the override's replacement geometry gets drawn ON TOP OF the original
        # live-queried segment instead of replacing it, and the two overlapping-but-not-identical
        # buffered outlines union into a visibly thicker/blobbier road than the selected width
        # (this is what made a road look oversized right after naming it from the Road Names
        # panel, since naming saves an "update" override with the same road's geometry).
        road_replaced_geoms = [
            ov["geom"] for ov in overrides
            if ov["feature_type"] == "road" and ov["action"] in ("delete", "update") and ov["geom"] is not None
        ]
        for row in road_rows:
            geom = wkb.loads(row.geom)
            highway = row.highway
            name = row.name
            if _road_segment_replaced(geom, road_replaced_geoms, display_epsg):
                continue
            try:
                gdf_line = gpd.GeoSeries([geom], crs="EPSG:4326").to_crs(epsg=display_epsg)
                line_proj = gdf_line.iloc[0]
            except Exception:
                continue

            expanded_frame = extent_poly.buffer(road_snap_tol)
            clipped = line_proj.intersection(expanded_frame)
            if clipped.is_empty:
                continue
            # Snap to frame boundary so buffered road edges reach the grid border cleanly.
            snapped_clipped = snap(clipped, extent_poly.boundary, road_snap_tol)
            road_label_features.append((snapped_clipped, name, highway))
            # Use buffered road polygon to keep intersections connected
            try:
                half_w = max(1.0, (road_width_m or 3.0) / 2.0)
                road_geom_width.append((snapped_clipped, half_w))
            except Exception:
                continue

        # Add user-provided road overrides
        for ov in road_add_geoms:
            geom = ov["geom"]
            name = ov["name"]
            try:
                gdf_line = gpd.GeoSeries([geom], crs="EPSG:4326").to_crs(epsg=display_epsg)
                line_proj = gdf_line.iloc[0]
            except Exception:
                continue
            expanded_frame = extent_poly.buffer(road_snap_tol)
            clipped = line_proj.intersection(expanded_frame)
            if clipped.is_empty:
                continue
            snapped_clipped = snap(clipped, extent_poly.boundary, road_snap_tol)
            road_label_features.append((snapped_clipped, name, "override"))
            try:
                half_w = max(1.0, ((ov.get("width_m") or road_width_override_m or road_width_m) or 3.0) / 2.0)
                road_geom_width.append((snapped_clipped, half_w))
            except Exception:
                continue

        road_edge_lines = _collect_connected_road_edge_lines(road_geom_width, snap_tol_m=road_snap_tol)
        has_roads = len(road_rows) > 0 or len(road_add_geoms) > 0

    # River names come from user-provided overrides (rivers have no name in detected_features/OSM
    # here) - named the same way as manually-added road overrides above.
    river_label_features = []
    river_add_geoms = [
        ov for ov in overrides
        if ov["feature_type"] == "river" and ov["action"] in ("add", "update")
        and ov["geom"] is not None and str(ov.get("name") or "").strip()
    ]
    for ov in river_add_geoms:
        geom = ov["geom"]
        name = ov["name"]
        try:
            gdf_line = gpd.GeoSeries([geom], crs="EPSG:4326").to_crs(epsg=display_epsg)
            line_proj = gdf_line.iloc[0]
        except Exception:
            continue
        expanded_frame = extent_poly.buffer(road_snap_tol)
        clipped = line_proj.intersection(expanded_frame)
        if clipped.is_empty:
            continue
        snapped_clipped = snap(clipped, extent_poly.boundary, road_snap_tol)
        river_label_features.append((snapped_clipped, name))

    key_bounds = draw_key_box(
        fig,
        has_buildings=has_buildings,
        has_roads=has_roads,
        has_rivers=has_rivers,
        has_fences=has_fences,
        font_scale=font_scale,
    )
    draw_certification_box(
        fig,
        certification_statement or DEFAULT_CERTIFICATION_STATEMENT,
        surveyor_name,
        key_bounds=key_bounds,
        font_scale=font_scale,
    )

    _draw_road_edges(ax, road_edge_lines, font_scale=font_scale, color=road_color, scale_ratio=scale_ratio, road_style=road_style)

    # Skip real-world-tiny structures (a shed, a kiosk) that wouldn't render legibly at this
    # plan's scale - drawing every detected speck identically at 1:500 and 1:10000 alike isn't
    # how an accurate survey plan generalizes.
    visible_buildings = filter_features_by_scale(buildings, display_epsg, scale_ratio, min_paper_mm=2.0)
    visible_added_buildings = filter_features_by_scale(added_buildings, display_epsg, scale_ratio, min_paper_mm=2.0)
    all_visible_buildings = visible_buildings + visible_added_buildings
    if all_visible_buildings:
        draw_building_hatch(
            ax, all_visible_buildings, display_epsg, scale_ratio=scale_ratio, font_scale=font_scale,
            color=building_color, hatch_type=building_hatch_type,
        )

    building_lw = scaled_line_weight(0.2, font_scale, scale_ratio)
    if visible_buildings:
        gpd.GeoDataFrame(geometry=visible_buildings, crs="EPSG:4326").to_crs(epsg=display_epsg).plot(
            ax=ax, facecolor="none", edgecolor=building_color, lw=building_lw, zorder=8
        )
    if visible_added_buildings:
        gpd.GeoDataFrame(geometry=visible_added_buildings, crs="EPSG:4326").to_crs(epsg=display_epsg).plot(
            ax=ax, facecolor="none", edgecolor=building_color, lw=building_lw, zorder=9
        )
    if fences or added_fences:
        draw_fences(
            ax,
            list(fences or []) + list(added_fences or []),
            display_epsg,
            scale_ratio=scale_ratio,
            font_scale=font_scale,
        )
    fence_avoid_geom = build_fence_avoid_geom(
        list(fences or []) + list(added_fences or []),
        display_epsg=display_epsg,
        scale_ratio=scale_ratio,
    )

    # Boundary thickness in mm based on common drafting line weights
    paper_name = paper_config["name"]
    boundary_mm = 0.7 if paper_name in ["A0"] else 0.5 if paper_name in ["A1"] else 0.35
    boundary_lw_pts = boundary_mm * 72.0 / 25.4
    gdf_plot.plot(ax=ax, facecolor="none", edgecolor=boundary_color, lw=boundary_lw_pts, zorder=20)
    ax.set_xlim(target_xlim)
    ax.set_ylim(target_ylim)

    major = nice_grid_step(max(ax.get_xlim()[1] - ax.get_xlim()[0], ax.get_ylim()[1] - ax.get_ylim()[0]))
    draw_grid(ax, poly, major / 5.0, major, font_scale, color=grid_color)

    # Get first point coordinates for display
    first_coords = list(poly.exterior.coords)[0]
    first_station = station_names[0] if station_names and len(station_names) > 0 else "A"
    first_point_info = (first_station, first_coords[0], first_coords[1])

    draw_coordinate_frame(ax, major, font_scale, first_point_info, color=grid_color, grid_font=grid_font, grid_size=grid_size)
    skipped_entries, boundary_label_boxes = annotate_vertices(
        ax,
        poly,
        plot_id,
        station_names,
        font_scale,
        min_label_length_m=min_label_length_m,
        avoid_geom=fence_avoid_geom,
        scale_ratio=scale_ratio,
        boundary_poly=poly,
        beacon_style=beacon_style,
        text_color=text_color,
        boundary_color=boundary_color,
        station_font=station_font,
        station_size=station_size,
        bearing_font=bearing_font,
        bearing_size=bearing_size,
    )
    draw_skipped_table(ax, skipped_entries, font_scale, poly=poly)

    # Road/river names (optional). Follow the path's own direction; keep clear of boundary labels.
    major_classes = {
        "trunk", "trunk_link", "motorway", "motorway_link",
        "primary", "primary_link", "secondary", "secondary_link",
        "tertiary", "tertiary_link",
    }
    span_x = max(abs(target_xlim[1] - target_xlim[0]), 1.0)
    span_y = max(abs(target_ylim[1] - target_ylim[0]), 1.0)

    def intersects(a, b):
        return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])

    def label_overlaps_boundary(box):
        return any(intersects(box, other) for other in boundary_label_boxes)

    def estimate_box(x, y, text_len, scale_w=0.018, scale_h=0.018):
        w = span_x * scale_w * max(1.0, text_len / 10.0)
        h = span_y * scale_h
        return (x - w / 2.0, y - h / 2.0, x + w / 2.0, y + h / 2.0)

    def boundary_skip(x, y, label):
        return label_overlaps_boundary(estimate_box(x, y, len(label)))

    qualifying_road_pairs = [
        (geom, name) for geom, name, highway in road_label_features
        if str(name or "").strip()
        and geom.length > min_label_length_m * 1.5
        and (not highway or highway.lower() in major_classes or highway == "override")
    ]
    _draw_names_along_path(
        ax, qualifying_road_pairs, color=road_color, font_scale=font_scale,
        skip_point_fn=boundary_skip,
    )
    _draw_names_along_path(
        ax, river_label_features, color=river_color, font_scale=font_scale,
        skip_point_fn=boundary_skip,
    )

    # draw_coordinate_frame's outer border sits at data-y = ax.get_ylim()[1] + grid_pad, above the
    # map itself - the north arrow's default anchor doesn't know about that extra padding, so
    # without this it can end up landing on/inside the grid frame instead of clearing it. Compute
    # the frame's actual top edge in figure coordinates and anchor the arrow safely above it.
    _grid_xlim = ax.get_xlim()
    _grid_pad = (_grid_xlim[1] - _grid_xlim[0]) * 0.035
    _, _grid_top_fig_y = fig.transFigure.inverted().transform(
        ax.transData.transform((_grid_xlim[0], ax.get_ylim()[1] + _grid_pad))
    )
    add_north_arrow(
        ax, font_scale, style=north_arrow_style, color=north_arrow_color,
        anchor_y=min(0.93, _grid_top_fig_y + 0.045),
    )
    add_scalebar(ax, 100 if scale_ratio <= 1000 else 500, font_scale=font_scale)

    ax.set_aspect("equal")
    ax.axis("off")

    fig.canvas.draw()
    # Match orthophoto save behavior so the page frame fills the preview consistently.
    fig.savefig(output_path, dpi=dpi)
    plt.close(fig)
