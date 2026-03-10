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
from shapely.geometry import LineString, Point, shape
from shapely.ops import snap
import matplotlib.patches as patches
import matplotlib.lines as mlines
from matplotlib.font_manager import FontProperties
from datetime import datetime
from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas

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
    "A4": 1.0,
    "A3": 1.25,
    "A2": 1.5,
    "A1": 1.8,
    "A0": 2.2,
}

DEFAULT_CERTIFICATION_STATEMENT = (
    "I hereby certify that this survey plan is a true representation of the survey "
    "executed by me and conforms with the regulations of surveying profession."
)


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


def format_bearing_dms(bearing_deg: float) -> str:
    deg = int(bearing_deg)
    minutes_full = (bearing_deg - deg) * 60.0
    minutes = int(round(minutes_full))
    if minutes == 60:
        deg += 1
        minutes = 0
    return f"{deg}\u00B0{minutes:02d}\u2032"


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


def draw_title_block(fig, title_text, plot_id, area_m2, scale_text, location_text, lga_text, state_text, font_scale=1.0):
    # Plot number at top right corner for identification
    fig.text(0.94, 0.95, f"Plot #{plot_id}", ha="right", fontsize=int(8*font_scale), weight="bold",)

    y = 0.955
    fig.text(0.5, y, str(title_text), ha="center", fontsize=int(12*font_scale), weight="bold")
    fig.text(0.5, y - 0.030, f"LOCATED AT: {location_text}", ha="center", fontsize=int(9*font_scale))
    fig.text(0.5, y - 0.050, str(lga_text), ha="center", fontsize=int(9*font_scale))
    fig.text(0.5, y - 0.070, str(state_text), ha="center", fontsize=int(9*font_scale))
    fig.text(0.5, y - 0.100, f"AREA = {area_m2/10000:.4f} HA.", ha="center", fontsize=int(9*font_scale), color="red")
    fig.text(0.5, y - 0.120, f"SCALE  {scale_text}", ha="center", fontsize=int(9*font_scale))


def draw_footer(fig, crs_text, source_text, surveyor, rank, font_scale=1.0):
    y_top = 0.155
    y_bot = 0.055
    y_bot_source = 0.045
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    fig.text(0.06, y_top, f"SURVEYOR: {surveyor}", fontsize=int(9*font_scale))
    fig.text(0.06, y_top - 0.025, f"RANK: {rank}", fontsize=int(9*font_scale))
    fig.text(0.06, y_top - 0.050, "SIGNATURE: ____________________", fontsize=int(9*font_scale))
    fig.text(0.06, y_top - 0.075, f"DATE PRINTED: {now}", fontsize=int(9*font_scale))

    fig.text(0.06, y_bot, str(crs_text), fontsize=int(8*font_scale), color="blue")
    fig.text(0.94, y_bot_source, str(source_text), fontsize=int(8*font_scale), ha="right")


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
    text_width_fig = max(0.16, min(0.29, 0.95 - x - 0.012))

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
        is_last_line = idx == len(lines) - 1
        fs = max(5, int(6 * font_scale))
        if is_last_line:
            fig.text(
                x,
                yy,
                line,
                transform=fig.transFigure,
                fontsize=fs,
                va="top",
                ha="left",
            )
        else:
            _draw_figure_text_justified(fig, x, yy, line, text_width_fig, fs)

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
        elif sym == "fence_line":
            draw_fence_symbol(fig, x + 0.03, x + 0.10, yy, color=col, lw=lw * font_scale)

        fig.text(x + 0.12, yy, lbl, fontsize=int(7*font_scale), va="center")
        yy -= row_h
    return (x, y, w, h)


# ======================
# Map Decorations
# ======================

def add_north_arrow(ax, font_scale=1.0, style: str = "classic", color: str = "black"):
    fig = ax.figure
    col = "blue" if str(color).lower() == "blue" else "black"
    style = (style or "classic").lower()
    x, y = 0.85, 0.86
    size = 0.030 * max(0.8, font_scale)

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
        xy=(0.85, 0.90),
        xytext=(0.85, 0.83),
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

def draw_grid(ax, plot_poly, minor: float, major: float, font_scale=1.0):
    xmin, xmax = ax.get_xlim()
    ymin, ymax = ax.get_ylim()

    # Tick-only grid to keep plot area clean
    tick_len = (xmax - xmin) * 0.01
    xs = np.arange(math.floor(xmin / major) * major, xmax + 0.1, major)
    ys = np.arange(math.floor(ymin / major) * major, ymax + 0.1, major)

    for x in xs:
        if x < xmin or x > xmax:
            continue
        ax.plot([x, x], [ymax, ymax - tick_len], color="blue", lw=0.6*font_scale, alpha=0.5)
        ax.plot([x, x], [ymin, ymin + tick_len], color="blue", lw=0.6*font_scale, alpha=0.5)

    for y in ys:
        if y < ymin or y > ymax:
            continue
        ax.plot([xmin, xmin + tick_len], [y, y], color="blue", lw=0.6*font_scale, alpha=0.5)
        ax.plot([xmax, xmax - tick_len], [y, y], color="blue", lw=0.6*font_scale, alpha=0.5)


