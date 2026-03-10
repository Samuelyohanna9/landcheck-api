# app/utils/orthophoto_renderer.py

import matplotlib
matplotlib.use("Agg")

import os
import math
import re
import numpy as np
import geopandas as gpd
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from sqlalchemy import text
from shapely import wkb
from datetime import datetime
import contextily as ctx

from reportlab.lib.pagesizes import A4, A3, A2, A1, A0
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas

# Paper size mapping (ReportLab points)
PAPER_SIZES_REPORTLAB = {
    "A4": A4,
    "A3": A3,
    "A2": A2,
    "A1": A1,
    "A0": A0,
}

# Paper sizes in inches for matplotlib figures
PAPER_SIZES_IN = {
    "A4": (8.27, 11.69),     # 210 x 297 mm
    "A3": (11.69, 16.54),    # 297 x 420 mm
    "A2": (16.54, 23.39),    # 420 x 594 mm
    "A1": (23.39, 33.11),    # 594 x 841 mm
    "A0": (33.11, 46.81),    # 841 x 1189 mm
}

# Font scale factors relative to A4
SCALE_FACTORS = {
    "A4": 1.0,
    "A3": 1.25,
    "A2": 1.5,
    "A1": 1.8,
    "A0": 2.2,
}


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
    size = paper_size.upper() if paper_size else "A4"
    if size not in PAPER_SIZES_IN:
        size = "A4"
    return {
        "width": PAPER_SIZES_IN[size][0],
        "height": PAPER_SIZES_IN[size][1],
        "scale": SCALE_FACTORS[size],
        "name": size,
    }

# =======================
# Helpers
# =======================

def nice_grid_step(span_m: float) -> float:
    if span_m <= 0:
        return 100.0
    base = 10 ** math.floor(math.log10(span_m))
    steps = np.array([0.02, 0.05, 0.1, 0.2, 0.5, 1.0]) * base
    return float(steps[np.argmin(np.abs(steps - span_m / 6))])


def parse_scale_ratio(scale_text: str) -> int:
    try:
        s = str(scale_text).replace(" ", "")
        if ":" in s:
            return int(s.split(":")[1])
        return int(s)
    except Exception:
        return 1000


def apply_true_scale(ax, geom, scale_ratio, map_w_in, map_h_in):
    minx, miny, maxx, maxy = geom.bounds
    cx = (minx + maxx) / 2
    cy = (miny + maxy) / 2

    paper_w_m = map_w_in * 0.0254
    paper_h_m = map_h_in * 0.0254

    real_w = paper_w_m * scale_ratio
    real_h = paper_h_m * scale_ratio

    ax.set_xlim(cx - real_w / 2, cx + real_w / 2)
    ax.set_ylim(cy - real_h / 2, cy + real_h / 2)


# =======================
# Layout helpers
# =======================

def draw_sheet_frame(fig, font_scale=1.0):
    fig.add_artist(patches.Rectangle((0.02, 0.02), 0.96, 0.96,
                                     transform=fig.transFigure, fill=False, lw=2*font_scale, zorder=10))
    fig.add_artist(patches.Rectangle((0.03, 0.03), 0.94, 0.94,
                                     transform=fig.transFigure, fill=False, lw=0.8*font_scale, zorder=10))


def draw_title_block(fig, title_text, plot_id, scale_text, location, lga, state, font_scale=1.0):
    # Plot number at top right corner for identification
    fig.text(0.94, 0.95, f"Plot #{plot_id}", ha="right", fontsize=int(8*font_scale), weight="bold",)
    
    y = 0.955
    fig.text(0.5, y, title_text, ha="center", fontsize=int(12*font_scale), weight="bold")
    fig.text(0.5, y-0.030, f"LOCATED AT: {location}", ha="center", fontsize=int(9*font_scale))
    fig.text(0.5, y-0.050, lga, ha="center", fontsize=int(9*font_scale))
    fig.text(0.5, y-0.070, state, ha="center", fontsize=int(9*font_scale))
    fig.text(0.5, y-0.100, f"SCALE {scale_text}", ha="center", fontsize=int(9*font_scale))


