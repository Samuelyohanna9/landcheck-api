# app/utils/map_renderer_layout.py
# Professional survey plan layout with TRUE SCALE rendering
# Supports multiple paper sizes: A4, A3, A2, A1, A0

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
    y_top = 0.185
    y_bot = 0.055
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    fig.text(0.06, y_top, f"SURVEYOR: {surveyor}", fontsize=int(9*font_scale))
    fig.text(0.06, y_top - 0.025, f"RANK: {rank}", fontsize=int(9*font_scale))
    fig.text(0.06, y_top - 0.050, "SIGNATURE: ____________________", fontsize=int(9*font_scale))
    fig.text(0.06, y_top - 0.075, f"DATE PRINTED: {now}", fontsize=int(9*font_scale))

    fig.text(0.06, y_bot, str(crs_text), fontsize=int(8*font_scale), color="blue")
    fig.text(0.94, y_bot, str(source_text), fontsize=int(8*font_scale), ha="right")


def draw_key_box(fig, has_buildings: bool, has_roads: bool, has_rivers: bool, font_scale=1.0):
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

    # box sizing based on number of items
    row_h = 0.022
    header_h = 0.028
    padding = 0.015
    h = header_h + len(items) * row_h + padding

    w = 0.30
    x = 0.50 - w / 2.0
    y = 0.065  # moved down a bit (you asked)

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

        fig.text(x + 0.12, yy, lbl, fontsize=int(7*font_scale), va="center")
        yy -= row_h


# ======================
# Map Decorations
# ======================

