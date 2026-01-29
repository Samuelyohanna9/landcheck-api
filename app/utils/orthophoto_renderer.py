# app/utils/orthophoto_renderer.py

import matplotlib
matplotlib.use("Agg")

import os
import math
import numpy as np
import geopandas as gpd
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from sqlalchemy import text
from shapely import wkb
from datetime import datetime
import contextily as ctx

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas

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

def draw_sheet_frame(fig):
    fig.add_artist(patches.Rectangle((0.02, 0.02), 0.96, 0.96,
                                     transform=fig.transFigure, fill=False, lw=2, zorder=10))
    fig.add_artist(patches.Rectangle((0.03, 0.03), 0.94, 0.94,
                                     transform=fig.transFigure, fill=False, lw=0.8, zorder=10))


def draw_title_block(fig, title_text, plot_id, scale_text, location, lga, state, station):
    y = 0.955
    fig.text(0.5, y, title_text, ha="center", fontsize=12, weight="bold")
    fig.text(0.5, y-0.025, f"OF PLOT {plot_id}", ha="center", fontsize=10)
    fig.text(0.5, y-0.055, f"STATION: {station}", ha="center", fontsize=9)
    fig.text(0.5, y-0.075, f"LOCATED AT: {location}", ha="center", fontsize=9)
    fig.text(0.5, y-0.095, lga, ha="center", fontsize=9)
    fig.text(0.5, y-0.115, state, ha="center", fontsize=9)
    fig.text(0.5, y-0.145, f"SCALE {scale_text}", ha="center", fontsize=9)


def draw_footer(fig, crs, source, surveyor, rank):
    y = 0.155
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    fig.text(0.05, y, f"SURVEYOR: {surveyor}", fontsize=8)
    fig.text(0.05, y-0.018, f"RANK: {rank}", fontsize=8)
    fig.text(0.05, y-0.036, "SIGNATURE: ____________________", fontsize=8)
    fig.text(0.05, y-0.054, f"DATE PRINTED: {now}", fontsize=8)
    fig.text(0.05, 0.05, crs, fontsize=7, color="blue")
    fig.text(0.95, 0.05, source, fontsize=7, ha="right")


def add_north_arrow(ax):
    ax.annotate(
        "N",
        xy=(0.93, 0.90),
        xytext=(0.93, 0.80),
        xycoords="axes fraction",
        arrowprops=dict(facecolor="black", width=2, headwidth=8),
        ha="center",
        fontsize=12,
        weight="bold",
        zorder=15
    )


def choose_scalebar_length(scale_ratio):
    if scale_ratio <= 500: return 50
    if scale_ratio <= 1000: return 100
    if scale_ratio <= 2000: return 200
    return 500


def add_scalebar(ax, length_m, segments=4):
    trans = ax.transAxes
    x0, y0, bar_h = 0.32, -0.12, 0.018
    seg = 0.25 / segments

    for i in range(segments):
        face = "black" if i % 2 == 0 else "white"
        ax.add_patch(patches.Rectangle(
            (x0 + i * seg, y0), seg, bar_h,
            transform=trans, facecolor=face, edgecolor="black", lw=0.8, clip_on=False, zorder=15
        ))

    ax.add_patch(patches.Rectangle(
        (x0, y0), 0.25, bar_h,
        transform=trans, fill=False, edgecolor="black", lw=1.2, clip_on=False, zorder=16
    ))

    ax.text(x0, y0 - 0.04, "0", transform=trans, ha="center", fontsize=8)
    for i in range(1, segments + 1):
        ax.text(x0 + i * seg, y0 - 0.04, f"{int(length_m * i / segments)}",
                transform=trans, ha="center", fontsize=8)
    ax.text(x0 + 0.125, y0 + bar_h + 0.02, "meters", transform=trans, ha="center", fontsize=8)


def draw_grid(ax, minor, major):
    xmin, xmax = ax.get_xlim()
    ymin, ymax = ax.get_ylim()

    for step, lw, a in [(minor, 0.3, 0.2), (major, 1.0, 0.6)]:
        xs = np.arange(math.floor(xmin / step) * step, xmax + step, step)
        ys = np.arange(math.floor(ymin / step) * step, ymax + step, step)

        for x in xs:
            ax.plot([x, x], [ymin, ymax], color="blue", lw=lw, alpha=a, zorder=3)
        for y in ys:
            ax.plot([xmin, xmax], [y, y], color="blue", lw=lw, alpha=a, zorder=3)