def draw_footer(fig, crs, source, surveyor, rank, font_scale=1.0):
    y = 0.155
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    fig.text(0.05, y, f"SURVEYOR: {surveyor}", fontsize=int(8*font_scale))
    fig.text(0.05, y-0.018, f"RANK: {rank}", fontsize=int(8*font_scale))
    fig.text(0.05, y-0.036, "SIGNATURE: ____________________", fontsize=int(8*font_scale))
    fig.text(0.05, y-0.054, f"DATE PRINTED: {now}", fontsize=int(8*font_scale))
    fig.text(0.05, 0.05, crs, fontsize=int(7*font_scale), color="blue")
    fig.text(0.95, 0.05, source, fontsize=int(7*font_scale), ha="right")


def add_north_arrow(ax, font_scale=1.0, style: str = "one_side_stem", color: str = "black"):
    fig = ax.figure
    col = "blue" if str(color).lower() == "blue" else "black"
    style = (style or "one_side_stem").lower()
    box = ax.get_position()
    x = float(box.x1)
    y = min(0.93, float(box.y1) + 0.060)
    size = 0.032 * max(0.8, font_scale)

    if style in ("one_side_stem", "one-sided-stem", "oneside_stem", "stacked_4n", "stacked4n", "vertical_4n"):
        stem_top = y + size * 0.50
        stem_bottom = y - size * 1.05
        four_y = y + size * 0.80
        n_y = y - size * 0.12
        n_x = x + size * 0.05
        n_gap = size * 0.26
        lower_stem_top = n_y - (n_gap / 2.0)
        upper_stem_bottom = n_y + (n_gap / 2.0)
        line_lw = max(0.7, 0.90 * font_scale)
        fig.add_artist(
            mlines.Line2D(
                [x, x],
                [stem_bottom, lower_stem_top],
                transform=fig.transFigure,
                color=col,
                lw=line_lw,
                zorder=20,
                solid_capstyle="butt",
            )
        )
        fig.add_artist(
            mlines.Line2D(
                [x, x],
                [upper_stem_bottom, stem_top],
                transform=fig.transFigure,
                color=col,
                lw=line_lw,
                zorder=20,
                solid_capstyle="butt",
            )
        )
        fig.text(
            x,
            four_y,
            "4",
            ha="center",
            va="center",
            fontsize=int(11.0 * font_scale),
            color=col,
            weight="normal",
            fontfamily="DejaVu Sans",
            zorder=25,
        )
        fig.text(
            n_x,
            n_y,
            "N",
            ha="center",
            va="center",
            fontsize=int(10.0 * font_scale),
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



def choose_scalebar_length(scale_ratio):
    if scale_ratio <= 500: return 50
    if scale_ratio <= 1000: return 100
    if scale_ratio <= 2000: return 200
    return 500


def add_scalebar(ax, length_m, segments=4, font_scale=1.0):
    trans = ax.transAxes
    x0, y0, bar_h = 0.32, -0.12, 0.018
    seg = 0.25 / segments

    for i in range(segments):
        face = "black" if i % 2 == 0 else "white"
        ax.add_patch(patches.Rectangle(
            (x0 + i * seg, y0), seg, bar_h,
            transform=trans, facecolor=face, edgecolor="black", lw=0.8*font_scale, clip_on=False, zorder=15
        ))

    ax.add_patch(patches.Rectangle(
        (x0, y0), 0.25, bar_h,
        transform=trans, fill=False, edgecolor="black", lw=1.2*font_scale, clip_on=False, zorder=16
    ))

    ax.text(x0, y0 - 0.04, "0", transform=trans, ha="center", fontsize=int(8*font_scale))
    for i in range(1, segments + 1):
        ax.text(x0 + i * seg, y0 - 0.04, f"{int(length_m * i / segments)}",
                transform=trans, ha="center", fontsize=int(8*font_scale))
    ax.text(x0 + 0.125, y0 + bar_h + 0.02, "meters", transform=trans, ha="center", fontsize=int(8*font_scale))


def draw_grid(ax, minor, major, font_scale=1.0):
    xmin, xmax = ax.get_xlim()
    ymin, ymax = ax.get_ylim()

    # Tick-only grid to keep map area clean
    tick_len = (xmax - xmin) * 0.01
    xs = np.arange(math.floor(xmin / major) * major, xmax + 0.1, major)
    ys = np.arange(math.floor(ymin / major) * major, ymax + 0.1, major)

    for x in xs:
        if x < xmin or x > xmax:
            continue
        ax.plot([x, x], [ymax, ymax - tick_len], color="blue", lw=0.6*font_scale, alpha=0.5, zorder=3)
        ax.plot([x, x], [ymin, ymin + tick_len], color="blue", lw=0.6*font_scale, alpha=0.5, zorder=3)

    for y in ys:
        if y < ymin or y > ymax:
            continue
        ax.plot([xmin, xmin + tick_len], [y, y], color="blue", lw=0.6*font_scale, alpha=0.5, zorder=3)
        ax.plot([xmax, xmax - tick_len], [y, y], color="blue", lw=0.6*font_scale, alpha=0.5, zorder=3)


def annotate_vertices_orthophoto(ax, poly, station_names=None, font_scale=1.0, scale_ratio: int = 1000):
    """Add station name labels to plot vertices in orthophoto."""
    from shapely.geometry import Point

    coords = list(poly.exterior.coords)
    default_labels = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    labels = station_names if station_names else default_labels

    for i in range(len(coords) - 1):
        p1 = Point(coords[i])
        p2 = Point(coords[i + 1])
        label = format_station_label(labels[i % len(labels)])

        seg_dx = p2.x - p1.x
        seg_dy = p2.y - p1.y
        seg_len = math.hypot(seg_dx, seg_dy) or 1.0
        nx, ny = -seg_dy / seg_len, seg_dx / seg_len
        station_offset = max(2.0, (5.0 / 1000.0) * scale_ratio)
        lx = p1.x + nx * station_offset
        ly = p1.y + ny * station_offset

        ax.text(
            lx,
            ly,
            label,
            fontsize=int(8*font_scale),
            color="black",
            ha="center",
            va="center",
            weight="bold",
            multialignment="center",
            linespacing=0.95,
            zorder=25,
            bbox=dict(facecolor="white", edgecolor="black", boxstyle="round,pad=0.15", alpha=0.85)
        )


def draw_coordinate_frame(ax, spacing, axis_epsg=3857, label_epsg=3857, font_scale=1.0):
    """Draw coordinate frame with labels in the requested coordinate system."""
    from pyproj import Transformer

    xmin, xmax = ax.get_xlim()
    ymin, ymax = ax.get_ylim()
    pad = (xmax - xmin) * 0.035

    ax.add_patch(patches.Rectangle((xmin-pad, ymin-pad), (xmax-xmin)+2*pad, (ymax-ymin)+2*pad,
                                   fill=False, lw=1.5*font_scale, clip_on=False, zorder=10))
    ax.add_patch(patches.Rectangle((xmin, ymin), (xmax-xmin), (ymax-ymin),
                                   fill=False, lw=1.0*font_scale, zorder=10))

    transformer = None
    if axis_epsg != label_epsg:
        transformer = Transformer.from_crs(axis_epsg, label_epsg, always_xy=True)

    xs = np.arange(math.floor(xmin / spacing) * spacing, xmax + 0.1, spacing)
    ys = np.arange(math.floor(ymin / spacing) * spacing, ymax + 0.1, spacing)

    # Draw easting labels at the top
    for x in xs:
        if x >= xmin and x <= xmax:
            if transformer:
                tx, _ = transformer.transform(x, (ymin + ymax) / 2)
                label = f"{int(tx)}"
            else:
                label = f"{int(x)}"
            ax.text(x, ymax + pad*0.45, label, ha="center", fontsize=int(7*font_scale), color="blue", zorder=11)

    # Draw northing labels on both sides
    for y in ys:
        if y >= ymin and y <= ymax:
            if transformer:
                _, ty = transformer.transform((xmin + xmax) / 2, y)
                label = f"{int(ty)}"
            else:
                label = f"{int(y)}"
            ax.text(xmin-pad*0.45, y, label, va="center", ha="right", fontsize=int(7*font_scale), color="blue", rotation=90, zorder=11)
            ax.text(xmax+pad*0.45, y, label, va="center", ha="left", fontsize=int(7*font_scale), color="blue", rotation=90, zorder=11)


# =======================
# Main Renderer
# =======================

def render_orthophoto_png(
    db, plot_id, output_path,
    title_text="ORTHOPHOTO", location_text="", lga_text="", state_text="",
    scale_text="1 : 1000", crs_footer_text="ORIGIN: WGS84 (UTM Projection)",
    source_footer_text="SOURCE: LandCheck System", surveyor_name="", surveyor_rank="",
    tile_source="esri", station_names=None,
    coordinate_system="wgs84", epsg_code=4326,
    use_topo_map=False,
    paper_size="A4",
    north_arrow_style="one_side_stem",
    north_arrow_color="black",
    preview_mode: bool = False,
):
    # Fetch Geometry from DB
    res = db.execute(text("SELECT geom FROM plots WHERE id=:id"), {"id": plot_id}).fetchone()
    if not res: 
        raise ValueError("Plot not found")

    plot_geom = wkb.loads(res[0])

    display_epsg = epsg_code
    if coordinate_system == "wgs84" or epsg_code == 4326:
        centroid = plot_geom.centroid
        utm_zone = int((centroid.x + 180) / 6) + 1
        hemisphere = "north" if centroid.y >= 0 else "south"
        display_epsg = 32600 + utm_zone if hemisphere == "north" else 32700 + utm_zone

    gdf_plot = gpd.GeoDataFrame(geometry=[plot_geom], crs="EPSG:4326").to_crs(epsg=display_epsg)
    poly = gdf_plot.geometry.iloc[0]

    # Canvas setup based on paper size
    paper_config = get_paper_config(paper_size)
    fig_width = paper_config["width"]
    fig_height = paper_config["height"]
    font_scale = paper_config["scale"]
    if preview_mode:
        dpi = 120
    else:
        dpi = 200 if paper_config["name"] in ["A4", "A3"] else 150 if paper_config["name"] == "A2" else 100

    fig = plt.figure(figsize=(fig_width, fig_height), dpi=dpi)
    canvas_agg = FigureCanvas(fig)
    
    # Match survey plan layout so scale/plot size aligns across previews
    map_left, map_bottom, map_width, map_height = 0.10, 0.30, 0.80, 0.45
    ax = fig.add_axes([map_left, map_bottom, map_width, map_height])

    scale_ratio = parse_scale_ratio(scale_text)
    apply_true_scale(ax, poly, scale_ratio, fig_width * map_width, fig_height * map_height)
    target_xlim = ax.get_xlim()
    target_ylim = ax.get_ylim()

    # Basemap - choose based on use_topo_map setting
    basemap_loaded = False
    axis_crs = f"EPSG:{display_epsg}"

    if use_topo_map:
        topo_zoom = 14 if preview_mode else 15
        if preview_mode:
            # Fast path for previews: single provider attempt to avoid stacked network timeouts.
            try:
                ctx.add_basemap(
                    ax,
                    source=ctx.providers.OpenTopoMap,
                    crs=axis_crs,
                    attribution=False,
                    zoom=topo_zoom,
                    reset_extent=True,
                )
                basemap_loaded = True
            except Exception:
                basemap_loaded = False
        else:
        # Use OpenTopoMap for terrain/elevation visualization
            topo_sources = [
                ("OpenTopoMap", "https://tile.opentopomap.org/{z}/{x}/{y}.png"),
                ("Stamen Terrain", "https://stamen-tiles.a.ssl.fastly.net/terrain/{z}/{x}/{y}.png"),
            ]

            for name, url in topo_sources:
                try:
                    ctx.add_basemap(
                        ax,
                        source=url,
                        crs=axis_crs,
                        attribution=False,
                        zoom=topo_zoom,
                        reset_extent=True,
                    )
                    basemap_loaded = True
                    print(f"Loaded topo basemap from {name}")
                    break
                except Exception as e:
                    print(f"{name} failed: {e}")
                    continue

            if not basemap_loaded:
                # Fallback to contextily OpenTopoMap provider
                try:
                    ctx.add_basemap(
                        ax,
                        source=ctx.providers.OpenTopoMap,
                        crs=axis_crs,
                        attribution=False,
                        zoom=topo_zoom,
                        reset_extent=True,
                    )
                    basemap_loaded = True
                except Exception as e:
                    print(f"OpenTopoMap provider failed: {e}")
    else:
        sat_zoom = 16 if preview_mode else 17
        if preview_mode:
            # Fast path for previews: no long fallback chain.
            try:
                ctx.add_basemap(
                    ax,
                    source=ctx.providers.Esri.WorldImagery,
                    crs=axis_crs,
                    attribution=False,
                    zoom=sat_zoom,
                    reset_extent=True,
                )
                basemap_loaded = True
            except Exception:
                try:
                    ctx.add_basemap(
                        ax,
                        source=ctx.providers.OpenStreetMap.Mapnik,
                        crs=axis_crs,
                        attribution=False,
                        zoom=sat_zoom,
                        reset_extent=True,
                    )
                    basemap_loaded = True
                except Exception:
                    basemap_loaded = False
        else:
        # Use satellite/aerial imagery
            basemap_sources = [
                ("Esri WorldImagery", "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"),
                ("OpenStreetMap", "https://tile.openstreetmap.org/{z}/{x}/{y}.png"),
                ("CartoDB Light", "https://basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png"),
            ]

            # First try the contextily providers
            try:
                ctx.add_basemap(
                    ax,
                    source=ctx.providers.Esri.WorldImagery,
                    crs=axis_crs,
                    attribution=False,
                    zoom=sat_zoom,
                    reset_extent=True,
                )
                basemap_loaded = True
            except Exception as e:
                print(f"Esri WorldImagery failed: {e}")

            if not basemap_loaded:
                try:
                    ctx.add_basemap(
                        ax,
                        source=ctx.providers.OpenStreetMap.Mapnik,
                        crs=axis_crs,
                        attribution=False,
                        zoom=sat_zoom,
                        reset_extent=True,
                    )
                    basemap_loaded = True
                except Exception as e:
                    print(f"OpenStreetMap failed: {e}")

            if not basemap_loaded:
                # Try URL-based approach as last resort
                for name, url in basemap_sources:
                    try:
                        ctx.add_basemap(
                            ax,
                            source=url,
                            crs=axis_crs,
                            attribution=False,
                            zoom=sat_zoom,
                            reset_extent=True,
                        )
                        basemap_loaded = True
                        print(f"Loaded basemap from {name}")
                        break
                    except Exception as e:
                        print(f"{name} failed: {e}")
                        continue

    if not basemap_loaded:
        # Add a light green background to represent land if no basemap loads
        ax.set_facecolor('#e8f4e8')
        map_type = "Topo" if use_topo_map else "Satellite"
        ax.text(0.5, 0.5, f"{map_type} imagery temporarily unavailable\nShowing plot boundary",
                transform=ax.transAxes, ha="center", va="center", fontsize=10, color="#555", alpha=0.8)

    # Restore target extent after basemap
    ax.set_xlim(target_xlim)
    ax.set_ylim(target_ylim)

    # Plot boundary (GeoPandas can autoscale; reapply limits afterward)
    gdf_plot.plot(ax=ax, facecolor="none", edgecolor="red", lw=2, zorder=20)
    ax.set_xlim(target_xlim)
    ax.set_ylim(target_ylim)

    # Grid Calculation
    span = max(ax.get_xlim()[1] - ax.get_xlim()[0], ax.get_ylim()[1] - ax.get_ylim()[0])
    major = nice_grid_step(span)

    # Features
    draw_grid(ax, major/5, major, font_scale)
    draw_coordinate_frame(ax, major, axis_epsg=display_epsg, label_epsg=display_epsg, font_scale=font_scale)

    # Add station name labels to vertices
    annotate_vertices_orthophoto(ax, poly, station_names, font_scale, scale_ratio=scale_ratio)

    draw_sheet_frame(fig, font_scale)
    draw_title_block(fig, title_text, plot_id, scale_text, location_text, lga_text, state_text, font_scale)
    draw_footer(fig, crs_footer_text, source_footer_text, surveyor_name, surveyor_rank, font_scale)
    add_north_arrow(ax, font_scale, style=north_arrow_style, color=north_arrow_color)
    add_scalebar(ax, choose_scalebar_length(scale_ratio), font_scale=font_scale)

    ax.set_aspect("equal")
    ax.axis("off")

    # Save logic
    fig.canvas.draw()
    fig.savefig(output_path, dpi=dpi)
    plt.close(fig)


def render_orthophoto_pdf_from_png(png_path, pdf_path, paper_size="A4"):
    """Convert PNG to PDF with specified paper size"""
    try:
        if not os.path.exists(png_path) or os.path.getsize(png_path) < 2000:
            raise RuntimeError("Generated PNG is invalid or missing.")

        page_size = PAPER_SIZES_REPORTLAB.get(paper_size.upper(), A4)
        c = canvas.Canvas(pdf_path, pagesize=page_size)
        w, h = page_size
        img = ImageReader(png_path)
        c.drawImage(img, 0, 0, w, h, preserveAspectRatio=True)
        c.showPage()
        c.save()

    finally:
        # THE CLEANUP: Delete the PNG after the PDF is created
        if os.path.exists(png_path):
            os.remove(png_path)
            print(f"Cleaned up temporary image: {png_path}")