def draw_coordinate_frame(ax, spacing: float, font_scale=1.0, first_point_info=None):
    """
    Draw coordinate frame with grid labels.
    first_point_info: tuple (station_name, easting, northing) to display below the grid
    """
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
            ax.text(x, ymax + pad * 0.45, f"{int(round(x))}", ha="center", fontsize=int(7*font_scale), color="blue")

    # Draw northing labels on both sides - include ALL grid lines including the first one
    for y in ys:
        if y >= ymin and y <= ymax:
            ax.text(
                xmin - pad * 0.45,
                y,
                f"{int(round(y))}",
                va="center",
                ha="right",
                fontsize=int(7*font_scale),
                color="blue",
                rotation=90,
            )
            ax.text(
                xmax + pad * 0.45,
                y,
                f"{int(round(y))}",
                va="center",
                ha="left",
                fontsize=int(7*font_scale),
                color="blue",
                rotation=90,
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
            fontsize=int(8*font_scale),
            color="black",
            weight="bold",
            clip_on=False,
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
    beacon_style: str = "circle",
):
    """
    Annotate vertices with station names and bearing/distance in RED.
    Applies simple collision-aware placement for tight turns.
    """
    coords = list(poly.exterior.coords)
    default_labels = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    labels = station_names if station_names else default_labels

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

    def place_text(x, y, text, font_size, color, rotation=0, weight="bold", scale_w=0.015, scale_h=0.02, normal=None):
        offset_m = max(2.0, (6.0 / 1000.0) * scale_ratio)
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
                (x, y),
            ]
            if avoid_geom is not None:
                candidates = [c for c in candidates if not avoid_geom.contains(Point(c[0], c[1]))]
                if not candidates:
                    for k in range(2, 7):
                        cand = (x + nx * offset_m * k, y + ny * offset_m * k)
                        if not avoid_geom.contains(Point(cand[0], cand[1])):
                            candidates.append(cand)
                    if not candidates:
                        candidates = [(x, y)]
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
                    linespacing=0.95,
                    zorder=25,
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

        draw_beacon(p1.x, p1.y)
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
            font_size=int(8 * font_scale),
            color="black",
            rotation=0,
            weight="bold",
            scale_w=0.010,
            scale_h=0.016,
            normal=None,
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

        place_text(
            mx,
            my,
            f"{format_bearing_dms(bearing)}\n{dist:.2f}m",
            font_size=int(6.5 * font_scale),
            color="red",
            rotation=ang,
            weight="normal",
            scale_w=0.02,
            scale_h=0.025,
            normal=normal,
        )

    return skipped, placed_boxes


