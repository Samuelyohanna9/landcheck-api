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

    # Fix invalid/self-intersecting polygons
    if not plot_poly.is_valid:
        plot_poly = plot_poly.buffer(0)  # This fixes most self-intersections

    def draw(step, lw, alpha):
        xs = np.arange(math.floor(xmin / step) * step, xmax + step, step)
        ys = np.arange(math.floor(ymin / step) * step, ymax + step, step)

        for x in xs:
            try:
                g = LineString([(x, ymin), (x, ymax)]).difference(plot_poly)
                for gg in getattr(g, "geoms", [g]):
                    if not gg.is_empty:
                        ax.plot(*gg.xy, color="blue", lw=lw*font_scale, alpha=alpha)
            except Exception:
                # If difference fails, just draw the full line
                ax.plot([x, x], [ymin, ymax], color="blue", lw=lw*font_scale, alpha=alpha)

        for y in ys:
            try:
                g = LineString([(xmin, y), (xmax, y)]).difference(plot_poly)
                for gg in getattr(g, "geoms", [g]):
                    if not gg.is_empty:
                        ax.plot(*gg.xy, color="blue", lw=lw*font_scale, alpha=alpha)
            except Exception:
                # If difference fails, just draw the full line
                ax.plot([xmin, xmax], [y, y], color="blue", lw=lw*font_scale, alpha=alpha)

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

    def estimate_box(x, y, text_len, scale_w, scale_h):
        w = span_x * scale_w * max(1.0, text_len / 8.0)
        h = span_y * scale_h
        return (x - w / 2.0, y - h / 2.0, x + w / 2.0, y + h / 2.0)

    def place_text(x, y, text, font_size, color, rotation=0, weight="bold", scale_w=0.015, scale_h=0.02, normal=None):
        offset_m = max(2.0, (6.0 / 1000.0) * scale_ratio)
        candidates = [(x, y)]
        if normal is not None:
            nx, ny = normal
            candidates = [
                (x + nx * offset_m, y + ny * offset_m),
                (x - nx * offset_m, y - ny * offset_m),
                (x + nx * offset_m * 1.5, y + ny * offset_m * 1.5),
                (x - nx * offset_m * 1.5, y - ny * offset_m * 1.5),
                (x, y),
            ]

        if avoid_geom is not None:
            candidates = [c for c in candidates if not avoid_geom.contains(Point(c[0], c[1]))] + candidates
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
            bx = estimate_box(x + dx, y + dy, len(text), scale_w, scale_h)
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
                    zorder=25,
                )
                placed_boxes.append(bx)
                return True
        return False

    skipped = []
    for i in range(len(coords) - 1):
        p1, p2 = Point(coords[i]), Point(coords[i + 1])
        label = labels[i % len(labels)]
        next_label = labels[(i + 1) % len(labels)]

        seg_dx = p2.x - p1.x
        seg_dy = p2.y - p1.y
        seg_len = math.hypot(seg_dx, seg_dy) or 1.0
        normal = (-seg_dy / seg_len, seg_dx / seg_len)

        place_text(
            p1.x,
            p1.y,
            label,
            font_size=int(9 * font_scale),
            color="black",
            rotation=0,
            weight="bold",
            scale_w=0.012,
            scale_h=0.018,
            normal=normal,
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
            weight="bold",
            scale_w=0.02,
            scale_h=0.025,
            normal=normal,
        )

    return skipped


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
    buildings, rivers = [], []
    for r in rows:
        g = wkb.loads(r.geom)
        if r.feature_type == "building":
            buildings.append(g)
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

    # Fix invalid/self-intersecting polygons
    if not poly.is_valid:
        poly = poly.buffer(0)
        gdf_plot = gpd.GeoDataFrame(geometry=[poly], crs=f"EPSG:{display_epsg}")

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

    scale_ratio = parse_scale_ratio(scale_text)
    apply_true_scale(ax, poly, scale_ratio, fig_width * map_width, fig_height * map_height)
    target_xlim = ax.get_xlim()
    target_ylim = ax.get_ylim()

    min_label_mm = 12
    min_label_length_m = (min_label_mm / 1000.0) * scale_ratio

    # flags for KEY (only show what exists)
    has_buildings = len(buildings) > 0
    has_rivers = len(rivers) > 0

    if rivers:
        gpd.GeoDataFrame(geometry=rivers, crs="EPSG:4326").to_crs(epsg=display_epsg).plot(
            ax=ax, color="blue", lw=1.2*font_scale, zorder=5
        )

    # Draw roads with class-based real-world widths
    road_rows = db.execute(text("""
        SELECT r.geom, r.highway, r.name
        FROM lines r
        JOIN plot_buffers b ON b.plot_id = :plot_id
        WHERE r.highway IS NOT NULL
          AND ST_Intersects(r.geom, b.geom)
    """), {"plot_id": plot_id}).fetchall()

    from shapely.geometry import box

    extent_poly = box(target_xlim[0], target_ylim[0], target_xlim[1], target_ylim[1])
    road_polys = []
    road_label_features = []
    for row in road_rows:
        geom = wkb.loads(row.geom)
        highway = row.highway
        name = row.name
        try:
            gdf_line = gpd.GeoSeries([geom], crs="EPSG:4326").to_crs(epsg=display_epsg)
            line_proj = gdf_line.iloc[0]
        except Exception:
            continue

        clipped = line_proj.intersection(extent_poly)
        if clipped.is_empty:
            continue

        road_label_features.append((clipped, name, highway))
        half_w = road_half_width_m(highway)
        try:
            road_polys.append((clipped.buffer(half_w, cap_style=2, join_style=2), half_w))
        except Exception:
            continue

    has_roads = len(road_rows) > 0
    draw_key_box(fig, has_buildings=has_buildings, has_roads=has_roads, has_rivers=has_rivers, font_scale=font_scale)

    road_union = None
    if road_polys:
        # Plot double-line road edges with width scaled to map scale
        road_union = None
        for poly, half_w in road_polys:
            total_width_m = max(2.0 * half_w, 1.0)
            mm_on_paper = (total_width_m / scale_ratio) * 1000.0
            lw_pts = max(0.6, mm_on_paper * 72.0 / 25.4)
            try:
                road_union = poly if road_union is None else road_union.union(poly)
                boundary = poly.boundary
                gpd.GeoSeries([boundary], crs=f"EPSG:{display_epsg}").plot(
                    ax=ax, color="dimgray", lw=lw_pts, zorder=6
                )
                gpd.GeoSeries([boundary], crs=f"EPSG:{display_epsg}").plot(
                    ax=ax, color="white", lw=lw_pts * 0.6, zorder=7
                )
            except Exception:
                continue

    # Label road names at midpoints between the two lines
    if road_label_features:
        seen_names = set()
        major_classes = {
            "trunk", "trunk_link", "motorway", "motorway_link",
            "primary", "primary_link", "secondary", "secondary_link",
            "tertiary", "tertiary_link",
        }
        for geom, name, highway in road_label_features:
            if not name or name in seen_names:
                continue
            if highway and highway.lower() not in major_classes:
                continue
            seen_names.add(name)
            try:
                mid = geom.interpolate(0.5, normalized=True)
                if geom.length <= min_label_length_m * 1.5:
                    continue
                angle = 0.0
                try:
                    p1 = geom.interpolate(0.45, normalized=True)
                    p2 = geom.interpolate(0.55, normalized=True)
                    angle = math.degrees(math.atan2(p2.y - p1.y, p2.x - p1.x))
                    if angle < -90 or angle > 90:
                        angle += 180
                except Exception:
                    pass
                ax.text(
                    mid.x,
                    mid.y,
                    name,
                    fontsize=int(7 * font_scale),
                    color="dimgray",
                    ha="center",
                    va="center",
                    rotation=angle,
                    weight="bold",
                    zorder=12,
                )
            except Exception:
                continue

    if buildings:
        gpd.GeoDataFrame(geometry=buildings, crs="EPSG:4326").to_crs(epsg=display_epsg).plot(
            ax=ax, facecolor="none", edgecolor="black", lw=1*font_scale, zorder=8
        )

    gdf_plot.plot(ax=ax, facecolor="none", edgecolor="red", lw=2*font_scale, zorder=20)
    ax.set_xlim(target_xlim)
    ax.set_ylim(target_ylim)

    major = nice_grid_step(max(ax.get_xlim()[1] - ax.get_xlim()[0], ax.get_ylim()[1] - ax.get_ylim()[0]))
    draw_grid(ax, poly, major / 5.0, major, font_scale)

    # Get first point coordinates for display
    first_coords = list(poly.exterior.coords)[0]
    first_station = station_names[0] if station_names and len(station_names) > 0 else "A"
    first_point_info = (first_station, first_coords[0], first_coords[1])

    draw_coordinate_frame(ax, major, font_scale, first_point_info)
    skipped_entries = annotate_vertices(
        ax,
        poly,
        plot_id,
        station_names,
        font_scale,
        min_label_length_m=min_label_length_m,
        avoid_geom=road_union,
        scale_ratio=scale_ratio,
    )
    draw_skipped_table(ax, skipped_entries, font_scale)

    add_north_arrow(ax, font_scale)
    add_scalebar(ax, 100 if scale_ratio <= 1000 else 500, font_scale=font_scale)

    ax.set_aspect("equal")
    ax.axis("off")

    fig.canvas.draw()
    plt.savefig(output_path, dpi=dpi, bbox_inches=None)
    plt.close(fig)
