import math
import os
from sqlalchemy import text
from shapely import wkb
from shapely.geometry import Point, Polygon
import geopandas as gpd
import ezdxf
from app.utils.coordinate_converter import COORDINATE_SYSTEMS


# ==========================
# Helpers
# ==========================

def bearing_deg(p1, p2):
    dx = p2.x - p1.x
    dy = p2.y - p1.y
    return (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0


def _clockwise_ring_coords(poly):
    coords = list(poly.exterior.coords)
    if len(coords) < 2:
        return coords

    vertex_count = max(0, len(coords) - 1)
    try:
        is_ccw = bool(poly.exterior.is_ccw)
    except Exception:
        is_ccw = False

    if not is_ccw or vertex_count <= 2:
        return coords

    body = coords[:-1]
    reordered_body = [body[0], *reversed(body[1:])]
    return reordered_body + [reordered_body[0]]


def _metric_epsg_for_wgs84_polygon(poly_wgs84: Polygon) -> int:
    centroid = poly_wgs84.centroid
    zone = int((centroid.x + 180) / 6) + 1
    zone = max(1, min(zone, 60))
    return (32600 + zone) if centroid.y >= 0 else (32700 + zone)


def _resolve_export_epsg(plot_geom_wgs84, coordinate_system: str | None) -> int:
    cs_key = str(coordinate_system or "").strip().lower()
    if cs_key and cs_key != "wgs84":
        epsg = COORDINATE_SYSTEMS.get(cs_key, {}).get("epsg")
        if epsg:
            return int(epsg)
    return _metric_epsg_for_wgs84_polygon(plot_geom_wgs84)


def nice_grid_step(span):
    if span <= 0:
        return 100.0
    base = 10 ** math.floor(math.log10(span))
    for m in [0.02, 0.05, 0.1, 0.2, 0.5, 1]:
        step = base * m
        if span / step <= 10:
            return step
    return base


def add_layers(doc):
    layers = {
        "PLOT": 1,
        "BUILDINGS": 7,
        "ROADS": 8,
        "RIVERS": 5,
        "GRID": 4,
        "COORDS": 4,
        "TEXT": 2,
    }
    for name, color in layers.items():
        if name not in doc.layers:
            doc.layers.new(name, dxfattribs={"color": color})


def add_text(msp, txt, x, y, h, layer="TEXT", rot=0):
    msp.add_text(
        txt,
        dxfattribs={
            "height": float(h),
            "layer": layer,
            "rotation": float(rot),
        },
    ).set_placement((float(x), float(y)), align=ezdxf.enums.TextEntityAlignment.CENTER)


def resolve_dxf_version() -> str:
    """Return a DXF release string supported by ezdxf.

    We default to R2000 (AC1015) for broad compatibility with older AutoCAD versions.
    Override with env var PLOT_DXF_VERSION (e.g., R2007, R2010, R2013, R2018).
    """
    requested = str(os.getenv("PLOT_DXF_VERSION", "R2000") or "R2000").strip().upper()
    allowed = {"R12", "R2000", "R2004", "R2007", "R2010", "R2013", "R2018"}
    return requested if requested in allowed else "R2000"


# ==========================
# Grid + Coordinates
# ==========================

def draw_grid_and_coords(msp, bounds, spacing):
    minx, miny, maxx, maxy = bounds

    gxmin = math.floor(minx / spacing) * spacing
    gxmax = math.ceil(maxx / spacing) * spacing
    gymin = math.floor(miny / spacing) * spacing
    gymax = math.ceil(maxy / spacing) * spacing

    text_h = 2.5  # meters (fixed, AutoCAD readable)

    x = gxmin
    while x <= gxmax:
        msp.add_line((x, gymin), (x, gymax), dxfattribs={"layer": "GRID"})
        add_text(msp, f"{int(round(x))}", x, gymax + spacing * 0.25, text_h, layer="COORDS")
        x += spacing

    y = gymin
    while y <= gymax:
        msp.add_line((gxmin, y), (gxmax, y), dxfattribs={"layer": "GRID"})
        add_text(msp, f"{int(round(y))}", gxmin - spacing * 0.25, y, text_h, layer="COORDS", rot=90)
        add_text(msp, f"{int(round(y))}", gxmax + spacing * 0.25, y, text_h, layer="COORDS", rot=90)
        y += spacing


# ==========================
# Main Exporter
# ==========================

def export_survey_plan_to_dxf(
    db,
    plot_id: int,
    output_path: str,
    coordinate_system: str | None = None,
    measurement_polygon: Polygon | None = None,
    export_epsg_override: int | None = None,
):
    """
    `measurement_polygon`/`export_epsg_override` (both required together) let the caller pass the
    same already-projected polygon the PDF/back-computation paths use (built directly from the
    surveyor's original input coordinates via `_resolve_measurement_polygon_context`), instead of
    this function re-deriving the boundary from `plots.geom`. `plots.geom` is itself only ever the
    *stored* WGS84 conversion of those original coordinates, so reprojecting it again here would
    round-trip through an extra transform (original CRS -> WGS84 -> export CRS) that the PDF path
    skips (original CRS -> export CRS directly) - a second lossy transform is exactly what was
    producing bearings/distances/area in the DXF that were a hair off from both the PDF and from
    AutoCAD (which a surveyor would drive from their original field coordinates, not a WGS84
    round-trip of them).
    """

    plot_wkb = db.execute(
        text("SELECT geom FROM plots WHERE id=:id"),
        {"id": plot_id}
    ).scalar()

    if plot_wkb is None:
        raise ValueError("Plot not found")

    rows = db.execute(text("""
        SELECT geom, feature_type FROM detected_features
        WHERE plot_id=:id
    """), {"id": plot_id}).fetchall()

    plot_geom = wkb.loads(plot_wkb)

    buildings, roads, rivers = [], [], []

    for r in rows:
        if r.geom is None:
            continue
        g = wkb.loads(r.geom)
        if r.feature_type == "building":
            buildings.append(g)
        elif r.feature_type == "road":
            roads.append(g)
        elif r.feature_type == "river":
            rivers.append(g)

    if measurement_polygon is not None and export_epsg_override is not None:
        export_epsg = int(export_epsg_override)
        poly = measurement_polygon
    else:
        export_epsg = _resolve_export_epsg(plot_geom, coordinate_system)
        gdf_plot = gpd.GeoDataFrame(geometry=[plot_geom], crs="EPSG:4326").to_crs(epsg=export_epsg)
        poly = gdf_plot.geometry.iloc[0]

    gdf_buildings = gpd.GeoDataFrame(geometry=buildings, crs="EPSG:4326").to_crs(epsg=export_epsg) if buildings else None
    gdf_roads = gpd.GeoDataFrame(geometry=roads, crs="EPSG:4326").to_crs(epsg=export_epsg) if roads else None
    gdf_rivers = gpd.GeoDataFrame(geometry=rivers, crs="EPSG:4326").to_crs(epsg=export_epsg) if rivers else None

    # =====================
    # Create DXF
    # =====================

    doc = ezdxf.new(resolve_dxf_version())
    doc.units = ezdxf.units.M

    add_layers(doc)
    msp = doc.modelspace()

    # =====================
    # Plot boundary
    # =====================

    coords = _clockwise_ring_coords(poly)

    for i in range(len(coords) - 1):
        p1 = coords[i]
        p2 = coords[i + 1]
        msp.add_line(p1[:2], p2[:2], dxfattribs={"layer": "PLOT"})

    # =====================
    # Bearings + distances
    # =====================

    for i in range(len(coords) - 1):
        p1 = Point(coords[i])
        p2 = Point(coords[i + 1])

        bearing = bearing_deg(p1, p2)
        dist = p1.distance(p2)

        mx = (p1.x + p2.x) / 2
        my = (p1.y + p2.y) / 2

        ang = math.degrees(math.atan2(p2.y - p1.y, p2.x - p1.x))
        if ang < -90 or ang > 90:
            ang += 180

        txt = f"{bearing:.1f}%%d  {dist:.1f}m"

        add_text(msp, txt, mx, my, 3.0, rot=ang)

    # =====================
    # Plot label
    # =====================

    c = poly.centroid
    add_text(msp, f"PLOT {plot_id}", c.x, c.y, 6.0)

    # =====================
    # Roads & Rivers
    # =====================

    def draw_lines(gdf, layer):
        if gdf is None:
            return
        for geom in gdf.geometry:
            if geom.geom_type == "LineString":
                pts = list(geom.coords)
                for i in range(len(pts) - 1):
                    msp.add_line(pts[i][:2], pts[i + 1][:2], dxfattribs={"layer": layer})
            elif geom.geom_type == "MultiLineString":
                for ln in geom.geoms:
                    pts = list(ln.coords)
                    for i in range(len(pts) - 1):
                        msp.add_line(pts[i][:2], pts[i + 1][:2], dxfattribs={"layer": layer})

    draw_lines(gdf_roads, "ROADS")
    draw_lines(gdf_rivers, "RIVERS")

    # =====================
    # Buildings (Polygon + MultiPolygon)
    # =====================

    if gdf_buildings is not None:
        for geom in gdf_buildings.geometry:

            if geom.geom_type == "Polygon":
                polys = [geom]
            elif geom.geom_type == "MultiPolygon":
                polys = list(geom.geoms)
            else:
                continue

            for poly2 in polys:
                pts = list(poly2.exterior.coords)
                for i in range(len(pts) - 1):
                    msp.add_line(
                        pts[i][:2],
                        pts[i + 1][:2],
                        dxfattribs={"layer": "BUILDINGS"}
                    )

    # =====================
    # Grid + coordinates
    # =====================

    minx, miny, maxx, maxy = poly.bounds
    span = max(maxx - minx, maxy - miny)
    major = nice_grid_step(span)

    draw_grid_and_coords(msp, (minx, miny, maxx, maxy), major)

    # =====================
    # Save
    # =====================

    doc.saveas(output_path)