def draw_skipped_table(ax, entries, font_scale=1.0):
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

    table = ax.table(
        cellText=cell_text,
        colLabels=header,
        cellLoc="center",
        colLoc="center",
        bbox=[0.62, 0.02, 0.36, 0.18],
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


def _draw_fence_line(ax, line_geom, scale_ratio: int, font_scale=1.0):
    if line_geom is None or line_geom.is_empty:
        return
    try:
        x_vals, y_vals = line_geom.xy
    except Exception:
        return

    length = getattr(line_geom, "length", 0.0) or 0.0
    if length <= 0:
        ax.plot(x_vals, y_vals, color="black", lw=0.8 * font_scale, zorder=7)
        return

    step_span = max(2.5, (9.0 / 1000.0) * max(scale_ratio, 100))
    tooth_amp = max(1.2, (3.0 / 1000.0) * max(scale_ratio, 100))
    probe = max(0.4, step_span * 0.2)
    if length < step_span * 1.2:
        ax.plot(x_vals, y_vals, color="black", lw=0.8 * font_scale, zorder=7)
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

    ax.plot(points_x, points_y, color="black", lw=0.8 * font_scale, zorder=7)


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
    north_arrow_style: str = "classic",
    north_arrow_color: str = "black",
    beacon_style: str = "circle",
    road_width_m: float | None = None,
    road_width_override_m: float | None = None,
    preview_mode: bool = False,
):
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

    # Get accurate area using geography (meters squared) - same as back computation
    area_m2 = db.execute(
        text("SELECT ST_Area(geom::geography) FROM plots WHERE id=:id"),
        {"id": plot_id}
    ).scalar() or 0

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
                result = [g for g in result if not g.intersects(geom)]
                delete_geoms.append(geom)
            if ov["action"] in ("add", "update"):
                result.append(geom)
                added.append(geom)
        if delete_geoms:
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

    gdf_plot = gpd.GeoDataFrame(geometry=[plot_geom], crs="EPSG:4326").to_crs(epsg=display_epsg)
    poly = gdf_plot.geometry.iloc[0]

    # Fix invalid/self-intersecting polygons
    if not poly.is_valid:
        poly = poly.buffer(0)
        gdf_plot = gpd.GeoDataFrame(geometry=[poly], crs=f"EPSG:{display_epsg}")

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

    draw_sheet_frame(fig)
    draw_title_block(fig, title_text, plot_id, area_m2, scale_text, location_text, lga_text, state_text, font_scale)
    draw_footer(fig, crs_footer_text, source_footer_text, surveyor_name, surveyor_rank, font_scale)

    scale_ratio = parse_scale_ratio(scale_text)
    apply_true_scale(ax, poly, scale_ratio, fig_width * map_width, fig_height * map_height)
    target_xlim = ax.get_xlim()
    target_ylim = ax.get_ylim()

    min_label_mm = 12
    min_label_length_m = (min_label_mm / 1000.0) * scale_ratio

    # flags for KEY (only show what exists)
    has_buildings = len(buildings) > 0
    has_rivers = len(rivers) > 0
    has_fences = len(fences) > 0

    if rivers:
        gpd.GeoDataFrame(geometry=rivers, crs="EPSG:4326").to_crs(epsg=display_epsg).plot(
            ax=ax, color="blue", lw=1.2*font_scale, zorder=5
        )

    from shapely.geometry import box

    extent_poly = box(target_xlim[0], target_ylim[0], target_xlim[1], target_ylim[1])
    road_polys = []
    road_label_features = []
    if preview_mode:
        for geom in roads_for_preview:
            try:
                gdf_line = gpd.GeoSeries([geom], crs="EPSG:4326").to_crs(epsg=display_epsg)
                line_proj = gdf_line.iloc[0]
            except Exception:
                continue
            snap_tol = max(1.0, (5.0 / 1000.0) * scale_ratio)
            expanded_frame = extent_poly.buffer(snap_tol)
            clipped = line_proj.intersection(expanded_frame)
            if clipped.is_empty:
                continue
            snapped_clipped = snap(clipped, extent_poly.boundary, snap_tol)
            try:
                half_w = max(1.0, (road_width_m or 3.0) / 2.0)
                road_polys.append(snapped_clipped.buffer(half_w, cap_style=2, join_style=2))
            except Exception:
                continue
        has_roads = len(road_polys) > 0
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

        road_delete_geoms = [ov["geom"] for ov in overrides if ov["feature_type"] == "road" and ov["action"] == "delete" and ov["geom"] is not None]
        road_add_geoms = [ov for ov in overrides if ov["feature_type"] == "road" and ov["action"] in ("add", "update") and ov["geom"] is not None]
        for row in road_rows:
            geom = wkb.loads(row.geom)
            highway = row.highway
            name = row.name
            if road_delete_geoms and any(geom.intersects(dg) for dg in road_delete_geoms):
                continue
            try:
                gdf_line = gpd.GeoSeries([geom], crs="EPSG:4326").to_crs(epsg=display_epsg)
                line_proj = gdf_line.iloc[0]
            except Exception:
                continue

            snap_tol = max(1.0, (5.0 / 1000.0) * scale_ratio)
            expanded_frame = extent_poly.buffer(snap_tol)
            clipped = line_proj.intersection(expanded_frame)
            if clipped.is_empty:
                continue
            # Snap to frame boundary so buffered road edges reach the grid border cleanly.
            snapped_clipped = snap(clipped, extent_poly.boundary, snap_tol)
            road_label_features.append((snapped_clipped, name, highway))
            # Use buffered road polygon to keep intersections connected
            try:
                half_w = max(1.0, (road_width_m or 3.0) / 2.0)
                road_polys.append(snapped_clipped.buffer(half_w, cap_style=2, join_style=2))
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
            snap_tol = max(1.0, (5.0 / 1000.0) * scale_ratio)
            expanded_frame = extent_poly.buffer(snap_tol)
            clipped = line_proj.intersection(expanded_frame)
            if clipped.is_empty:
                continue
            snapped_clipped = snap(clipped, extent_poly.boundary, snap_tol)
            road_label_features.append((snapped_clipped, name, "override"))
            try:
                half_w = max(1.0, ((ov.get("width_m") or road_width_override_m or road_width_m) or 3.0) / 2.0)
                road_polys.append(snapped_clipped.buffer(half_w, cap_style=2, join_style=2))
            except Exception:
                continue

        has_roads = len(road_rows) > 0 or len(road_add_geoms) > 0
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

    if road_polys:
        try:
            road_union = road_polys[0]
            for rp in road_polys[1:]:
                road_union = road_union.union(rp)
            boundary = road_union.boundary
            # Clip roads to map frame for even endings at the border
            frame = box(target_xlim[0], target_ylim[0], target_xlim[1], target_ylim[1])
            snap_tol = max(1.0, (5.0 / 1000.0) * scale_ratio)
            snapped = snap(boundary, frame.boundary, snap_tol)
            clipped = snapped.intersection(frame)
            gpd.GeoSeries([clipped], crs=f"EPSG:{display_epsg}").plot(
                ax=ax, color="black", lw=0.8 * font_scale, zorder=6
            )
        except Exception:
            pass

    if buildings:
        gpd.GeoDataFrame(geometry=buildings, crs="EPSG:4326").to_crs(epsg=display_epsg).plot(
            ax=ax, facecolor="none", edgecolor="black", lw=1*font_scale, zorder=8
        )
    if added_buildings:
        gpd.GeoDataFrame(geometry=added_buildings, crs="EPSG:4326").to_crs(epsg=display_epsg).plot(
            ax=ax, facecolor="none", edgecolor="black", lw=1*font_scale, zorder=9
        )
    if fences or added_fences:
        draw_fences(ax, fences, display_epsg, scale_ratio=scale_ratio, font_scale=font_scale)

    # Boundary thickness in mm based on common drafting line weights
    paper_name = paper_config["name"]
    boundary_mm = 0.7 if paper_name in ["A0"] else 0.5 if paper_name in ["A1"] else 0.35
    boundary_lw_pts = boundary_mm * 72.0 / 25.4
    gdf_plot.plot(ax=ax, facecolor="none", edgecolor="red", lw=boundary_lw_pts, zorder=20)
    ax.set_xlim(target_xlim)
    ax.set_ylim(target_ylim)

    major = nice_grid_step(max(ax.get_xlim()[1] - ax.get_xlim()[0], ax.get_ylim()[1] - ax.get_ylim()[0]))
    draw_grid(ax, poly, major / 5.0, major, font_scale)

    # Get first point coordinates for display
    first_coords = list(poly.exterior.coords)[0]
    first_station = station_names[0] if station_names and len(station_names) > 0 else "A"
    first_point_info = (first_station, first_coords[0], first_coords[1])

    draw_coordinate_frame(ax, major, font_scale, first_point_info)
    skipped_entries, boundary_label_boxes = annotate_vertices(
        ax,
        poly,
        plot_id,
        station_names,
        font_scale,
        min_label_length_m=min_label_length_m,
        scale_ratio=scale_ratio,
        boundary_poly=poly,
        beacon_style=beacon_style,
    )
    draw_skipped_table(ax, skipped_entries, font_scale)

    # Road names (optional). Keep small and never overlap boundary labels.
    if not preview_mode and road_label_features:
        seen_names = set()
        boundary_buffer = poly.buffer((12.0 / 1000.0) * scale_ratio)
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

        for geom, name, highway in road_label_features:
            if not name or name in seen_names:
                continue
            if highway and highway.lower() not in major_classes and highway != "override":
                continue
            seen_names.add(name)
            try:
                if geom.length <= min_label_length_m * 1.5:
                    continue
                mid = geom.interpolate(0.5, normalized=True)
                angle = 0.0
                try:
                    p1 = geom.interpolate(0.45, normalized=True)
                    p2 = geom.interpolate(0.55, normalized=True)
                    angle = math.degrees(math.atan2(p2.y - p1.y, p2.x - p1.x))
                    if angle < -90 or angle > 90:
                        angle += 180
                except Exception:
                    pass
                # Keep road label anchored at the road midpoint.
                road_label_size = int(6.5 * font_scale)
                ax.text(
                    mid.x,
                    mid.y,
                    name,
                    fontsize=road_label_size,
                    color="black",
                    ha="center",
                    va="center",
                    rotation=angle,
                    weight="normal",
                    zorder=10,
                )
            except Exception:
                continue

    add_north_arrow(ax, font_scale, style=north_arrow_style, color=north_arrow_color)
    add_scalebar(ax, 100 if scale_ratio <= 1000 else 500, font_scale=font_scale)

    ax.set_aspect("equal")
    ax.axis("off")

    fig.canvas.draw()
    # Match orthophoto save behavior so the page frame fills the preview consistently.
    fig.savefig(output_path, dpi=dpi)
    plt.close(fig)
