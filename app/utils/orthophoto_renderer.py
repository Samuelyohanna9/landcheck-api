import matplotlib
matplotlib.use("Agg")

import math
import numpy as np
import geopandas as gpd
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from sqlalchemy import text
from shapely import wkb
from datetime import datetime
import contextily as ctx

from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader


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
        s = scale_text.replace(" ", "")
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


def draw_sheet_frame(fig):
    fig.add_artist(patches.Rectangle((0.02, 0.02), 0.96, 0.96,
                                     transform=fig.transFigure, fill=False, lw=2))
    fig.add_artist(patches.Rectangle((0.03, 0.03), 0.94, 0.94,
                                     transform=fig.transFigure, fill=False, lw=0.8))


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
    xmin, xmax = ax.get_xlim()
    ymin, ymax = ax.get_ylim()
    x = xmax - (xmax-xmin)*0.06
    y = ymax - (ymax-ymin)*0.10
    ax.annotate("N", xy=(x,y), xytext=(x, y-(ymax-ymin)*0.08),
                arrowprops=dict(facecolor="black", width=2, headwidth=8),
                ha="center", fontsize=12, weight="bold")


def choose_scalebar_length(scale_ratio):
    if scale_ratio <= 500: return 50
    if scale_ratio <= 1000: return 100
    if scale_ratio <= 2000: return 200
    return 500


def add_scalebar(ax, length_m, segments=4):
    xmin, xmax = ax.get_xlim()
    ymin, ymax = ax.get_ylim()

    bar_h = (ymax-ymin)*0.01
    x0 = xmin + (xmax-xmin)/2 - length_m/2
    y0 = ymin - (ymax-ymin)*0.08

    seg = length_m/segments

    for i in range(segments):
        face = "black" if i%2==0 else "white"
        ax.add_patch(patches.Rectangle((x0+i*seg, y0), seg, bar_h,
                    facecolor=face, edgecolor="black", lw=0.8, clip_on=False))

    ax.add_patch(patches.Rectangle((x0,y0),length_m,bar_h,fill=False,
                                   edgecolor="black",lw=1.2,clip_on=False))

    ax.text(x0, y0-bar_h*1.5, "0", ha="center", va="top", fontsize=7)
    for i in range(1,segments+1):
        ax.text(x0+i*seg, y0-bar_h*1.5, f"{int(i*seg)}", ha="center", va="top", fontsize=7)

    ax.text(x0+length_m/2, y0+bar_h*2.5, "meters", ha="center", fontsize=7)


def draw_grid(ax, minor, major):
    xmin,xmax=ax.get_xlim()
    ymin,ymax=ax.get_ylim()
    for step,lw,a in [(minor,0.3,0.2),(major,1.0,0.6)]:
        xs=np.arange(math.floor(xmin/step)*step, xmax+step, step)
        ys=np.arange(math.floor(ymin/step)*step, ymax+step, step)
        for x in xs: ax.plot([x,x],[ymin,ymax],color="blue",lw=lw,alpha=a)
        for y in ys: ax.plot([xmin,xmax],[y,y],color="blue",lw=lw,alpha=a)


def draw_coordinate_frame(ax, spacing):
    xmin,xmax=ax.get_xlim()
    ymin,ymax=ax.get_ylim()
    pad=(xmax-xmin)*0.035

    ax.add_patch(patches.Rectangle((xmin-pad,ymin-pad),(xmax-xmin)+2*pad,(ymax-ymin)+2*pad,fill=False,lw=1.5,clip_on=False))
    ax.add_patch(patches.Rectangle((xmin,ymin),(xmax-xmin),(ymax-ymin),fill=False,lw=1.0))

    xs=np.arange(math.floor(xmin/spacing)*spacing,xmax+0.1,spacing)
    ys=np.arange(math.floor(ymin/spacing)*spacing,ymax+0.1,spacing)

    for x in xs:
        ax.text(x,ymax+pad*0.45,f"{int(x)}",ha="center",fontsize=7,color="blue")

    for y in ys:
        if y<ymin+spacing*0.9: continue
        ax.text(xmin-pad*0.45,y,f"{int(y)}",va="center",ha="right",fontsize=7,color="blue",rotation=90)
        ax.text(xmax+pad*0.45,y,f"{int(y)}",va="center",ha="left",fontsize=7,color="blue",rotation=90)


# =======================
# Main Renderer
# =======================

def render_orthophoto_png(
    db, plot_id, output_path,
    title_text="ORTHOPHOTO",
    location_text="",
    lga_text="",
    state_text="",
    station_text="",
    scale_text="1 : 1000",
    crs_footer_text="ORIGIN: WGS84 (Displayed in meters)",
    source_footer_text="SOURCE: LandCheck System",
    surveyor_name="",
    surveyor_rank="",
    tile_source="esri"
):

    plot_wkb=db.execute(text("SELECT geom FROM plots WHERE id=:id"),{"id":plot_id}).scalar()
    buffer_wkb=db.execute(text("SELECT geom FROM plot_buffers WHERE plot_id=:id"),{"id":plot_id}).scalar()

    gdf_plot=gpd.GeoDataFrame(geometry=[wkb.loads(plot_wkb)],crs="EPSG:4326").to_crs(3857)
    gdf_buf=gpd.GeoDataFrame(geometry=[wkb.loads(buffer_wkb)],crs="EPSG:4326").to_crs(3857)

    poly=gdf_plot.geometry.iloc[0]
    buf=gdf_buf.geometry.iloc[0]

    fig=plt.figure(figsize=(8.27,11.69),dpi=200)
    map_left,map_bottom,map_width,map_height=0.10,0.24,0.80,0.52
    ax=fig.add_axes([map_left,map_bottom,map_width,map_height])

    draw_sheet_frame(fig)
    draw_title_block(fig,title_text,plot_id,scale_text,location_text,lga_text,state_text,station_text)
    draw_footer(fig,crs_footer_text,source_footer_text,surveyor_name,surveyor_rank)

    scale_ratio=parse_scale_ratio(scale_text)
    fig_w,fig_h=fig.get_size_inches()
    apply_true_scale(ax,buf,scale_ratio,fig_w*map_width,fig_h*map_height)

    source=ctx.providers.Esri.WorldImagery if tile_source=="esri" else ctx.providers.OpenStreetMap.Mapnik
    ctx.add_basemap(ax,source=source,crs="EPSG:3857",attribution=False)

    span=max(ax.get_xlim()[1]-ax.get_xlim()[0],ax.get_ylim()[1]-ax.get_ylim()[0])
    major=nice_grid_step(span)

    draw_grid(ax,major/5,major)
    draw_coordinate_frame(ax,major)

    gdf_plot.plot(ax=ax,facecolor="none",edgecolor="red",lw=2)

    add_north_arrow(ax)
    add_scalebar(ax,choose_scalebar_length(scale_ratio))

    ax.set_aspect("equal")
    ax.axis("off")

    plt.savefig(output_path,dpi=200,bbox_inches="tight")
    plt.close(fig)


def render_orthophoto_pdf_from_png(png_path, pdf_path):
    c=canvas.Canvas(pdf_path,pagesize=landscape(A4))
    w,h=landscape(A4)
    m=20
    c.drawImage(ImageReader(png_path),m,m,w-2*m,h-2*m,preserveAspectRatio=True)
    c.save()
