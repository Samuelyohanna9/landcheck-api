# app/utils/map_renderer_layout.py
# A4 PORTRAIT – Professional survey plan layout with TRUE SCALE rendering

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
# Geometry & Scale Helpers
# ======================

def calculate_bearing_deg(p1: Point, p2: Point) -> float:
    dx, dy = p2.x - p1.x, p2.y - p1.y
    return (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0

def nice_grid_step(span_m: float) -> float:
    if span_m <= 0: return 100.0
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
    fig.add_artist(patches.Rectangle((0.02, 0.02), 0.96, 0.96, transform=fig.transFigure, fill=False, lw=2))
    fig.add_artist(patches.Rectangle((0.03, 0.03), 0.94, 0.94, transform=fig.transFigure, fill=False, lw=0.8))

def draw_title_block(fig, title_text, plot_id, area_m2, scale_text, location_text, lga_text, state_text, station_text):
    y = 0.955
    fig.text(0.5, y, str(title_text), ha="center", fontsize=12, weight="bold")
    fig.text(0.5, y - 0.025, f"OF PLOT {plot_id}", ha="center", fontsize=10)
    fig.text(0.5, y - 0.055, f"STATION: {station_text}", ha="center", fontsize=9)
    fig.text(0.5, y - 0.075, f"LOCATED AT: {location_text}", ha="center", fontsize=9)
    fig.text(0.5, y - 0.095, str(lga_text), ha="center", fontsize=9)
    fig.text(0.5, y - 0.115, str(state_text), ha="center", fontsize=9)
    fig.text(0.5, y - 0.145, f"AREA = {area_m2/10000:.4f} HA.", ha="center", fontsize=9, color="red")
    fig.text(0.5, y - 0.165, f"SCALE  {scale_text}", ha="center", fontsize=9)

def draw_footer(fig, crs_text, source_text, surveyor, rank):
    y_top = 0.185
    y_bot = 0.055 
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    fig.text(0.06, y_top, f"SURVEYOR: {surveyor}", fontsize=9)
    fig.text(0.06, y_top - 0.025, f"RANK: {rank}", fontsize=9)
    fig.text(0.06, y_top - 0.050, "SIGNATURE: ____________________", fontsize=9)
    fig.text(0.06, y_top - 0.075, f"DATE PRINTED: {now}", fontsize=9)

    fig.text(0.06, y_bot, str(crs_text), fontsize=8, color="blue")
    fig.text(0.94, y_bot, str(source_text), fontsize=8, ha="right")

def draw_key_box(fig):
    w, h = 0.28, 0.11
    x, y = 0.50 - w / 2.0, 0.08 
    
    fig.add_artist(patches.Rectangle((x, y), w, h, transform=fig.transFigure, fill=False, lw=0.9))
    fig.text(x + w / 2.0, y + h - 0.02, "KEY", ha="center", fontsize=8, weight="bold")
    
    items = [("PERIMETER (Plot)", "red", 2), ("BUILDINGS", "black", 1), 
              ("ROADS", "dimgray", 1), ("RIVERS", "blue", 1)]
    
    yy = y + h - 0.045
    for lbl, col, lw in items:
        line = mlines.Line2D([x + 0.03, x + 0.10], [yy, yy], transform=fig.transFigure, color=col, lw=lw)
        fig.add_artist(line)
        fig.text(x + 0.12, yy, lbl, fontsize=7, va="center")
        yy -= 0.02

# ======================
# Map Decorations
# ======================

def add_north_arrow(ax):
    # Using ax.annotate but with figure fraction to hit the red shaded spot
    ax.annotate("N", xy=(0.85, 0.90), xytext=(0.85, 0.83), xycoords="figure fraction",
                arrowprops=dict(facecolor="black", width=2, headwidth=8), ha="center", 
                fontsize=12, weight="bold", zorder=20)

def add_scalebar(ax, length_m: float, segments: int = 4):
    x0, y0, bar_h, total_w = 0.225, -0.15, 0.012, 0.55 
    seg_w = total_w / float(segments)

    for i in range(segments):
        xi = x0 + i * seg_w
        face = "black" if i % 2 == 0 else "white"
        ax.add_patch(patches.Rectangle((xi, y0), seg_w, bar_h, transform=ax.transAxes, 
                                        facecolor=face, edgecolor="black", linewidth=0.8, clip_on=False))

    ax.add_patch(patches.Rectangle((x0, y0), total_w, bar_h, transform=ax.transAxes, 
                                    fill=False, edgecolor="black", linewidth=1.2, clip_on=False))

    ax.text(x0, y0 - 0.03, "0", transform=ax.transAxes, ha="center", va="top", fontsize=7, clip_on=False)
    for i in range(1, segments + 1):
        value = int(round((length_m / segments) * i))
        ax.text(x0 + i * seg_w, y0 - 0.03, f"{value}", transform=ax.transAxes, 
                ha="center", va="top", fontsize=7, clip_on=False)

    ax.text(x0 + total_w / 2.0, y0 + 0.025, "meters", transform=ax.transAxes, ha="center", fontsize=7, clip_on=False)

# ======================
# Grid & Annotations
# ======================

def draw_grid(ax, plot_poly, minor: float, major: float):
    xmin, xmax = ax.get_xlim()
    ymin, ymax = ax.get_ylim()
    def draw(step, lw, alpha):
        xs = np.arange(math.floor(xmin / step) * step, xmax + step, step)
        ys = np.arange(math.floor(ymin / step) * step, ymax + step, step)
        for x in xs:
            g = LineString([(x, ymin), (x, ymax)]).difference(plot_poly)
            for gg in getattr(g, "geoms", [g]):
                if not gg.is_empty: ax.plot(*gg.xy, color="blue", lw=lw, alpha=alpha)
        for y in ys:
            g = LineString([(xmin, y), (xmax, y)]).difference(plot_poly)
            for gg in getattr(g, "geoms", [g]):
                if not gg.is_empty: ax.plot(*gg.xy, color="blue", lw=lw, alpha=alpha)
    draw(minor, 0.3, 0.20); draw(major, 1.0, 0.60)

def draw_coordinate_frame(ax, spacing: float):
    xmin, xmax = ax.get_xlim()
    ymin, ymax = ax.get_ylim()
    pad = (xmax - xmin) * 0.035
    ax.add_patch(patches.Rectangle((xmin - pad, ymin - pad), (xmax - xmin) + 2 * pad, 
                                    (ymax - ymin) + 2 * pad, fill=False, lw=1.5, clip_on=False))
    ax.add_patch(patches.Rectangle((xmin, ymin), (xmax-xmin), (ymax-ymin), fill=False, lw=1.0, clip_on=False))

    xs = np.arange(math.floor(xmin / spacing) * spacing, xmax + 0.1, spacing)
    ys = np.arange(math.floor(ymin / spacing) * spacing, ymax + 0.1, spacing)
    for x in xs:
        ax.text(x, ymax + pad * 0.45, f"{int(round(x))}", ha="center", fontsize=7, color="blue")
    for y in ys:
        if y < ymin + spacing * 0.9: continue
        ax.text(xmin - pad * 0.45, y, f"{int(round(y))}", va="center", ha="right", fontsize=7, color="blue", rotation=90)
        ax.text(xmax + pad * 0.45, y, f"{int(round(y))}", va="center", ha="left", fontsize=7, color="blue", rotation=90)

def annotate_vertices(ax, poly, plot_id: int, station_names=None):
    coords = list(poly.exterior.coords)
    default_labels = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    labels = station_names if station_names else default_labels

    for i in range(len(coords) - 1):
        p1, p2 = Point(coords[i]), Point(coords[i + 1])
        label = labels[i % len(labels)]
        ax.text(p1.x, p1.y, label, fontsize=8, color="blue", ha="center", va="center",
                bbox=dict(facecolor="white", edgecolor="blue", boxstyle="circle,pad=0.2"))

        bearing, dist = calculate_bearing_deg(p1, p2), p1.distance(p2)
        mx, my = (p1.x + p2.x) / 2.0, (p1.y + p2.y) / 2.0
        ang = math.degrees(math.atan2(p2.y - p1.y, p2.x - p1.x))
        if ang < -90 or ang > 90: ang += 180
        ax.text(mx, my, f"{bearing:.1f}°\n{dist:.1f}m", fontsize=6.5, ha="center", va="center", rotation=ang)

    ax.text(poly.centroid.x, poly.centroid.y, f"PLOT {plot_id}", fontsize=9, weight="bold", ha="center",
            bbox=dict(facecolor="white", alpha=0.8))

# ======================
# Main Renderer Function
# ======================

def render_plot_map_layout(db, plot_id: int, output_path: str, title_text: str = "SURVEY PLAN",
                           location_text: str = "LOC", lga_text: str = "LGA", state_text: str = "STATE",
                           station_text: str = "STN", scale_text: str = "1 : 1000", 
                           crs_footer_text: str = "ORIGIN: WGS84", source_footer_text: str = "SOURCE: LandCheck",
                           surveyor_name: str = "SURV", surveyor_rank: str = "RANK", station_names=None):

    plot_wkb = db.execute(text("SELECT geom FROM plots WHERE id=:id"), {"id": plot_id}).scalar()
    rows = db.execute(text("SELECT geom, feature_type FROM detected_features WHERE plot_id=:id"), {"id": plot_id}).fetchall()
    if not plot_wkb: raise ValueError("Plot not found")
    
    plot_geom = wkb.loads(plot_wkb)
    buildings, roads, rivers = [], [], []
    for r in rows:
        g = wkb.loads(r.geom)
        if r.feature_type == "building": buildings.append(g)
        elif r.feature_type == "road": roads.append(g)
        elif r.feature_type == "river": rivers.append(g)

    gdf_plot = gpd.GeoDataFrame(geometry=[plot_geom], crs="EPSG:4326").to_crs(3857)
    poly = gdf_plot.geometry.iloc[0]
    area_m2 = float(poly.area)

    fig = plt.figure(figsize=(8.27, 11.69), dpi=200)
    canvas_obj = FigureCanvas(fig)
    
    map_left, map_bottom, map_width, map_height = 0.10, 0.30, 0.80, 0.45
    ax = fig.add_axes([map_left, map_bottom, map_width, map_height])

    draw_sheet_frame(fig)
    draw_title_block(fig, title_text, plot_id, area_m2, scale_text, location_text, lga_text, state_text, station_text)
    draw_footer(fig, crs_footer_text, source_footer_text, surveyor_name, surveyor_rank)
    draw_key_box(fig)

    if rivers: gpd.GeoDataFrame(geometry=rivers, crs="EPSG:4326").to_crs(3857).plot(ax=ax, color="blue", lw=1.2)
    if roads: gpd.GeoDataFrame(geometry=roads, crs="EPSG:4326").to_crs(3857).plot(ax=ax, color="dimgray", lw=1.2)
    if buildings: gpd.GeoDataFrame(geometry=buildings, crs="EPSG:4326").to_crs(3857).plot(ax=ax, facecolor="none", edgecolor="black")
    gdf_plot.plot(ax=ax, facecolor="none", edgecolor="red", lw=2)

    scale_ratio = parse_scale_ratio(scale_text)
    apply_true_scale(ax, poly, scale_ratio, 8.27 * map_width, 11.69 * map_height)
    
    major = nice_grid_step(max(ax.get_xlim()[1]-ax.get_xlim()[0], ax.get_ylim()[1]-ax.get_ylim()[0]))
    draw_grid(ax, poly, major/5.0, major)
    draw_coordinate_frame(ax, major)
    annotate_vertices(ax, poly, plot_id, station_names)
    
    # FIXED: Passing ax to the arrow function
    add_north_arrow(ax)
    add_scalebar(ax, 100 if scale_ratio <= 1000 else 500)

    ax.set_aspect("equal")
    ax.axis("off")

    fig.canvas.draw()
    plt.savefig(output_path, dpi=200, bbox_inches=None) 
    plt.close(fig)