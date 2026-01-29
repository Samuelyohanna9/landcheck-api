# app/routers/plots.py

from fastapi import APIRouter, Depends, Body
from sqlalchemy.orm import Session
from geoalchemy2.shape import from_shape
from shapely.geometry import Polygon
from sqlalchemy import text
from fastapi.responses import FileResponse
import os

from app.db import SessionLocal
from app.models.plot import Plot
from app.models.plot_buffer import PlotBuffer
from app.utils.pdf import generate_plot_report_pdf
from app.utils.map_renderer_layout import render_plot_map_layout
from app.utils.back_computation import compute_back_computation
from app.utils.back_computation_pdf import render_back_computation_pdf
from shapely import wkb
import geopandas as gpd
from app.utils.dwg_exporter import export_survey_plan_to_dxf


from app.utils.orthophoto_renderer import (
    render_orthophoto_png,
    render_orthophoto_pdf_from_png
)

router = APIRouter(prefix="/plots", tags=["plots"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------- CREATE PLOT ----------------

@router.post("")
def create_plot(coords: list[list[float]], db: Session = Depends(get_db)):

    polygon = Polygon(coords)
    geom = from_shape(polygon, srid=4326)

    plot = Plot(geom=geom)
    db.add(plot)
    db.commit()
    db.refresh(plot)

    # ---------------- BUFFER ----------------

    db.execute(text("""
        INSERT INTO plot_buffers (plot_id, geom)
        SELECT :plot_id,
               ST_Buffer(geom::geography, 50)::geometry
        FROM plots
        WHERE id = :plot_id
    """), {"plot_id": plot.id})

    # ---------------- BUILDINGS ----------------

    db.execute(text("""
        INSERT INTO detected_features (plot_id, feature_type, location, geom)
        SELECT :plot_id, 'building', 'inside', m.geom
        FROM multipolygons m
        JOIN plots p ON p.id = :plot_id
        WHERE m.building IS NOT NULL
          AND ST_Intersects(m.geom, p.geom)
    """), {"plot_id": plot.id})

    db.execute(text("""
        INSERT INTO detected_features (plot_id, feature_type, location, geom)
        SELECT :plot_id, 'building', 'buffer', m.geom
        FROM multipolygons m
        JOIN plot_buffers b ON b.plot_id = :plot_id
        JOIN plots p ON p.id = :plot_id
        WHERE m.building IS NOT NULL
          AND ST_Intersects(m.geom, b.geom)
          AND NOT ST_Intersects(m.geom, p.geom)
    """), {"plot_id": plot.id})

    # ---------------- ROADS ----------------

    db.execute(text("""
        INSERT INTO detected_features (plot_id, feature_type, location, geom)
        SELECT :plot_id, 'road', 'inside', r.geom
        FROM (
            SELECT geom FROM lines WHERE highway IS NOT NULL
            UNION ALL
            SELECT geom FROM multilinestrings
            WHERE type = 'highway' OR other_tags LIKE '%highway%'
        ) r
        JOIN plots p ON p.id = :plot_id
        WHERE ST_Intersects(r.geom, p.geom)
    """), {"plot_id": plot.id})

    db.execute(text("""
        INSERT INTO detected_features (plot_id, feature_type, location, geom)
        SELECT :plot_id, 'road', 'buffer', r.geom
        FROM (
            SELECT geom FROM lines WHERE highway IS NOT NULL
            UNION ALL
            SELECT geom FROM multilinestrings
            WHERE type = 'highway' OR other_tags LIKE '%highway%'
        ) r
        JOIN plot_buffers b ON b.plot_id = :plot_id
        JOIN plots p ON p.id = :plot_id
        WHERE ST_Intersects(r.geom, b.geom)
          AND NOT ST_Intersects(r.geom, p.geom)
    """), {"plot_id": plot.id})

    # ---------------- RIVERS ----------------

    db.execute(text("""
        INSERT INTO detected_features (plot_id, feature_type, location, geom)
        SELECT :plot_id, 'river', 'inside', r.geom
        FROM (
            SELECT geom FROM lines WHERE waterway IS NOT NULL
            UNION ALL
            SELECT geom FROM multilinestrings
            WHERE type = 'waterway' OR other_tags LIKE '%waterway%'
        ) r
        JOIN plots p ON p.id = :plot_id
        WHERE ST_Intersects(r.geom, p.geom)
    """), {"plot_id": plot.id})

    db.execute(text("""
        INSERT INTO detected_features (plot_id, feature_type, location, geom)
        SELECT :plot_id, 'river', 'buffer', r.geom
        FROM (
            SELECT geom FROM lines WHERE waterway IS NOT NULL
            UNION ALL
            SELECT geom FROM multilinestrings
            WHERE type = 'waterway' OR other_tags LIKE '%waterway%'
        ) r
        JOIN plot_buffers b ON b.plot_id = :plot_id
        JOIN plots p ON p.id = :plot_id
        WHERE ST_Intersects(r.geom, b.geom)
          AND NOT ST_Intersects(r.geom, p.geom)
    """), {"plot_id": plot.id})

    db.commit()

    return {"plot_id": plot.id}


# ---------------- FEATURES SUMMARY ----------------

@router.get("/{plot_id}/features")
def get_plot_features(plot_id: int, db: Session = Depends(get_db)):

    sql = text("""
        SELECT feature_type, location, COUNT(*) as count
        FROM detected_features
        WHERE plot_id = :plot_id
        GROUP BY feature_type, location
    """)

    rows = db.execute(sql, {"plot_id": plot_id}).fetchall()

    response = {"plot_id": plot_id, "inside": {}, "buffer": {}}

    for r in rows:
        response[r.location][r.feature_type] = r.count

    return response


# ---------------- REPORT DATA ----------------

@router.get("/{plot_id}/report")
def get_plot_report(plot_id: int, db: Session = Depends(get_db)):

    area = db.execute(
        text("SELECT ST_Area(geom::geography) FROM plots WHERE id=:id"),
        {"id": plot_id}
    ).scalar()

    rows = db.execute(text("""
        SELECT feature_type, location, COUNT(*) as count
        FROM detected_features
        WHERE plot_id = :plot_id
        GROUP BY feature_type, location
    """), {"plot_id": plot_id}).fetchall()

    inside = {}
    buffer = {}

    for r in rows:
        (inside if r.location == "inside" else buffer)[r.feature_type] = r.count

    return {
        "plot_id": plot_id,
        "area_m2": round(area, 2) if area else None,
        "features": {"inside": inside, "buffer": buffer}
    }


# ---------------- SURVEY PLAN PDF ----------------

@router.post("/{plot_id}/report/pdf")
def download_plot_report_pdf(plot_id: int, db: Session = Depends(get_db),
    title_text: str = Body("SURVEY PLAN"),
    station_text: str = Body(""),
    location_text: str = Body(""),
    lga_text: str = Body(""),
    state_text: str = Body(""),
    scale_text: str = Body("1 : 1000"),
    surveyor_name: str = Body(""),
    surveyor_rank: str = Body(""),
    station_names: list[str] = Body(default=[])):

    reports_dir = "app/reports"
    maps_dir = "app/reports/maps"
    os.makedirs(reports_dir, exist_ok=True)
    os.makedirs(maps_dir, exist_ok=True)

    pdf_path = f"{reports_dir}/plot_{plot_id}_report.pdf"
    map_path = f"{maps_dir}/plot_{plot_id}_map.png"

    render_plot_map_layout(db, plot_id, map_path, title_text, location_text,
                            lga_text, state_text, station_text, scale_text,
                            surveyor_name, surveyor_rank, station_names)

    report = get_plot_report(plot_id, db)
    generate_plot_report_pdf(report, pdf_path, map_path)

    return FileResponse(pdf_path, filename=f"plot_{plot_id}_report.pdf")


# ---------------- SURVEY PLAN PREVIEW ----------------

@router.post("/{plot_id}/report/preview")
def preview_plot_map(plot_id: int, db: Session = Depends(get_db),
    title_text: str = Body("SURVEY PLAN"),
    station_text: str = Body(""),
    location_text: str = Body(""),
    lga_text: str = Body(""),
    state_text: str = Body(""),
    scale_text: str = Body("1 : 1000"),
    surveyor_name: str = Body(""),
    surveyor_rank: str = Body(""),
    station_names: list[str] = Body(default=[])):

    maps_dir = "app/reports/previews"
    os.makedirs(maps_dir, exist_ok=True)

    map_path = f"{maps_dir}/plot_{plot_id}_preview.png"

    render_plot_map_layout(db, plot_id, map_path, title_text, location_text,
                            lga_text, state_text, station_text, scale_text,
                            surveyor_name, surveyor_rank, station_names)

    return FileResponse(map_path, media_type="image/png")


# ---------------- BACK COMPUTATION ----------------

@router.get("/{plot_id}/back-computation/pdf")
def download_back_computation_pdf(plot_id: int, db: Session = Depends(get_db)):

    reports_dir = "app/reports"
    os.makedirs(reports_dir, exist_ok=True)

    pdf_path = f"{reports_dir}/plot_{plot_id}_back_computation.pdf"

    plot_wkb = db.execute(text("SELECT geom FROM plots WHERE id=:id"), {"id": plot_id}).scalar()
    plot_geom = wkb.loads(plot_wkb)

    gdf = gpd.GeoDataFrame(geometry=[plot_geom], crs="EPSG:4326").to_crs(3857)
    poly = gdf.geometry.iloc[0]

    rows, sum_de, sum_dn = compute_back_computation(poly)

    render_back_computation_pdf(rows, sum_de, sum_dn, poly.area, plot_id, pdf_path)

    return FileResponse(pdf_path, filename=f"plot_{plot_id}_back_computation.pdf")


# ---------------- ORTHOPHOTO ----------------

@router.post("/{plot_id}/orthophoto/preview")
def orthophoto_preview(plot_id: int, db: Session = Depends(get_db)):

    out_dir = "app/reports/orthophoto"
    os.makedirs(out_dir, exist_ok=True)

    png_path = f"{out_dir}/plot_{plot_id}_orthophoto_preview.png"

    render_orthophoto_png(
        db=db,
        plot_id=plot_id,
        output_path=png_path,
        scale_text="1 : 1000"
    )

    return FileResponse(png_path, media_type="image/png")


@router.get("/{plot_id}/orthophoto/pdf")   # <-- CHANGED TO GET
def orthophoto_pdf(plot_id: int, db: Session = Depends(get_db)):

    out_dir = "app/reports/orthophoto"
    os.makedirs(out_dir, exist_ok=True)

    png_path = f"{out_dir}/plot_{plot_id}_orthophoto.png"
    pdf_path = f"{out_dir}/plot_{plot_id}_orthophoto.pdf"

    render_orthophoto_png(
        db=db,
        plot_id=plot_id,
        output_path=png_path,
        scale_text="1 : 1000"
    )

    render_orthophoto_pdf_from_png(png_path, pdf_path)

    return FileResponse(pdf_path, media_type="application/pdf")
@router.get("/{plot_id}/survey-plan/dwg")
def download_survey_plan_dwg(plot_id: int, db: Session = Depends(get_db)):

    out_dir = "app/reports/dwg"
    os.makedirs(out_dir, exist_ok=True)

    dxf_path = f"{out_dir}/plot_{plot_id}_survey_plan.dxf"

    export_survey_plan_to_dxf(db, plot_id, dxf_path)

    return FileResponse(
        dxf_path,
        media_type="application/dxf",
        filename=f"plot_{plot_id}_survey_plan.dxf"
    )