def add_north_arrow(ax, font_scale=1.0):
    # Keep your current placement (figure fraction)
    ax.annotate(
        "N",
        xy=(0.85, 0.90),
        xytext=(0.85, 0.83),
        xycoords="figure fraction",
        arrowprops=dict(facecolor="black", width=2*font_scale, headwidth=8*font_scale),
        ha="center",
        fontsize=int(12*font_scale),
        weight="bold",
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

    def draw(step, lw, alpha):
        xs = np.arange(math.floor(xmin / step) * step, xmax + step, step)
        ys = np.arange(math.floor(ymin / step) * step, ymax + step, step)

        for x in xs:
            g = LineString([(x, ymin), (x, ymax)]).difference(plot_poly)
            for gg in getattr(g, "geoms", [g]):
                if not gg.is_empty:
                    ax.plot(*gg.xy, color="blue", lw=lw*font_scale, alpha=alpha)

        for y in ys:
            g = LineString([(xmin, y), (xmax, y)]).difference(plot_poly)
            for gg in getattr(g, "geoms", [g]):
                if not gg.is_empty:
                    ax.plot(*gg.xy, color="blue", lw=lw*font_scale, alpha=alpha)

    draw(minor, 0.3, 0.20)
    draw(major, 1.0, 0.60)


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

    # Add first point coordinates text below the grid frame
    if first_point_info:
        station_name, easting, northing = first_point_info
        coord_text = f"{station_name}: {easting:.2f}E, {northing:.2f}N"
        ax.text(
            (xmin + xmax) / 2,
            ymin - pad * 2.5,
            coord_text,
            ha="center",
            va="top",
            fontsize=int(8*font_scale),
            color="black",
            weight="bold",
            clip_on=False,
        )


def annotate_vertices(ax, poly, plot_id: int, station_names=None, font_scale=1.0, first_point_coords=None):
    """
    Annotate vertices with station names and bearing/distance in RED.
    first_point_coords: tuple (station_name, easting, northing) for first point label
    """
    coords = list(poly.exterior.coords)
    default_labels = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    labels = station_names if station_names else default_labels

    for i in range(len(coords) - 1):
        p1, p2 = Point(coords[i]), Point(coords[i + 1])
        label = labels[i % len(labels)]

        ax.text(
            p1.x,
            p1.y,
            label,
            fontsize=int(9*font_scale),
            color="black",
            ha="center",
            va="center",
            weight="bold",
        )

        # Bearing and distance in RED
        bearing, dist = calculate_bearing_deg(p1, p2), p1.distance(p2)
        mx, my = (p1.x + p2.x) / 2.0, (p1.y + p2.y) / 2.0
        ang = math.degrees(math.atan2(p2.y - p1.y, p2.x - p1.x))
        if ang < -90 or ang > 90:
            ang += 180

        ax.text(mx, my, f"{bearing:.1f}°\n{dist:.1f}m", fontsize=int(6.5*font_scale),
                ha="center", va="center", rotation=ang, color="red", weight="bold")


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
    station_names=None,
    coordinate_system: str = "wgs84",
    epsg_code: int = 4326,
    paper_size: str = "A4",
):
    plot_wkb = db.execute(text("SELECT geom FROM plots WHERE id=:id"), {"id": plot_id}).scalar()
    rows = db.execute(
        text("SELECT geom, feature_type FROM detected_features WHERE plot_id=:id"),
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
    buildings, roads, rivers = [], [], []
    for r in rows:
        g = wkb.loads(r.geom)
        if r.feature_type == "building":
            buildings.append(g)
        elif r.feature_type == "road":
            roads.append(g)
        elif r.feature_type == "river":
            rivers.append(g)

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

    # Get paper configuration
    paper_config = get_paper_config(paper_size)
    fig_width = paper_config["width"]
    fig_height = paper_config["height"]
    font_scale = paper_config["scale"]

    # Adjust DPI based on paper size (larger papers need lower DPI for reasonable file sizes)
    dpi = 200 if paper_size in ["A4", "A3"] else 150 if paper_size == "A2" else 100

    fig = plt.figure(figsize=(fig_width, fig_height), dpi=dpi)
    canvas_obj = FigureCanvas(fig)

    map_left, map_bottom, map_width, map_height = 0.10, 0.30, 0.80, 0.45
    ax = fig.add_axes([map_left, map_bottom, map_width, map_height])

    draw_sheet_frame(fig)
    draw_title_block(fig, title_text, plot_id, area_m2, scale_text, location_text, lga_text, state_text, font_scale)
    draw_footer(fig, crs_footer_text, source_footer_text, surveyor_name, surveyor_rank, font_scale)

    # flags for KEY (only show what exists)
    has_buildings = len(buildings) > 0
    has_roads = len(roads) > 0
    has_rivers = len(rivers) > 0
    draw_key_box(fig, has_buildings=has_buildings, has_roads=has_roads, has_rivers=has_rivers, font_scale=font_scale)

    if rivers:
        gpd.GeoDataFrame(geometry=rivers, crs="EPSG:4326").to_crs(epsg=display_epsg).plot(ax=ax, color="blue", lw=1.2*font_scale)

    # Draw roads as double lines
    if roads:
        gdf_roads = gpd.GeoDataFrame(geometry=roads, crs="EPSG:4326").to_crs(epsg=display_epsg)
        road_width = 3 * font_scale  # Total road width
        # Draw outer line (wider)
        gdf_roads.plot(ax=ax, color="dimgray", lw=road_width)
        # Draw inner line (narrower, white to create double-line effect)
        gdf_roads.plot(ax=ax, color="white", lw=road_width * 0.5)

    if buildings:
        gpd.GeoDataFrame(geometry=buildings, crs="EPSG:4326").to_crs(epsg=display_epsg).plot(
            ax=ax, facecolor="none", edgecolor="black", lw=1*font_scale
        )

    gdf_plot.plot(ax=ax, facecolor="none", edgecolor="red", lw=2*font_scale)

    scale_ratio = parse_scale_ratio(scale_text)
    apply_true_scale(ax, poly, scale_ratio, fig_width * map_width, fig_height * map_height)

    major = nice_grid_step(max(ax.get_xlim()[1] - ax.get_xlim()[0], ax.get_ylim()[1] - ax.get_ylim()[0]))
    draw_grid(ax, poly, major / 5.0, major, font_scale)

    # Get first point coordinates for display
    first_coords = list(poly.exterior.coords)[0]
    first_station = station_names[0] if station_names and len(station_names) > 0 else "A"
    first_point_info = (first_station, first_coords[0], first_coords[1])

    draw_coordinate_frame(ax, major, font_scale, first_point_info)
    annotate_vertices(ax, poly, plot_id, station_names, font_scale)

    add_north_arrow(ax, font_scale)
    add_scalebar(ax, 100 if scale_ratio <= 1000 else 500, font_scale=font_scale)

    ax.set_aspect("equal")
    ax.axis("off")

    fig.canvas.draw()
    plt.savefig(output_path, dpi=dpi, bbox_inches=None)
    plt.close(fig)
