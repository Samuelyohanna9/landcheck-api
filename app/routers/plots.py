# app/routers/plots.py

from fastapi import APIRouter, Depends, Body, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from geoalchemy2.shape import from_shape
from shapely.geometry import Polygon
from sqlalchemy import text
from fastapi.responses import FileResponse
from app.schemas.plot_create import PlotCreateRequest
from typing import Optional, Union, List

import os
import tempfile
import glob
import re

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

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
REPORTS_DIR = os.path.join(BASE_DIR, "reports")

# Coordinate system EPSG codes mapping
COORDINATE_SYSTEMS = {
    "wgs84": 4326,
    "utm_31n": 32631,
    "utm_32n": 32632,
    "utm_33n": 32633,
    "minna_31": 26331,
    "minna_32": 26332,
    "minna_33": 26333,
}

COORDINATE_SYSTEM_NAMES = {
    "wgs84": "WGS84 (Lat/Lon)",
    "utm_31n": "UTM Zone 31N",
    "utm_32n": "UTM Zone 32N",
    "utm_33n": "UTM Zone 33N",
    "minna_31": "Minna Datum Zone 31",
    "minna_32": "Minna Datum Zone 32",
    "minna_33": "Minna Datum Zone 33",
}


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_plot_meta_table(db: Session):
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS plot_meta (
            plot_id INTEGER PRIMARY KEY REFERENCES plots(id) ON DELETE CASCADE,
            title_text VARCHAR(255),
            location_text TEXT,
            lga_text TEXT,
            state_text TEXT,
            surveyor_name TEXT,
            surveyor_rank TEXT,
            scale_text VARCHAR(50),
            paper_size VARCHAR(10),
            coordinate_system VARCHAR(20),
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """))
    # Ensure columns exist if the table was created previously with missing fields
    columns_to_add = [
        ("title_text", "VARCHAR(255)"),
        ("location_text", "TEXT"),
        ("lga_text", "TEXT"),
        ("state_text", "TEXT"),
        ("surveyor_name", "TEXT"),
        ("surveyor_rank", "TEXT"),
        ("scale_text", "VARCHAR(50)"),
        ("paper_size", "VARCHAR(10)"),
        ("coordinate_system", "VARCHAR(20)"),
        ("created_at", "TIMESTAMP DEFAULT NOW()"),
        ("updated_at", "TIMESTAMP DEFAULT NOW()"),
    ]
    for col_name, col_type in columns_to_add:
        try:
            db.execute(text(f"ALTER TABLE plot_meta ADD COLUMN IF NOT EXISTS {col_name} {col_type}"))
        except Exception:
            # Ignore for databases that don't support IF NOT EXISTS or already have column
            pass
    db.commit()


def ensure_plots_created_at(db: Session):
    try:
        db.execute(text("ALTER TABLE plots ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()"))
        db.commit()
    except Exception:
        db.rollback()


def upsert_plot_meta(
    db: Session,
    plot_id: int,
    title_text: Optional[str] = None,
    location_text: Optional[str] = None,
    lga_text: Optional[str] = None,
    state_text: Optional[str] = None,
    surveyor_name: Optional[str] = None,
    surveyor_rank: Optional[str] = None,
    scale_text: Optional[str] = None,
    paper_size: Optional[str] = None,
    coordinate_system: Optional[str] = None,
):
    ensure_plot_meta_table(db)

    db.execute(text("""
        INSERT INTO plot_meta (
            plot_id, title_text, location_text, lga_text, state_text,
            surveyor_name, surveyor_rank, scale_text, paper_size, coordinate_system
        )
        VALUES (
            :plot_id, :title_text, :location_text, :lga_text, :state_text,
            :surveyor_name, :surveyor_rank, :scale_text, :paper_size, :coordinate_system
        )
        ON CONFLICT (plot_id) DO UPDATE SET
            title_text = COALESCE(NULLIF(EXCLUDED.title_text, ''), plot_meta.title_text),
            location_text = COALESCE(NULLIF(EXCLUDED.location_text, ''), plot_meta.location_text),
            lga_text = COALESCE(NULLIF(EXCLUDED.lga_text, ''), plot_meta.lga_text),
            state_text = COALESCE(NULLIF(EXCLUDED.state_text, ''), plot_meta.state_text),
            surveyor_name = COALESCE(NULLIF(EXCLUDED.surveyor_name, ''), plot_meta.surveyor_name),
            surveyor_rank = COALESCE(NULLIF(EXCLUDED.surveyor_rank, ''), plot_meta.surveyor_rank),
            scale_text = COALESCE(NULLIF(EXCLUDED.scale_text, ''), plot_meta.scale_text),
            paper_size = COALESCE(NULLIF(EXCLUDED.paper_size, ''), plot_meta.paper_size),
            coordinate_system = COALESCE(NULLIF(EXCLUDED.coordinate_system, ''), plot_meta.coordinate_system),
            updated_at = NOW()
    """), {
        "plot_id": plot_id,
        "title_text": title_text,
        "location_text": location_text,
        "lga_text": lga_text,
        "state_text": state_text,
        "surveyor_name": surveyor_name,
        "surveyor_rank": surveyor_rank,
        "scale_text": scale_text,
        "paper_size": paper_size,
        "coordinate_system": coordinate_system,
    })
    db.commit()


def safe_remove(path: str):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except Exception:
        pass


def resolve_existing_path(candidates: list[str]) -> str | None:
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def cleanup_preview_files(plot_id: int):
    patterns = [
        os.path.join(REPORTS_DIR, "orthophoto", f"plot_{plot_id}_orthophoto_*_preview*.png"),
        os.path.join(REPORTS_DIR, "orthophoto", f"plot_{plot_id}_orthophoto_preview*.png"),
        os.path.join(REPORTS_DIR, "previews", f"plot_{plot_id}_preview*.png"),
        f"app/reports/orthophoto/plot_{plot_id}_orthophoto_*_preview*.png",
        f"app/reports/orthophoto/plot_{plot_id}_orthophoto_preview*.png",
        f"app/reports/previews/plot_{plot_id}_preview*.png",
    ]
    for pattern in patterns:
        for path in glob.glob(pattern):
            safe_remove(path)


# ---------------- CREATE PLOT ----------------

@router.post("")
def create_plot(payload: Union[PlotCreateRequest, List[List[float]]], db: Session = Depends(get_db)):

    coords = payload.coordinates if isinstance(payload, PlotCreateRequest) else payload
    meta = payload.meta if isinstance(payload, PlotCreateRequest) else None

    if len(coords) < 3:
        raise HTTPException(status_code=400, detail="Polygon requires at least 3 points")

    ensure_plots_created_at(db)

    polygon = Polygon(coords)
    geom = from_shape(polygon, srid=4326)

    plot = Plot(geom=geom)
    db.add(plot)
    db.commit()
    db.refresh(plot)

    # Ensure meta table exists and create a base row for this plot
    ensure_plot_meta_table(db)
    db.execute(text("""
        INSERT INTO plot_meta (plot_id)
        VALUES (:plot_id)
        ON CONFLICT (plot_id) DO NOTHING
    """), {"plot_id": plot.id})
    db.commit()

    if meta:
        upsert_plot_meta(
            db=db,
            plot_id=plot.id,
            title_text=meta.title or None,
            location_text=meta.location or None,
            lga_text=meta.lga or None,
            state_text=meta.state or None,
            surveyor_name=meta.surveyor or None,
            surveyor_rank=meta.rank or None,
            scale_text=meta.scale or None,
        )

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
def download_plot_report_pdf(plot_id: int, db: Session = Depends(get_db), background_tasks: BackgroundTasks = None,
    title_text: str = Body("SURVEY PLAN"),
    location_text: str = Body(""),
    lga_text: str = Body(""),
    state_text: str = Body(""),
    scale_text: str = Body("1 : 1000"),
    surveyor_name: str = Body(""),
    surveyor_rank: str = Body(""),
    station_names: list[str] = Body(default=[]),
    coordinate_system: str = Body("wgs84"),
    paper_size: str = Body("A4"),
    north_arrow_style: str = Body("classic"),
    north_arrow_color: str = Body("black"),
    beacon_style: str = Body("circle")):

    reports_dir = REPORTS_DIR
    maps_dir = os.path.join(REPORTS_DIR, "maps")
    os.makedirs(reports_dir, exist_ok=True)
    os.makedirs(maps_dir, exist_ok=True)

    pdf_path = f"{reports_dir}/plot_{plot_id}_report.pdf"

    tmp_map = tempfile.NamedTemporaryFile(suffix="_map.png", delete=False)
    map_path = tmp_map.name
    tmp_map.close()

    # Save/refresh plot metadata
    upsert_plot_meta(
        db=db,
        plot_id=plot_id,
        title_text=title_text,
        location_text=location_text,
        lga_text=lga_text,
        state_text=state_text,
        surveyor_name=surveyor_name,
        surveyor_rank=surveyor_rank,
        scale_text=scale_text,
        paper_size=paper_size,
        coordinate_system=coordinate_system,
    )

    # Get EPSG code for selected coordinate system
    epsg_code = COORDINATE_SYSTEMS.get(coordinate_system, 4326)
    crs_name = COORDINATE_SYSTEM_NAMES.get(coordinate_system, "WGS84")

    render_plot_map_layout(
        db=db,
        plot_id=plot_id,
        output_path=map_path,
        title_text=title_text,
        location_text=location_text,
        lga_text=lga_text,
        state_text=state_text,
        scale_text=scale_text,
        surveyor_name=surveyor_name,
        surveyor_rank=surveyor_rank,
        station_names=station_names if station_names else None,
        coordinate_system=coordinate_system,
        epsg_code=epsg_code,
        crs_footer_text=f"COORDINATE SYSTEM: {crs_name}",
        paper_size=paper_size,
        north_arrow_style=north_arrow_style,
        north_arrow_color=north_arrow_color,
        beacon_style=beacon_style,
    )

    report = get_plot_report(plot_id, db)
    generate_plot_report_pdf(report, pdf_path, map_path, paper_size=paper_size)

    safe_remove(map_path)

    return FileResponse(pdf_path, filename=f"plot_{plot_id}_report.pdf")


# ---------------- SIMPLE PDF DOWNLOAD (GET) ----------------

@router.get("/{plot_id}/download/pdf")
def simple_download_pdf(plot_id: int, db: Session = Depends(get_db), background_tasks: BackgroundTasks = None):
    """Simple GET endpoint for basic PDF download from dashboard"""
    reports_dir = REPORTS_DIR
    maps_dir = os.path.join(REPORTS_DIR, "maps")
    os.makedirs(reports_dir, exist_ok=True)
    os.makedirs(maps_dir, exist_ok=True)

    pdf_path = f"{reports_dir}/plot_{plot_id}_report.pdf"

    tmp_map = tempfile.NamedTemporaryFile(suffix="_map.png", delete=False)
    map_path = tmp_map.name
    tmp_map.close()

    # Use default values
    render_plot_map_layout(
        db=db,
        plot_id=plot_id,
        output_path=map_path,
        title_text="SURVEY PLAN",
        location_text="",
        lga_text="",
        state_text="",
        scale_text="1 : 1000",
        surveyor_name="",
        surveyor_rank="",
        station_names=None,
        coordinate_system="wgs84",
        epsg_code=4326,
        crs_footer_text="COORDINATE SYSTEM: WGS84"
    )

    report = get_plot_report(plot_id, db)
    generate_plot_report_pdf(report, pdf_path, map_path)

    safe_remove(map_path)

    return FileResponse(pdf_path, filename=f"plot_{plot_id}_report.pdf")


# ---------------- SURVEY PLAN PREVIEW ----------------

@router.post("/{plot_id}/report/preview")
def preview_plot_map(plot_id: int, db: Session = Depends(get_db), background_tasks: BackgroundTasks = None,
    title_text: str = Body("SURVEY PLAN"),
    location_text: str = Body(""),
    lga_text: str = Body(""),
    state_text: str = Body(""),
    scale_text: str = Body("1 : 1000"),
    surveyor_name: str = Body(""),
    surveyor_rank: str = Body(""),
    station_names: list[str] = Body(default=[]),
    coordinate_system: str = Body("wgs84"),
    paper_size: str = Body("A4"),
    north_arrow_style: str = Body("classic"),
    north_arrow_color: str = Body("black"),
    beacon_style: str = Body("circle")):

    cleanup_preview_files(plot_id)
    tmp_map = tempfile.NamedTemporaryFile(suffix="_preview.png", delete=False)
    map_path = tmp_map.name
    tmp_map.close()

    # Save/refresh plot metadata
    upsert_plot_meta(
        db=db,
        plot_id=plot_id,
        title_text=title_text,
        location_text=location_text,
        lga_text=lga_text,
        state_text=state_text,
        surveyor_name=surveyor_name,
        surveyor_rank=surveyor_rank,
        scale_text=scale_text,
        paper_size=paper_size,
        coordinate_system=coordinate_system,
    )

    # Get EPSG code for selected coordinate system
    epsg_code = COORDINATE_SYSTEMS.get(coordinate_system, 4326)
    crs_name = COORDINATE_SYSTEM_NAMES.get(coordinate_system, "WGS84")

    render_plot_map_layout(
        db=db,
        plot_id=plot_id,
        output_path=map_path,
        title_text=title_text,
        location_text=location_text,
        lga_text=lga_text,
        state_text=state_text,
        scale_text=scale_text,
        surveyor_name=surveyor_name,
        surveyor_rank=surveyor_rank,
        station_names=station_names if station_names else None,
        coordinate_system=coordinate_system,
        epsg_code=epsg_code,
        crs_footer_text=f"COORDINATE SYSTEM: {crs_name}",
        paper_size=paper_size,
        north_arrow_style=north_arrow_style,
        north_arrow_color=north_arrow_color,
        beacon_style=beacon_style,
    )

    if background_tasks:
        background_tasks.add_task(safe_remove, map_path)

    return FileResponse(map_path, media_type="image/png", background=background_tasks)


# ---------------- BACK COMPUTATION ----------------

@router.post("/{plot_id}/back-computation/pdf")
def download_back_computation_pdf(plot_id: int, db: Session = Depends(get_db), background_tasks: BackgroundTasks = None,
    coordinate_system: str = Body("wgs84"),
    station_names: list[str] = Body(default=[])):

    reports_dir = REPORTS_DIR
    os.makedirs(reports_dir, exist_ok=True)

    pdf_path = f"{reports_dir}/plot_{plot_id}_back_computation.pdf"

    # Get plot geometry
    plot_wkb = db.execute(text("SELECT geom FROM plots WHERE id=:id"), {"id": plot_id}).scalar()
    plot_geom = wkb.loads(plot_wkb)

    # Get accurate area using geography (meters squared)
    area_m2 = db.execute(
        text("SELECT ST_Area(geom::geography) FROM plots WHERE id=:id"),
        {"id": plot_id}
    ).scalar() or 0

    # Convert to user's selected coordinate system
    gdf = gpd.GeoDataFrame(geometry=[plot_geom], crs="EPSG:4326")

    # Get EPSG code for selected coordinate system
    epsg_code = COORDINATE_SYSTEMS.get(coordinate_system, 4326)
    crs_name = COORDINATE_SYSTEM_NAMES.get(coordinate_system, "WGS84")

    # If WGS84 is selected, use UTM for calculations (need projected CRS for distances)
    if coordinate_system == "wgs84":
        centroid = plot_geom.centroid
        utm_zone = int((centroid.x + 180) / 6) + 1
        hemisphere = "north" if centroid.y >= 0 else "south"
        epsg_code = 32600 + utm_zone if hemisphere == "north" else 32700 + utm_zone
        crs_name = f"UTM Zone {utm_zone}{'N' if hemisphere == 'north' else 'S'}"

    gdf_projected = gdf.to_crs(epsg=epsg_code)
    poly = gdf_projected.geometry.iloc[0]

    # Use custom station names if provided
    labels = station_names if station_names else None

    rows, sum_de, sum_dn = compute_back_computation(poly, labels)

    render_back_computation_pdf(rows, sum_de, sum_dn, area_m2, plot_id, pdf_path, crs_name)

    return FileResponse(pdf_path, filename=f"plot_{plot_id}_back_computation.pdf")


# ---------------- ORTHOPHOTO ----------------

@router.post("/{plot_id}/orthophoto/preview")
def orthophoto_preview(plot_id: int, db: Session = Depends(get_db), background_tasks: BackgroundTasks = None,
    scale_text: str = Body("1 : 1000"),
    station_names: list[str] = Body(default=[]),
    coordinate_system: str = Body("wgs84"),
    paper_size: str = Body("A4"),
    use_topo_map: bool = Body(False),
    north_arrow_style: str = Body("classic"),
    north_arrow_color: str = Body("black")):

    cleanup_preview_files(plot_id)
    tmp_png = tempfile.NamedTemporaryFile(suffix="_orthophoto_preview.png", delete=False)
    png_path = tmp_png.name
    tmp_png.close()

    # Save/refresh plot metadata (scale, paper size, coord system)
    upsert_plot_meta(
        db=db,
        plot_id=plot_id,
        scale_text=scale_text,
        paper_size=paper_size,
        coordinate_system=coordinate_system,
    )

    # Get EPSG code for selected coordinate system
    epsg_code = COORDINATE_SYSTEMS.get(coordinate_system, 4326)
    crs_name = COORDINATE_SYSTEM_NAMES.get(coordinate_system, "WGS84")

    render_orthophoto_png(
        db=db,
        plot_id=plot_id,
        output_path=png_path,
        title_text="TOPO MAP" if use_topo_map else "ORTHOPHOTO",
        scale_text=scale_text,
        station_names=station_names if station_names else None,
        coordinate_system=coordinate_system,
        epsg_code=epsg_code,
        crs_footer_text=f"COORDINATE SYSTEM: {crs_name}",
        source_footer_text="SOURCE: OpenTopoMap" if use_topo_map else "SOURCE: Satellite Imagery",
        use_topo_map=use_topo_map,
        paper_size=paper_size,
        north_arrow_style=north_arrow_style,
        north_arrow_color=north_arrow_color,
    )

    if background_tasks:
        background_tasks.add_task(safe_remove, png_path)

    return FileResponse(
        png_path,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
        background=background_tasks,
    )


@router.post("/{plot_id}/orthophoto/pdf")
def orthophoto_pdf(plot_id: int, db: Session = Depends(get_db), background_tasks: BackgroundTasks = None,
    title_text: str = Body("ORTHOPHOTO"),
    location_text: str = Body(""),
    lga_text: str = Body(""),
    state_text: str = Body(""),
    scale_text: str = Body("1 : 1000"),
    surveyor_name: str = Body(""),
    surveyor_rank: str = Body(""),
    station_names: list[str] = Body(default=[]),
    coordinate_system: str = Body("wgs84"),
    paper_size: str = Body("A4"),
    use_topo_map: bool = Body(False),
    north_arrow_style: str = Body("classic"),
    north_arrow_color: str = Body("black")):

    out_dir = os.path.join(REPORTS_DIR, "orthophoto")
    os.makedirs(out_dir, exist_ok=True)

    # Use different filename for topo vs satellite (response name only)
    map_type = "topo" if use_topo_map else "satellite"
    pdf_path = f"{out_dir}/plot_{plot_id}_orthophoto_{map_type}.pdf"

    tmp_png = tempfile.NamedTemporaryFile(suffix=f"_{map_type}.png", delete=False)
    png_path = tmp_png.name
    tmp_png.close()

    # Save/refresh plot metadata (scale, paper size, coord system)
    upsert_plot_meta(
        db=db,
        plot_id=plot_id,
        title_text=title_text,
        location_text=location_text,
        lga_text=lga_text,
        state_text=state_text,
        surveyor_name=surveyor_name,
        surveyor_rank=surveyor_rank,
        scale_text=scale_text,
        paper_size=paper_size,
        coordinate_system=coordinate_system,
    )

    # Get EPSG code for selected coordinate system
    epsg_code = COORDINATE_SYSTEMS.get(coordinate_system, 4326)
    crs_name = COORDINATE_SYSTEM_NAMES.get(coordinate_system, "WGS84")

    render_orthophoto_png(
        db=db,
        plot_id=plot_id,
        output_path=png_path,
        title_text=title_text,
        location_text=location_text,
        lga_text=lga_text,
        state_text=state_text,
        scale_text=scale_text,
        surveyor_name=surveyor_name,
        surveyor_rank=surveyor_rank,
        station_names=station_names if station_names else None,
        coordinate_system=coordinate_system,
        epsg_code=epsg_code,
        crs_footer_text=f"COORDINATE SYSTEM: {crs_name}",
        use_topo_map=use_topo_map,
        paper_size=paper_size,
        north_arrow_style=north_arrow_style,
        north_arrow_color=north_arrow_color,
    )

    render_orthophoto_pdf_from_png(png_path, pdf_path, paper_size=paper_size)

    filename = f"plot_{plot_id}_{'topomap' if use_topo_map else 'orthophoto'}.pdf"
    safe_remove(png_path)

    return FileResponse(pdf_path, media_type="application/pdf", filename=filename)
@router.get("/{plot_id}/survey-plan/dwg")
def download_survey_plan_dwg(plot_id: int, db: Session = Depends(get_db)):

    out_dir = os.path.join(REPORTS_DIR, "dwg")
    os.makedirs(out_dir, exist_ok=True)

    dxf_path = f"{out_dir}/plot_{plot_id}_survey_plan.dxf"

    export_survey_plan_to_dxf(db, plot_id, dxf_path)

    return FileResponse(
        dxf_path,
        media_type="application/dxf",
        filename=f"plot_{plot_id}_survey_plan.dxf"
    )


@router.get("/{plot_id}/reports/survey-plan")
def get_saved_survey_plan_pdf(plot_id: int):
    pdf_path = resolve_existing_path([
        os.path.join(REPORTS_DIR, f"plot_{plot_id}_report.pdf"),
        f"app/reports/plot_{plot_id}_report.pdf",
    ])
    if not pdf_path:
        raise HTTPException(status_code=404, detail="Survey plan PDF not found")
    return FileResponse(pdf_path, media_type="application/pdf", filename=f"plot_{plot_id}_survey_plan.pdf")


@router.get("/{plot_id}/reports/orthophoto")
def get_saved_orthophoto_pdf(plot_id: int, map_type: str = "satellite"):
    safe_type = "topo" if str(map_type).lower() in ["topo", "topomap", "topo_map"] else "satellite"
    pdf_path = resolve_existing_path([
        os.path.join(REPORTS_DIR, "orthophoto", f"plot_{plot_id}_orthophoto_{safe_type}.pdf"),
        f"app/reports/orthophoto/plot_{plot_id}_orthophoto_{safe_type}.pdf",
    ])
    if not pdf_path:
        raise HTTPException(status_code=404, detail="Orthophoto PDF not found")
    filename = f"plot_{plot_id}_{'topomap' if safe_type == 'topo' else 'orthophoto'}.pdf"
    return FileResponse(pdf_path, media_type="application/pdf", filename=filename)


@router.get("/{plot_id}/reports/back-computation")
def get_saved_back_computation_pdf(plot_id: int):
    pdf_path = resolve_existing_path([
        os.path.join(REPORTS_DIR, f"plot_{plot_id}_back_computation.pdf"),
        f"app/reports/plot_{plot_id}_back_computation.pdf",
    ])
    if not pdf_path:
        raise HTTPException(status_code=404, detail="Back computation PDF not found")
    return FileResponse(pdf_path, media_type="application/pdf", filename=f"plot_{plot_id}_back_computation.pdf")