def draw_coordinate_frame(ax, spacing):
    xmin, xmax = ax.get_xlim()
    ymin, ymax = ax.get_ylim()
    pad = (xmax - xmin) * 0.035

    ax.add_patch(patches.Rectangle((xmin-pad, ymin-pad), (xmax-xmin)+2*pad, (ymax-ymin)+2*pad,
                                   fill=False, lw=1.5, clip_on=False, zorder=10))
    ax.add_patch(patches.Rectangle((xmin, ymin), (xmax-xmin), (ymax-ymin),
                                   fill=False, lw=1.0, zorder=10))

    xs = np.arange(math.floor(xmin / spacing) * spacing, xmax + 0.1, spacing)
    ys = np.arange(math.floor(ymin / spacing) * spacing, ymax + 0.1, spacing)

    for x in xs:
        ax.text(x, ymax + pad*0.45, f"{int(x)}", ha="center", fontsize=7, color="blue", zorder=11)

    for y in ys:
        if y < ymin + spacing * 0.9: continue
        ax.text(xmin-pad*0.45, y, f"{int(y)}", va="center", ha="right", fontsize=7, color="blue", rotation=90, zorder=11)
        ax.text(xmax+pad*0.45, y, f"{int(y)}", va="center", ha="left", fontsize=7, color="blue", rotation=90, zorder=11)


# =======================
# Main Renderer
# =======================

def render_orthophoto_png(
    db, plot_id, output_path,
    title_text="ORTHOPHOTO", location_text="", lga_text="", state_text="",
    station_text="", scale_text="1 : 1000", crs_footer_text="ORIGIN: WGS84 (UTM Projection)",
    source_footer_text="SOURCE: LandCheck System", surveyor_name="", surveyor_rank="",
    tile_source="esri"
):
    # Fetch Geometry from DB
    res = db.execute(text("SELECT geom FROM plots WHERE id=:id"), {"id": plot_id}).fetchone()
    if not res: 
        raise ValueError("Plot not found")

    gdf_plot = gpd.GeoDataFrame(geometry=[wkb.loads(res[0])], crs="EPSG:4326").to_crs(3857)
    poly = gdf_plot.geometry.iloc[0]

    # Explicit Canvas Setup for reliability
    fig = plt.figure(figsize=(8.27, 11.69), dpi=200)
    canvas_agg = FigureCanvas(fig)
    
    map_left, map_bottom, map_width, map_height = 0.10, 0.24, 0.80, 0.52
    ax = fig.add_axes([map_left, map_bottom, map_width, map_height])

    scale_ratio = parse_scale_ratio(scale_text)
    apply_true_scale(ax, poly, scale_ratio, 8.27 * map_width, 11.69 * map_height)

    # Basemap
    source = ctx.providers.Esri.WorldImagery if tile_source == "esri" else ctx.providers.OpenStreetMap.Mapnik
    try:
        ctx.add_basemap(ax, source=source, crs="EPSG:3857", attribution=False)
    except Exception:
        pass

    # Grid Calculation
    span = max(ax.get_xlim()[1] - ax.get_xlim()[0], ax.get_ylim()[1] - ax.get_ylim()[0])
    major = nice_grid_step(span)

    # Features
    draw_grid(ax, major/5, major)
    draw_coordinate_frame(ax, major)
    gdf_plot.plot(ax=ax, facecolor="none", edgecolor="red", lw=2, zorder=20)
    
    draw_sheet_frame(fig)
    draw_title_block(fig, title_text, plot_id, scale_text, location_text, lga_text, state_text, station_text)
    draw_footer(fig, crs_footer_text, source_footer_text, surveyor_name, surveyor_rank)
    add_north_arrow(ax)
    add_scalebar(ax, choose_scalebar_length(scale_ratio))

    ax.set_aspect("equal")
    ax.axis("off")

    # Save logic
    fig.canvas.draw()
    fig.savefig(output_path, dpi=200)
    plt.close(fig)


def render_orthophoto_pdf_from_png(png_path, pdf_path):
    if not os.path.exists(png_path) or os.path.getsize(png_path) < 2000:
        raise RuntimeError("Generated PNG is invalid or missing.")

    c = canvas.Canvas(pdf_path, pagesize=A4)
    w, h = A4
    img = ImageReader(png_path)
    c.drawImage(img, 0, 0, w, h, preserveAspectRatio=True)
    c.showPage()
    c.save()