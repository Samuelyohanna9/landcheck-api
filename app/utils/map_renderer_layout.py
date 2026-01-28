# app/utils/map_renderer_layout.py
# A4 PORTRAIT – Professional survey plan layout with TRUE SCALE rendering
#
# Notes:
# - Uses matplotlib Agg backend (safe for FastAPI threads).
# - TRUE SCALE: axis extent is computed from A4 physical map window * scale ratio (e.g. 1:1000).
# - Buffer is kept (for zoom/logic) but NOT drawn.
# - Bearings/distances are plain text (no boxes).
# - Footer fields are editable (title, station, surveyor, rank, scale, location...) and
#   DATE PRINTED is automatic (current date/time at render).

import matplotlib
matplotlib.use("Agg")

import math
import numpy as np
import geopandas as gpd
import matplotlib.pyplot as plt
from sqlalchemy import text
from shapely import wkb
from shapely.geometry import LineString, Point
import matplotlib.patches as patches
import matplotlib.lines as mlines
from datetime import datetime


# ======================
# Geometry helpers
# ======================

def calculate_bearing_deg(p1: Point, p2: Point) -> float:
    """Survey bearing (0° = North, clockwise)."""
    dx = p2.x - p1.x
    dy = p2.y - p1.y
    return (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0


def nice_grid_step(span_m: float) -> float:
    """Pick a nice major grid spacing based on map span."""
    if span_m <= 0:
        return 100.0
    base = 10 ** math.floor(math.log10(span_m))
    steps = np.array([0.02, 0.05, 0.1, 0.2, 0.5, 1.0]) * base
    target = span_m / 6.0
    return float(steps[np.argmin(np.abs(steps - target))])


def parse_scale_ratio(scale_text: str) -> int:
    """
    Accepts: '1 : 1000', '1:1000', '1000', etc.
    Returns denominator (e.g. 1000).
    """
    try:
        s = str(scale_text).strip().replace(" ", "")
        if ":" in s:
            left, right = s.split(":")
            # allow "1:1000" or "1: 1000"
            denom = int(right)
            return max(1, denom)
        # if user passes just "1000"
        denom = int(s)
        return max(1, denom)
    except Exception:
        return 1000


def apply_true_scale(ax, geom_for_extent, scale_ratio: int, map_width_in: float, map_height_in: float):
    """
    True scale:
      - 1 inch on paper = 0.0254 meters
      - map window physical size (inches) -> ground coverage (meters) via scale ratio
      - set axis extent centered on geometry centroid
    """
    # Work with bounds (geometry should already be in meters CRS, EPSG:3857 here)
    minx, miny, maxx, maxy = geom_for_extent.bounds
    cx = (minx + maxx) / 2.0
    cy = (miny + maxy) / 2.0

    inch_to_m = 0.0254
    paper_w_m = map_width_in * inch_to_m
    paper_h_m = map_height_in * inch_to_m

    # Ground coverage for the map window at requested scale
    real_w = paper_w_m * float(scale_ratio)
    real_h = paper_h_m * float(scale_ratio)

    ax.set_xlim(cx - real_w / 2.0, cx + real_w / 2.0)
    ax.set_ylim(cy - real_h / 2.0, cy + real_h / 2.0)


# ======================
# Page layout
# ======================

def draw_sheet_frame(fig):
    fig.add_artist(
        patches.Rectangle((0.02, 0.02), 0.96, 0.96,
                          transform=fig.transFigure, fill=False, lw=2)
    )
    fig.add_artist(
        patches.Rectangle((0.03, 0.03), 0.94, 0.94,
                          transform=fig.transFigure, fill=False, lw=0.8)
    )


def draw_title_block(
    fig,
    title_text: str,
    plot_id: int,
    area_m2: float,
    scale_text: str,
    location_text: str,
    lga_text: str,
    state_text: str,
    station_text: str,
):
    y = 0.955
    fig.text(0.5, y, str(title_text), ha="center", fontsize=12, weight="bold")
    fig.text(0.5, y - 0.025, f"OF PLOT {plot_id}", ha="center", fontsize=10)

    # Editable station/location details
    fig.text(0.5, y - 0.055, f"STATION: {station_text}", ha="center", fontsize=9)
    fig.text(0.5, y - 0.075, f"LOCATED AT: {location_text}", ha="center", fontsize=9)
    fig.text(0.5, y - 0.095, str(lga_text), ha="center", fontsize=9)
    fig.text(0.5, y - 0.115, str(state_text), ha="center", fontsize=9)

    fig.text(0.5, y - 0.145, f"AREA = {area_m2/10000:.4f} HA.", ha="center", fontsize=9, color="red")
    fig.text(0.5, y - 0.165, f"SCALE  {scale_text}", ha="center", fontsize=9)


def draw_footer(fig, crs_text: str, source_text: str, surveyor: str, rank: str):
    """
    Footer text block aligned to sit above the bottom frame.
    DATE PRINTED is automatic.
    """
    # Align with KEY top visually; keep this stable.
    y = 0.155
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    fig.text(0.05, y,        f"SURVEYOR: {surveyor}", fontsize=8)
    fig.text(0.05, y - 0.018, f"RANK: {rank}", fontsize=8)
    fig.text(0.05, y - 0.036, "SIGNATURE: ____________________", fontsize=8)
    fig.text(0.05, y - 0.054, f"DATE PRINTED: {now}", fontsize=8)

    # Raised origin/source a bit (as requested)
    fig.text(0.05, 0.050, str(crs_text), fontsize=7, color="blue")
    fig.text(0.95, 0.050, str(source_text), fontsize=7, ha="right")


def draw_key_box(fig):
    """
    Key box centered in lower area (as your current layout).
    """
    w, h = 0.28, 0.12
    x = 0.50 - w / 2.0
    y = 0.05

    fig.add_artist(
        patches.Rectangle((x, y), w, h, transform=fig.transFigure, fill=False, lw=0.9)
    )
    fig.text(x + w / 2.0, y + h - 0.02, "KEY", ha="center", fontsize=9, weight="bold")

    items = [
        ("PERIMETER (Plot)", "red", 2),
        ("BUILDINGS", "black", 1),
    ]

    yy = y + h - 0.05
    for lbl, col, lw in items:
        fig.lines.append(
            mlines.Line2D([x + 0.03, x + 0.11], [yy, yy],
                          transform=fig.transFigure, color=col, lw=lw)
        )
        fig.text(x + 0.13, yy, lbl, fontsize=8, va="center")
        yy -= 0.04


# ======================
# Map decorations
# ======================

def add_north_arrow(ax):
    xmin, xmax = ax.get_xlim()
    ymin, ymax = ax.get_ylim()

    x = xmax - (xmax - xmin) * 0.06
    y = ymax - (ymax - ymin) * 0.10

    ax.annotate(
        "N",
        xy=(x, y),
        xytext=(x, y - (ymax - ymin) * 0.08),
        arrowprops=dict(facecolor="black", width=2, headwidth=8),
        ha="center",
        fontsize=12,
        weight="bold",
    )


def choose_scalebar_length(scale_ratio: int) -> int:
    """
    Sensible Nigerian survey defaults depending on scale.
    """
    if scale_ratio <= 500:
        return 50
    if scale_ratio <= 1000:
        return 100
    if scale_ratio <= 2000:
        return 200
    return 500


def add_scalebar(ax, length_m: float, segments: int = 4):
    """
    Segmented engineering/survey scale bar (black/white).
    """
    xmin, xmax = ax.get_xlim()
    ymin, ymax = ax.get_ylim()

    bar_h = (ymax - ymin) * 0.006

    x0 = xmin + (xmax - xmin) * 0.10
    y0 = ymin - (ymax - ymin) * 0.05

    seg_len = float(length_m) / float(segments)

    # segments
    for i in range(segments):
        xi = x0 + i * seg_len
        face = "black" if (i % 2 == 0) else "white"
        ax.add_patch(
            patches.Rectangle(
                (xi, y0),
                seg_len,
                bar_h,
                facecolor=face,
                edgecolor="black",
                linewidth=0.8,
                clip_on=False,
            )
        )

    # outline
    ax.add_patch(
        patches.Rectangle(
            (x0, y0),
            float(length_m),
            bar_h,
            fill=False,
            edgecolor="black",
            linewidth=1.2,
            clip_on=False,
        )
    )

    # numeric ticks
    ax.text(x0, y0 - bar_h * 2.0, "0", ha="center", va="top", fontsize=7)
    for i in range(1, segments + 1):
        ax.text(
            x0 + i * seg_len,
            y0 - bar_h * 2.0,
            f"{int(round(i * seg_len))}",
            ha="center",
            va="top",
            fontsize=7,
        )

    ax.text(x0 + float(length_m) / 2.0, y0 + bar_h * 3.0, "meters", ha="center", fontsize=7)


def draw_grid(ax, plot_poly, minor: float, major: float):
    """
    Grid lines masked out inside the plot polygon.
    """
    xmin, xmax = ax.get_xlim()
    ymin, ymax = ax.get_ylim()

    def draw(step, lw, alpha):
        xs = np.arange(math.floor(xmin / step) * step, xmax + step, step)
        ys = np.arange(math.floor(ymin / step) * step, ymax + step, step)

        for x in xs:
            g = LineString([(x, ymin), (x, ymax)]).difference(plot_poly)
            for gg in getattr(g, "geoms", [g]):
                if not gg.is_empty:
                    ax.plot(*gg.xy, color="blue", lw=lw, alpha=alpha)

        for y in ys:
            g = LineString([(xmin, y), (xmax, y)]).difference(plot_poly)
            for gg in getattr(g, "geoms", [g]):
                if not gg.is_empty:
                    ax.plot(*gg.xy, color="blue", lw=lw, alpha=alpha)

    draw(minor, 0.3, 0.20)
    draw(major, 1.0, 0.60)


def draw_coordinate_frame(ax, spacing: float):
    """
    Double rectangular coordinate frame around the grid area,
    and coordinate labels confined to the frame (no stray labels below map).
    """
    xmin, xmax = ax.get_xlim()
    ymin, ymax = ax.get_ylim()

    pad = (xmax - xmin) * 0.035

    # outer & inner frames
    ax.add_patch(
        patches.Rectangle(
            (xmin - pad, ymin - pad),
            (xmax - xmin) + 2 * pad,
            (ymax - ymin) + 2 * pad,
            fill=False,
            lw=1.5,
        )
    )
    ax.add_patch(
        patches.Rectangle((xmin, ymin), (xmax - xmin), (ymax - ymin), fill=False, lw=1.0)
    )

    # label ranges
    xs = np.arange(math.floor(xmin / spacing) * spacing, xmax + 0.1, spacing)
    ys = np.arange(math.floor(ymin / spacing) * spacing, ymax + 0.1, spacing)

    # Eastings along top only (cleaner)
    for x in xs:
        ax.text(x, ymax + pad * 0.45, f"{int(round(x))}", ha="center", fontsize=7, color="blue")

    # Northings left and right; skip very bottom one to prevent “stray” label below map window
    for y in ys:
        if y < ymin + spacing * 0.9:
            continue
        ax.text(
            xmin - pad * 0.45,
            y,
            f"{int(round(y))}",
            va="center",
            ha="right",
            fontsize=7,
            color="blue",
            rotation=90,
        )
        ax.text(
            xmax + pad * 0.45,
            y,
            f"{int(round(y))}",
            va="center",
            ha="left",
            fontsize=7,
            color="blue",
            rotation=90,
        )

def annotate_vertices(ax, poly, plot_id: int, station_names=None):
    coords = list(poly.exterior.coords)
    default_labels = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    labels = station_names if station_names else default_labels

    for i in range(len(coords) - 1):
        p1 = Point(coords[i])
        p2 = Point(coords[i + 1])

        label = labels[i] if i < len(labels) else labels[i % len(labels)]

        # Station label
        ax.text(
            p1.x, p1.y, label,
            fontsize=8, color="blue",
            ha="center", va="center",
            bbox=dict(facecolor="white", edgecolor="blue", boxstyle="circle,pad=0.2"),
        )

        # Bearing + distance
        bearing = calculate_bearing_deg(p1, p2)
        dist = p1.distance(p2)

        mx, my = (p1.x + p2.x) / 2.0, (p1.y + p2.y) / 2.0
        ang = math.degrees(math.atan2(p2.y - p1.y, p2.x - p1.x))
        if ang < -90 or ang > 90:
            ang += 180

        ax.text(
            mx, my,
            f"{bearing:.1f}°\n{dist:.1f}m",
            fontsize=6.5,
            ha="center",
            va="center",
            rotation=ang,
            color="black",
        )

    # Plot label
    c = poly.centroid
    ax.text(
        c.x, c.y, f"PLOT {plot_id}",
        fontsize=9, weight="bold",
        ha="center", va="center",
        bbox=dict(facecolor="white", alpha=0.8),
    )


# ======================
# Main renderer
# ======================

def render_plot_map_layout(
    db,
    plot_id: int,
    output_path: str,
    title_text: str = "SURVEY PLAN",
    location_text: str = "__________________",
    lga_text: str = "LOCAL GOVERNMENT AREA",
    state_text: str = "STATE",
    station_text: str = "STATION",
    scale_text: str = "1 : 1000",
    crs_footer_text: str = "ORIGIN: WGS84)",
    source_footer_text: str = "SOURCE: LandCheck System (MVP)",
    surveyor_name: str = "__________________",
    surveyor_rank: str = "__________________",
    station_names: list[str] | None = None,
):
    # ---- Load geometries ----
    plot_wkb = db.execute(text("SELECT geom FROM plots WHERE id=:id"), {"id": plot_id}).scalar()
    buffer_wkb = db.execute(text("SELECT geom FROM plot_buffers WHERE plot_id=:id"), {"id": plot_id}).scalar()

    rows = db.execute(
        text(
            """
            SELECT geom FROM detected_features
            WHERE plot_id=:id AND feature_type='building'
            """
        ),
        {"id": plot_id},
    ).fetchall()

    if plot_wkb is None:
        raise ValueError(f"Plot id={plot_id} not found (plots.geom is null).")
    if buffer_wkb is None:
        raise ValueError(f"Buffer for plot id={plot_id} not found (plot_buffers.geom is null).")

    plot_geom = wkb.loads(plot_wkb)
    buffer_geom = wkb.loads(buffer_wkb)
    buildings = [wkb.loads(r.geom) for r in rows if getattr(r, "geom", None) is not None]

    # ---- Project to meters CRS ----
    gdf_plot = gpd.GeoDataFrame(geometry=[plot_geom], crs="EPSG:4326").to_crs(3857)
    gdf_buffer = gpd.GeoDataFrame(geometry=[buffer_geom], crs="EPSG:4326").to_crs(3857)

    gdf_buildings = None
    if buildings:
        gdf_buildings = gpd.GeoDataFrame(geometry=buildings, crs="EPSG:4326").to_crs(3857)

    poly = gdf_plot.geometry.iloc[0]
    area_m2 = float(poly.area)

    # ---- Figure ----
    fig = plt.figure(figsize=(8.27, 11.69), dpi=200)

    # Map window (figure coordinates)
    map_left, map_bottom, map_width, map_height = 0.10, 0.24, 0.80, 0.52
    ax = fig.add_axes([map_left, map_bottom, map_width, map_height])

    # Page texts/frames
    draw_sheet_frame(fig)
    draw_title_block(
        fig=fig,
        title_text=title_text,
        plot_id=plot_id,
        area_m2=area_m2,
        scale_text=scale_text,
        location_text=location_text,
        lga_text=lga_text,
        state_text=state_text,
        station_text=station_text,
    )
    draw_footer(fig, crs_footer_text, source_footer_text, surveyor_name, surveyor_rank)
    draw_key_box(fig)

    # ---- Plot content ----
    # Buffer kept for logic/extent but NOT drawn (requested)
    # (do not plot gdf_buffer)

    # Buildings
    if gdf_buildings is not None and not gdf_buildings.empty:
        gdf_buildings.plot(ax=ax, facecolor="none", edgecolor="black", lw=1)

    # Plot boundary
    gdf_plot.plot(ax=ax, facecolor="none", edgecolor="red", lw=2)

    # ---- TRUE SCALE extent ----
    scale_ratio = parse_scale_ratio(scale_text)
    fig_w_in, fig_h_in = fig.get_size_inches()
    map_w_in = fig_w_in * map_width
    map_h_in = fig_h_in * map_height

    # Center scale view on plot, but ensure plot fits:
    # 1) set true scale window
    apply_true_scale(ax, poly, scale_ratio, map_w_in, map_h_in)

    # 2) if plot doesn't fit inside true-scale window (user chose too large scale),
    #    gently expand to include it (prevents plot clipping).
    minx, miny, maxx, maxy = poly.bounds
    x0, x1 = ax.get_xlim()
    y0, y1 = ax.get_ylim()
    if (minx < x0) or (maxx > x1) or (miny < y0) or (maxy > y1):
        # expand just enough with small margin
        padx = (maxx - minx) * 0.10
        pady = (maxy - miny) * 0.10
        ax.set_xlim(minx - padx, maxx + padx)
        ax.set_ylim(miny - pady, maxy + pady)

    # ---- Grid & coordinates ----
    span = max(ax.get_xlim()[1] - ax.get_xlim()[0], ax.get_ylim()[1] - ax.get_ylim()[0])
    major = nice_grid_step(span)
    minor = major / 5.0

    draw_grid(ax, poly, minor, major)
    draw_coordinate_frame(ax, major)

    # Bearings/vertices
    annotate_vertices(ax, poly, plot_id, station_names)
    # North arrow & improved scale bar
    add_north_arrow(ax)
    add_scalebar(ax, choose_scalebar_length(scale_ratio), segments=4)

    ax.set_aspect("equal")
    ax.axis("off")

    plt.savefig(output_path, dpi=200, bbox_inches="tight")
    plt.close(fig)
