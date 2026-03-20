# app/routers/plots.py

from fastapi import APIRouter, Depends, Body, HTTPException, BackgroundTasks, Query
from sqlalchemy.orm import Session
from geoalchemy2.shape import from_shape
from shapely.geometry import Polygon, shape, Point, box
from shapely.affinity import rotate
from shapely.ops import unary_union
from sqlalchemy import text
from fastapi.responses import FileResponse
from app.schemas.plot_create import PlotCreateRequest
from typing import Optional, Union, List, Any

import os
import tempfile
import glob
import re
import shutil
import zipfile
import csv
import time
import json
import hashlib
import math
import textwrap
from threading import Lock
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as patches

from app.db import SessionLocal
from app.models.plot import Plot
from app.models.plot_buffer import PlotBuffer
from app.utils.pdf import generate_plot_report_pdf
from app.utils.map_renderer_layout import (
    render_plot_map_layout,
    get_paper_config,
    parse_scale_ratio,
    apply_true_scale,
    annotate_vertices,
    draw_building_hatch,
    draw_fences,
    build_fence_avoid_geom,
    add_north_arrow,
)
from app.utils.back_computation import compute_back_computation
from app.utils.back_computation_pdf import render_back_computation_pdf
from shapely import wkb
import geopandas as gpd
from app.utils.dwg_exporter import export_survey_plan_to_dxf
from app.utils.r2_exports import upload_export_file_best_effort


from app.utils.orthophoto_renderer import (
    render_orthophoto_png,
    render_orthophoto_pdf_from_png
)

router = APIRouter(prefix="/plots", tags=["plots"])

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
REPORTS_DIR = os.path.join(BASE_DIR, "reports")
PREVIEW_CACHE_DIR = os.path.join(REPORTS_DIR, "previews_cache")
PREVIEW_CACHE_TTL_SECONDS = max(30, int(os.getenv("PLOT_PREVIEW_CACHE_TTL_SECONDS", "180")))
PREVIEW_CACHE_MAX_FILES_PER_PLOT = max(5, int(os.getenv("PLOT_PREVIEW_CACHE_MAX_FILES_PER_PLOT", "24")))
PREVIEW_LAYOUT_VERSION = "survey_layout_2026_03_10_adamawa_v83"
CLEAN_COPY_RENDER_VERSION = "clean_copy_2026_03_20_layout_v6"

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

DEFAULT_CERTIFICATION_STATEMENT = (
    "I hereby certify that this survey plan is a true representation of the survey "
    "executed by me and conforms with the regulations of surveying profession."
)
DEFAULT_TEMPLATE_NAME = "general"
DEFAULT_ADAMAWA_AUTHORITY_TITLE = "SURVEYOR GENERAL"
DEFAULT_ADAMAWA_AUTHORITY_DATE = "November, 2024"
DEFAULT_ADAMAWA_ORIGIN_TEXT = "ORIGIN:- WGS 84 UTM ZONE 33N"
DEFAULT_ADAMAWA_TOPO_SHEET_TEXT = "BASED ON GIREI TOPO SHEET 197 NE"
DEFAULT_ADAMAWA_DISCLAIMER_TEXT = (
    "Detail shewn not the result of accurate survey. All bearing and distances shewn on this plan "
    "have been computed from registered Co-ordinates."
)

_PLOTS_SCHEMA_READY = False
_PLOTS_SCHEMA_LOCK = Lock()


def ensure_plots_schema_once(db: Session):
    global _PLOTS_SCHEMA_READY
    if _PLOTS_SCHEMA_READY:
        return
    with _PLOTS_SCHEMA_LOCK:
        if _PLOTS_SCHEMA_READY:
            return
        ensure_plot_meta_table(db)
        ensure_plot_feature_overrides_table(db)
        ensure_plot_subdivision_tables(db)
        ensure_plot_query_indexes(db)
        _PLOTS_SCHEMA_READY = True


def get_db():
    db = SessionLocal()
    try:
        ensure_plots_schema_once(db)
        yield db
    finally:
        db.close()


def _table_exists(db: Session, table_name: str) -> bool:
    try:
        return db.execute(text("SELECT to_regclass(:table_name)"), {"table_name": table_name}).scalar() is not None
    except Exception:
        return False


def _safe_run_ddl(db: Session, ddl_sql: str):
    try:
        db.execute(text(ddl_sql))
        db.commit()
    except Exception:
        db.rollback()


def ensure_plot_query_indexes(db: Session):
    index_plan = [
        ("public.plots", "CREATE INDEX IF NOT EXISTS idx_plots_geom ON plots USING GIST (geom)"),
        ("public.plot_buffers", "CREATE INDEX IF NOT EXISTS idx_plot_buffers_plot_id ON plot_buffers(plot_id)"),
        ("public.plot_buffers", "CREATE INDEX IF NOT EXISTS idx_plot_buffers_geom ON plot_buffers USING GIST (geom)"),
        ("public.detected_features", "CREATE INDEX IF NOT EXISTS idx_detected_features_plot_id ON detected_features(plot_id)"),
        ("public.detected_features", "CREATE INDEX IF NOT EXISTS idx_detected_features_plot_type ON detected_features(plot_id, feature_type)"),
        ("public.detected_features", "CREATE INDEX IF NOT EXISTS idx_detected_features_geom ON detected_features USING GIST (geom)"),
        ("public.lines", "CREATE INDEX IF NOT EXISTS idx_lines_geom ON lines USING GIST (geom)"),
        ("public.lines", "CREATE INDEX IF NOT EXISTS idx_lines_highway_not_null ON lines(highway) WHERE highway IS NOT NULL"),
        ("public.lines", "CREATE INDEX IF NOT EXISTS idx_lines_waterway_not_null ON lines(waterway) WHERE waterway IS NOT NULL"),
        ("public.multipolygons", "CREATE INDEX IF NOT EXISTS idx_multipolygons_geom ON multipolygons USING GIST (geom)"),
        ("public.multipolygons", "CREATE INDEX IF NOT EXISTS idx_multipolygons_building_not_null ON multipolygons(building) WHERE building IS NOT NULL"),
        ("public.multilinestrings", "CREATE INDEX IF NOT EXISTS idx_multilinestrings_geom ON multilinestrings USING GIST (geom)"),
        ("public.multilinestrings", "CREATE INDEX IF NOT EXISTS idx_multilinestrings_type ON multilinestrings(type)"),
    ]
    for table_name, ddl_sql in index_plan:
        if _table_exists(db, table_name):
            _safe_run_ddl(db, ddl_sql)


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
            certification_statement TEXT,
            scale_text VARCHAR(50),
            paper_size VARCHAR(10),
            coordinate_system VARCHAR(20),
            template_name VARCHAR(40) DEFAULT 'general',
            adamawa_rof_no TEXT,
            adamawa_owner_name TEXT,
            adamawa_authority_title TEXT,
            adamawa_authority_date_text TEXT,
            adamawa_control_point_name TEXT,
            adamawa_northing TEXT,
            adamawa_easting TEXT,
            adamawa_elevation TEXT,
            adamawa_origin_text TEXT,
            adamawa_topo_sheet_text TEXT,
            adamawa_computation_no TEXT,
            adamawa_cadastral_sheet_no TEXT,
            adamawa_plan_no TEXT,
            adamawa_surveyed_by_text TEXT,
            adamawa_disclaimer_text TEXT,
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
        ("certification_statement", "TEXT"),
        ("scale_text", "VARCHAR(50)"),
        ("paper_size", "VARCHAR(10)"),
        ("coordinate_system", "VARCHAR(20)"),
        ("template_name", "VARCHAR(40) DEFAULT 'general'"),
        ("adamawa_rof_no", "TEXT"),
        ("adamawa_owner_name", "TEXT"),
        ("adamawa_authority_title", "TEXT"),
        ("adamawa_authority_date_text", "TEXT"),
        ("adamawa_control_point_name", "TEXT"),
        ("adamawa_northing", "TEXT"),
        ("adamawa_easting", "TEXT"),
        ("adamawa_elevation", "TEXT"),
        ("adamawa_origin_text", "TEXT"),
        ("adamawa_topo_sheet_text", "TEXT"),
        ("adamawa_computation_no", "TEXT"),
        ("adamawa_cadastral_sheet_no", "TEXT"),
        ("adamawa_plan_no", "TEXT"),
        ("adamawa_surveyed_by_text", "TEXT"),
        ("adamawa_disclaimer_text", "TEXT"),
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


def ensure_plot_feature_overrides_table(db: Session):
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS plot_feature_overrides (
            id SERIAL PRIMARY KEY,
            plot_id INTEGER NOT NULL REFERENCES plots(id) ON DELETE CASCADE,
            feature_type TEXT NOT NULL CHECK (feature_type IN ('road', 'building', 'river', 'fence')),
            action TEXT NOT NULL CHECK (action IN ('add', 'delete', 'update')),
            name TEXT,
            width_m DOUBLE PRECISION,
            geom GEOMETRY,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """))
    try:
        db.execute(text("ALTER TABLE plot_feature_overrides ADD COLUMN IF NOT EXISTS width_m DOUBLE PRECISION"))
        # Upgrade legacy CHECK constraints that do not include fence
        check_rows = db.execute(text("""
            SELECT c.conname AS name, pg_get_constraintdef(c.oid) AS definition
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE t.relname = 'plot_feature_overrides'
              AND c.contype = 'c'
        """)).mappings().all()
        has_fence_check = False
        for row in check_rows:
            definition = (row.get("definition") or "").lower()
            if "feature_type" not in definition:
                continue
            if "fence" in definition:
                has_fence_check = True
                continue
            constraint_name = (row.get("name") or "").replace('"', '""')
            if constraint_name:
                db.execute(text(f'ALTER TABLE plot_feature_overrides DROP CONSTRAINT IF EXISTS "{constraint_name}"'))
        if not has_fence_check:
            db.execute(text("""
                ALTER TABLE plot_feature_overrides
                ADD CONSTRAINT plot_feature_overrides_feature_type_check
                CHECK (feature_type IN ('road', 'building', 'river', 'fence'))
            """))
        db.execute(text("CREATE INDEX IF NOT EXISTS idx_plot_feature_overrides_plot_id ON plot_feature_overrides(plot_id)"))
        db.execute(text("CREATE INDEX IF NOT EXISTS idx_plot_feature_overrides_geom ON plot_feature_overrides USING GIST (geom)"))
        db.commit()
    except Exception:
        db.rollback()


def ensure_plot_subdivision_tables(db: Session):
    db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS plot_subdivision_batches (
                id SERIAL PRIMARY KEY,
                parent_plot_id INTEGER NOT NULL REFERENCES plots(id) ON DELETE CASCADE,
                estate_name TEXT,
                method TEXT NOT NULL,
                requested_count INTEGER,
                target_area_m2 DOUBLE PRECISION,
                orientation_deg DOUBLE PRECISION DEFAULT 0,
                generated_count INTEGER DEFAULT 0,
                total_area_m2 DOUBLE PRECISION DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'completed',
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
            """
        )
    )
    db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS plot_subdivision_items (
                id SERIAL PRIMARY KEY,
                batch_id INTEGER NOT NULL REFERENCES plot_subdivision_batches(id) ON DELETE CASCADE,
                child_plot_id INTEGER NOT NULL REFERENCES plots(id) ON DELETE CASCADE,
                lot_no TEXT,
                area_m2 DOUBLE PRECISION,
                created_at TIMESTAMP DEFAULT NOW()
            )
            """
        )
    )
    for col_name, col_type in [
        ("parent_plot_id", "INTEGER"),
        ("subdivision_batch_id", "INTEGER"),
        ("subdivision_lot_no", "TEXT"),
        ("estate_name", "TEXT"),
    ]:
        try:
            db.execute(text(f"ALTER TABLE plot_meta ADD COLUMN IF NOT EXISTS {col_name} {col_type}"))
        except Exception:
            pass

    index_sql = [
        "CREATE INDEX IF NOT EXISTS idx_plot_sub_batches_parent ON plot_subdivision_batches(parent_plot_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_plot_sub_items_batch ON plot_subdivision_items(batch_id)",
        "CREATE INDEX IF NOT EXISTS idx_plot_sub_items_child ON plot_subdivision_items(child_plot_id)",
        "CREATE INDEX IF NOT EXISTS idx_plot_meta_parent_plot_id ON plot_meta(parent_plot_id)",
        "CREATE INDEX IF NOT EXISTS idx_plot_meta_subdivision_batch ON plot_meta(subdivision_batch_id)",
    ]
    for ddl in index_sql:
        try:
            db.execute(text(ddl))
        except Exception:
            pass
    db.commit()


def upsert_plot_meta(
    db: Session,
    plot_id: int,
    title_text: Optional[str] = None,
    location_text: Optional[str] = None,
    lga_text: Optional[str] = None,
    state_text: Optional[str] = None,
    surveyor_name: Optional[str] = None,
    surveyor_rank: Optional[str] = None,
    certification_statement: Optional[str] = None,
    scale_text: Optional[str] = None,
    paper_size: Optional[str] = None,
    coordinate_system: Optional[str] = None,
    template_name: Optional[str] = None,
    adamawa_rof_no: Optional[str] = None,
    adamawa_owner_name: Optional[str] = None,
    adamawa_authority_title: Optional[str] = None,
    adamawa_authority_date_text: Optional[str] = None,
    adamawa_control_point_name: Optional[str] = None,
    adamawa_northing: Optional[str] = None,
    adamawa_easting: Optional[str] = None,
    adamawa_elevation: Optional[str] = None,
    adamawa_origin_text: Optional[str] = None,
    adamawa_topo_sheet_text: Optional[str] = None,
    adamawa_computation_no: Optional[str] = None,
    adamawa_cadastral_sheet_no: Optional[str] = None,
    adamawa_plan_no: Optional[str] = None,
    adamawa_surveyed_by_text: Optional[str] = None,
    adamawa_disclaimer_text: Optional[str] = None,
    commit: bool = True,
):
    ensure_plot_meta_table(db)

    db.execute(text("""
        INSERT INTO plot_meta (
            plot_id, title_text, location_text, lga_text, state_text,
            surveyor_name, surveyor_rank, certification_statement, scale_text, paper_size, coordinate_system,
            template_name, adamawa_rof_no, adamawa_owner_name, adamawa_authority_title, adamawa_authority_date_text,
            adamawa_control_point_name, adamawa_northing, adamawa_easting, adamawa_elevation, adamawa_origin_text,
            adamawa_topo_sheet_text, adamawa_computation_no, adamawa_cadastral_sheet_no, adamawa_plan_no,
            adamawa_surveyed_by_text, adamawa_disclaimer_text
        )
        VALUES (
            :plot_id, :title_text, :location_text, :lga_text, :state_text,
            :surveyor_name, :surveyor_rank, :certification_statement, :scale_text, :paper_size, :coordinate_system,
            :template_name, :adamawa_rof_no, :adamawa_owner_name, :adamawa_authority_title, :adamawa_authority_date_text,
            :adamawa_control_point_name, :adamawa_northing, :adamawa_easting, :adamawa_elevation, :adamawa_origin_text,
            :adamawa_topo_sheet_text, :adamawa_computation_no, :adamawa_cadastral_sheet_no, :adamawa_plan_no,
            :adamawa_surveyed_by_text, :adamawa_disclaimer_text
        )
        ON CONFLICT (plot_id) DO UPDATE SET
            title_text = COALESCE(NULLIF(EXCLUDED.title_text, ''), plot_meta.title_text),
            location_text = COALESCE(NULLIF(EXCLUDED.location_text, ''), plot_meta.location_text),
            lga_text = COALESCE(NULLIF(EXCLUDED.lga_text, ''), plot_meta.lga_text),
            state_text = COALESCE(NULLIF(EXCLUDED.state_text, ''), plot_meta.state_text),
            surveyor_name = COALESCE(NULLIF(EXCLUDED.surveyor_name, ''), plot_meta.surveyor_name),
            surveyor_rank = COALESCE(NULLIF(EXCLUDED.surveyor_rank, ''), plot_meta.surveyor_rank),
            certification_statement = COALESCE(NULLIF(EXCLUDED.certification_statement, ''), plot_meta.certification_statement),
            scale_text = COALESCE(NULLIF(EXCLUDED.scale_text, ''), plot_meta.scale_text),
            paper_size = COALESCE(NULLIF(EXCLUDED.paper_size, ''), plot_meta.paper_size),
            coordinate_system = COALESCE(NULLIF(EXCLUDED.coordinate_system, ''), plot_meta.coordinate_system),
            template_name = COALESCE(NULLIF(EXCLUDED.template_name, ''), plot_meta.template_name),
            adamawa_rof_no = COALESCE(NULLIF(EXCLUDED.adamawa_rof_no, ''), plot_meta.adamawa_rof_no),
            adamawa_owner_name = COALESCE(NULLIF(EXCLUDED.adamawa_owner_name, ''), plot_meta.adamawa_owner_name),
            adamawa_authority_title = COALESCE(NULLIF(EXCLUDED.adamawa_authority_title, ''), plot_meta.adamawa_authority_title),
            adamawa_authority_date_text = COALESCE(NULLIF(EXCLUDED.adamawa_authority_date_text, ''), plot_meta.adamawa_authority_date_text),
            adamawa_control_point_name = COALESCE(NULLIF(EXCLUDED.adamawa_control_point_name, ''), plot_meta.adamawa_control_point_name),
            adamawa_northing = COALESCE(NULLIF(EXCLUDED.adamawa_northing, ''), plot_meta.adamawa_northing),
            adamawa_easting = COALESCE(NULLIF(EXCLUDED.adamawa_easting, ''), plot_meta.adamawa_easting),
            adamawa_elevation = COALESCE(NULLIF(EXCLUDED.adamawa_elevation, ''), plot_meta.adamawa_elevation),
            adamawa_origin_text = COALESCE(NULLIF(EXCLUDED.adamawa_origin_text, ''), plot_meta.adamawa_origin_text),
            adamawa_topo_sheet_text = COALESCE(NULLIF(EXCLUDED.adamawa_topo_sheet_text, ''), plot_meta.adamawa_topo_sheet_text),
            adamawa_computation_no = COALESCE(NULLIF(EXCLUDED.adamawa_computation_no, ''), plot_meta.adamawa_computation_no),
            adamawa_cadastral_sheet_no = COALESCE(NULLIF(EXCLUDED.adamawa_cadastral_sheet_no, ''), plot_meta.adamawa_cadastral_sheet_no),
            adamawa_plan_no = COALESCE(NULLIF(EXCLUDED.adamawa_plan_no, ''), plot_meta.adamawa_plan_no),
            adamawa_surveyed_by_text = COALESCE(NULLIF(EXCLUDED.adamawa_surveyed_by_text, ''), plot_meta.adamawa_surveyed_by_text),
            adamawa_disclaimer_text = COALESCE(NULLIF(EXCLUDED.adamawa_disclaimer_text, ''), plot_meta.adamawa_disclaimer_text),
            updated_at = NOW()
    """), {
        "plot_id": plot_id,
        "title_text": title_text,
        "location_text": location_text,
        "lga_text": lga_text,
        "state_text": state_text,
        "surveyor_name": surveyor_name,
        "surveyor_rank": surveyor_rank,
        "certification_statement": certification_statement,
        "scale_text": scale_text,
        "paper_size": paper_size,
        "coordinate_system": coordinate_system,
        "template_name": template_name,
        "adamawa_rof_no": adamawa_rof_no,
        "adamawa_owner_name": adamawa_owner_name,
        "adamawa_authority_title": adamawa_authority_title,
        "adamawa_authority_date_text": adamawa_authority_date_text,
        "adamawa_control_point_name": adamawa_control_point_name,
        "adamawa_northing": adamawa_northing,
        "adamawa_easting": adamawa_easting,
        "adamawa_elevation": adamawa_elevation,
        "adamawa_origin_text": adamawa_origin_text,
        "adamawa_topo_sheet_text": adamawa_topo_sheet_text,
        "adamawa_computation_no": adamawa_computation_no,
        "adamawa_cadastral_sheet_no": adamawa_cadastral_sheet_no,
        "adamawa_plan_no": adamawa_plan_no,
        "adamawa_surveyed_by_text": adamawa_surveyed_by_text,
        "adamawa_disclaimer_text": adamawa_disclaimer_text,
    })
    if commit:
        db.commit()


def get_plot_meta(db: Session, plot_id: int) -> dict:
    row = db.execute(text("""
        SELECT title_text, location_text, lga_text, state_text,
               surveyor_name, surveyor_rank, certification_statement, scale_text, paper_size, coordinate_system,
               template_name, adamawa_rof_no, adamawa_owner_name, adamawa_authority_title, adamawa_authority_date_text,
               adamawa_control_point_name, adamawa_northing, adamawa_easting, adamawa_elevation, adamawa_origin_text,
               adamawa_topo_sheet_text, adamawa_computation_no, adamawa_cadastral_sheet_no, adamawa_plan_no,
               adamawa_surveyed_by_text, adamawa_disclaimer_text,
               parent_plot_id, subdivision_batch_id, subdivision_lot_no, estate_name
        FROM plot_meta
        WHERE plot_id = :plot_id
    """), {"plot_id": plot_id}).mappings().first()
    if not row:
        return {
            "title_text": "SURVEY PLAN",
            "location_text": "",
            "lga_text": "",
            "state_text": "",
            "surveyor_name": "",
            "surveyor_rank": "",
            "certification_statement": DEFAULT_CERTIFICATION_STATEMENT,
            "scale_text": "1 : 1000",
            "paper_size": "A4",
            "coordinate_system": "wgs84",
            "template_name": DEFAULT_TEMPLATE_NAME,
            "adamawa_rof_no": "",
            "adamawa_owner_name": "",
            "adamawa_authority_title": DEFAULT_ADAMAWA_AUTHORITY_TITLE,
            "adamawa_authority_date_text": DEFAULT_ADAMAWA_AUTHORITY_DATE,
            "adamawa_control_point_name": "",
            "adamawa_northing": "",
            "adamawa_easting": "",
            "adamawa_elevation": "",
            "adamawa_origin_text": DEFAULT_ADAMAWA_ORIGIN_TEXT,
            "adamawa_topo_sheet_text": DEFAULT_ADAMAWA_TOPO_SHEET_TEXT,
            "adamawa_computation_no": "",
            "adamawa_cadastral_sheet_no": "",
            "adamawa_plan_no": "",
            "adamawa_surveyed_by_text": "",
            "adamawa_disclaimer_text": DEFAULT_ADAMAWA_DISCLAIMER_TEXT,
            "parent_plot_id": None,
            "subdivision_batch_id": None,
            "subdivision_lot_no": "",
            "estate_name": "",
        }
    return {
        "title_text": row.get("title_text") or "SURVEY PLAN",
        "location_text": row.get("location_text") or "",
        "lga_text": row.get("lga_text") or "",
        "state_text": row.get("state_text") or "",
        "surveyor_name": row.get("surveyor_name") or "",
        "surveyor_rank": row.get("surveyor_rank") or "",
        "certification_statement": row.get("certification_statement") or DEFAULT_CERTIFICATION_STATEMENT,
        "scale_text": row.get("scale_text") or "1 : 1000",
        "paper_size": row.get("paper_size") or "A4",
        "coordinate_system": row.get("coordinate_system") or "wgs84",
        "template_name": row.get("template_name") or DEFAULT_TEMPLATE_NAME,
        "adamawa_rof_no": row.get("adamawa_rof_no") or "",
        "adamawa_owner_name": row.get("adamawa_owner_name") or "",
        "adamawa_authority_title": row.get("adamawa_authority_title") or DEFAULT_ADAMAWA_AUTHORITY_TITLE,
        "adamawa_authority_date_text": row.get("adamawa_authority_date_text") or DEFAULT_ADAMAWA_AUTHORITY_DATE,
        "adamawa_control_point_name": row.get("adamawa_control_point_name") or "",
        "adamawa_northing": row.get("adamawa_northing") or "",
        "adamawa_easting": row.get("adamawa_easting") or "",
        "adamawa_elevation": row.get("adamawa_elevation") or "",
        "adamawa_origin_text": row.get("adamawa_origin_text") or DEFAULT_ADAMAWA_ORIGIN_TEXT,
        "adamawa_topo_sheet_text": row.get("adamawa_topo_sheet_text") or DEFAULT_ADAMAWA_TOPO_SHEET_TEXT,
        "adamawa_computation_no": row.get("adamawa_computation_no") or "",
        "adamawa_cadastral_sheet_no": row.get("adamawa_cadastral_sheet_no") or "",
        "adamawa_plan_no": row.get("adamawa_plan_no") or "",
        "adamawa_surveyed_by_text": row.get("adamawa_surveyed_by_text") or "",
        "adamawa_disclaimer_text": row.get("adamawa_disclaimer_text") or DEFAULT_ADAMAWA_DISCLAIMER_TEXT,
        "parent_plot_id": row.get("parent_plot_id"),
        "subdivision_batch_id": row.get("subdivision_batch_id"),
        "subdivision_lot_no": row.get("subdivision_lot_no") or "",
        "estate_name": row.get("estate_name") or "",
    }


def safe_remove(path: str):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except Exception:
        pass


def _pdf_response_with_r2(
    local_pdf_path: str,
    filename: str,
    *,
    category: str,
    project_id: int | None = None,
):
    upload_meta = upload_export_file_best_effort(
        local_pdf_path,
        filename,
        category=category,
        project_id=project_id,
        content_type="application/pdf",
    )
    response = FileResponse(local_pdf_path, media_type="application/pdf", filename=filename)
    if upload_meta:
        object_key = upload_meta.get("object_key")
        public_url = upload_meta.get("public_url")
        if object_key:
            response.headers["X-LandCheck-R2-Key"] = str(object_key)
        if public_url:
            response.headers["X-LandCheck-R2-Url"] = str(public_url)
    return response


def safe_rmtree(path: str):
    try:
        shutil.rmtree(path, ignore_errors=True)
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


def build_preview_revision_token(db: Session, plot_id: int) -> dict:
    feature_row = db.execute(text("""
        SELECT COALESCE(MAX(id), 0) AS max_id, COUNT(*) AS count
        FROM detected_features
        WHERE plot_id = :plot_id
    """), {"plot_id": plot_id}).mappings().first() if _table_exists(db, "public.detected_features") else {"max_id": 0, "count": 0}

    override_row = db.execute(text("""
        SELECT
            COALESCE(MAX(EXTRACT(EPOCH FROM updated_at)), 0)::BIGINT AS updated_epoch,
            COUNT(*) AS count
        FROM plot_feature_overrides
        WHERE plot_id = :plot_id
    """), {"plot_id": plot_id}).mappings().first() if _table_exists(db, "public.plot_feature_overrides") else {"updated_epoch": 0, "count": 0}

    geom_hex = db.execute(
        text("SELECT encode(ST_AsEWKB(geom), 'hex') FROM plots WHERE id = :plot_id"),
        {"plot_id": plot_id},
    ).scalar() if _table_exists(db, "public.plots") else ""

    return {
        "features_max_id": int((feature_row or {}).get("max_id") or 0),
        "features_count": int((feature_row or {}).get("count") or 0),
        "overrides_updated_epoch": int((override_row or {}).get("updated_epoch") or 0),
        "overrides_count": int((override_row or {}).get("count") or 0),
        "plot_geom_hash": hashlib.sha256((geom_hex or "").encode("utf-8")).hexdigest() if geom_hex else "",
    }


def build_preview_cache_key(plot_id: int, payload: dict, revision_token: dict) -> str:
    packed = json.dumps(
        {
            "plot_id": plot_id,
            "payload": payload,
            "revision": revision_token,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(packed.encode("utf-8")).hexdigest()


def build_plot_geom_revision_token(db: Session, plot_id: int) -> dict:
    geom_hex = db.execute(
        text("SELECT encode(ST_AsEWKB(geom), 'hex') FROM plots WHERE id = :plot_id"),
        {"plot_id": plot_id},
    ).scalar() if _table_exists(db, "public.plots") else ""
    return {
        "plot_geom_hash": hashlib.sha256((geom_hex or "").encode("utf-8")).hexdigest() if geom_hex else "",
    }


def preview_cache_path(plot_id: int, cache_key: str, variant: str = "preview") -> str:
    os.makedirs(PREVIEW_CACHE_DIR, exist_ok=True)
    safe_variant = re.sub(r"[^a-zA-Z0-9_-]", "_", str(variant or "preview"))
    return os.path.join(PREVIEW_CACHE_DIR, f"plot_{plot_id}_{safe_variant}_{cache_key}.png")


def get_cached_preview_path(plot_id: int, cache_key: str, variant: str = "preview") -> str | None:
    cache_path = preview_cache_path(plot_id, cache_key, variant=variant)
    if not os.path.exists(cache_path):
        return None
    try:
        age_s = max(0.0, time.time() - os.path.getmtime(cache_path))
        if age_s > PREVIEW_CACHE_TTL_SECONDS:
            safe_remove(cache_path)
            return None
        return cache_path
    except Exception:
        return None


def prune_preview_cache(plot_id: int, variant: str | None = None):
    os.makedirs(PREVIEW_CACHE_DIR, exist_ok=True)
    if variant:
        safe_variant = re.sub(r"[^a-zA-Z0-9_-]", "_", str(variant))
        pattern = os.path.join(PREVIEW_CACHE_DIR, f"plot_{plot_id}_{safe_variant}_*.png")
    else:
        pattern = os.path.join(PREVIEW_CACHE_DIR, f"plot_{plot_id}_*.png")
    files = []
    for path in glob.glob(pattern):
        try:
            files.append((path, os.path.getmtime(path)))
        except Exception:
            safe_remove(path)
    files.sort(key=lambda item: item[1], reverse=True)
    now = time.time()
    for idx, (path, modified_at) in enumerate(files):
        too_old = (now - modified_at) > (PREVIEW_CACHE_TTL_SECONDS * 6)
        overflow = idx >= PREVIEW_CACHE_MAX_FILES_PER_PLOT
        if too_old or overflow:
            safe_remove(path)


def _coerce_positive_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        parsed = int(value)
        if parsed <= 0:
            return None
        return parsed
    except Exception:
        return None


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return float(default)
        parsed = float(value)
        if math.isnan(parsed) or math.isinf(parsed):
            return float(default)
        return float(parsed)
    except Exception:
        return float(default)


def _station_name(index: int) -> str:
    name = ""
    num = int(index)
    while True:
        name = chr(65 + (num % 26)) + name
        num = (num // 26) - 1
        if num < 0:
            break
    return name


def _polygon_parts(geom: Any) -> list[Polygon]:
    if geom is None:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if geom.geom_type == "MultiPolygon":
        return [g for g in geom.geoms if isinstance(g, Polygon)]
    if geom.geom_type == "GeometryCollection":
        out: list[Polygon] = []
        for g in geom.geoms:
            if isinstance(g, Polygon):
                out.append(g)
            elif g.geom_type == "MultiPolygon":
                out.extend([p for p in g.geoms if isinstance(p, Polygon)])
        return out
    return []


def _largest_polygon(geom: Any) -> Polygon | None:
    parts = [p for p in _polygon_parts(geom) if p.area > 1e-9]
    if not parts:
        return None
    return max(parts, key=lambda p: p.area)


def _clean_single_polygon(geom: Any) -> Polygon | None:
    if geom is None:
        return None
    cleaned = geom.buffer(0)
    return _largest_polygon(cleaned)


def _metric_epsg_for_wgs84_polygon(poly_wgs84: Polygon) -> int:
    centroid = poly_wgs84.centroid
    zone = int((centroid.x + 180) / 6) + 1
    zone = max(1, min(zone, 60))
    return (32600 + zone) if centroid.y >= 0 else (32700 + zone)


def _split_polygon_once_by_area(poly_metric: Polygon, target_area: float) -> tuple[Polygon, Polygon]:
    poly = _clean_single_polygon(poly_metric)
    if poly is None:
        raise HTTPException(status_code=400, detail="Subdivision failed: invalid mother parcel geometry.")

    minx, miny, maxx, maxy = poly.bounds
    if maxx - minx <= 1e-8:
        raise HTTPException(status_code=400, detail="Subdivision failed: mother parcel width too small for automatic split.")

    pad_x = max(1.0, (maxx - minx) * 0.2)
    pad_y = max(1.0, (maxy - miny) * 0.2)
    lo = minx + 1e-9
    hi = maxx - 1e-9
    best_x = (lo + hi) / 2.0
    best_diff = float("inf")
    best_left: Polygon | None = None

    for _ in range(56):
        mid = (lo + hi) / 2.0
        left_mask = box(minx - pad_x, miny - pad_y, mid, maxy + pad_y)
        left_geom = _clean_single_polygon(poly.intersection(left_mask))
        left_area = float(left_geom.area if left_geom is not None else 0.0)
        diff = abs(left_area - target_area)
        if diff < best_diff and left_geom is not None:
            best_diff = diff
            best_x = mid
            best_left = left_geom
        if left_area < target_area:
            lo = mid
        else:
            hi = mid

    if best_left is None or best_left.area <= 1e-6:
        raise HTTPException(status_code=400, detail="Subdivision failed: unable to compute left split parcel.")

    right_mask = box(best_x, miny - pad_y, maxx + pad_x, maxy + pad_y)
    right_geom = _clean_single_polygon(poly.intersection(right_mask))
    if right_geom is None or right_geom.area <= 1e-6:
        # fallback using difference if masking loses a tiny seam
        right_geom = _clean_single_polygon(poly.difference(best_left))
    if right_geom is None or right_geom.area <= 1e-6:
        raise HTTPException(status_code=400, detail="Subdivision failed: unable to compute right split parcel.")

    return best_left, right_geom


def _subdivide_polygon_equal_count(poly_metric: Polygon, split_count: int, orientation_deg: float) -> list[Polygon]:
    if split_count < 2:
        raise HTTPException(status_code=400, detail="Subdivision requires at least 2 derived plots.")
    base_poly = _clean_single_polygon(poly_metric)
    if base_poly is None:
        raise HTTPException(status_code=400, detail="Mother parcel geometry is invalid.")

    rotated = _clean_single_polygon(rotate(base_poly, -orientation_deg, origin="centroid", use_radians=False))
    if rotated is None:
        raise HTTPException(status_code=400, detail="Subdivision failed: unable to rotate mother parcel.")

    pieces: list[Polygon] = []
    remaining = rotated
    for idx in range(split_count - 1):
        pending_slots = split_count - idx
        target_area = float(remaining.area / pending_slots)
        left_piece, right_piece = _split_polygon_once_by_area(remaining, target_area)
        pieces.append(left_piece)
        remaining = right_piece
    pieces.append(remaining)

    out: list[Polygon] = []
    for piece in pieces:
        restored = _clean_single_polygon(rotate(piece, orientation_deg, origin=base_poly.centroid, use_radians=False))
        if restored is None or restored.area <= 1e-8:
            raise HTTPException(status_code=400, detail="Subdivision failed: generated a degenerate parcel.")
        out.append(restored)
    return out


def _normalize_fraction_weights(values: Any) -> list[float]:
    if not isinstance(values, (list, tuple)):
        return []
    out: list[float] = []
    for raw in values:
        parsed = _coerce_float(raw, 0.0)
        if parsed > 0:
            out.append(float(parsed))
    return out


def _normalize_fraction_breaks(values: Any) -> list[float]:
    if not isinstance(values, (list, tuple)):
        return []
    raw_breaks: list[float] = []
    for raw in values:
        parsed = _coerce_float(raw, float("nan"))
        if not math.isfinite(parsed):
            continue
        # Allow percentages (0-100) and normalized ratios (0-1).
        if parsed > 1.0 and parsed <= 100.0:
            parsed = parsed / 100.0
        if 0.0 < parsed < 1.0:
            raw_breaks.append(float(parsed))
    if not raw_breaks:
        return []
    raw_breaks.sort()
    deduped: list[float] = []
    for value in raw_breaks:
        if not deduped or abs(value - deduped[-1]) > 1e-6:
            deduped.append(value)
    return deduped


def _breaks_to_weights(breaks: list[float]) -> list[float]:
    if not breaks:
        return []
    weights: list[float] = []
    prev = 0.0
    for value in breaks:
        weights.append(max(value - prev, 0.0))
        prev = value
    weights.append(max(1.0 - prev, 0.0))
    return [w for w in weights if w > 0]


def _weights_to_breaks(weights: list[float]) -> list[float]:
    if not weights or len(weights) < 2:
        return []
    total = sum(weights)
    if total <= 0:
        return []
    acc = 0.0
    out: list[float] = []
    for weight in weights[:-1]:
        acc += float(weight) / total
        out.append(max(0.0, min(1.0, acc)))
    return out


def _subdivide_polygon_weighted(poly_metric: Polygon, weights: list[float], orientation_deg: float) -> list[Polygon]:
    normalized_weights = [float(w) for w in weights if float(w) > 0]
    if len(normalized_weights) < 2:
        raise HTTPException(status_code=400, detail="Fraction subdivision requires at least 2 positive fraction values.")

    base_poly = _clean_single_polygon(poly_metric)
    if base_poly is None:
        raise HTTPException(status_code=400, detail="Mother parcel geometry is invalid.")

    rotated = _clean_single_polygon(rotate(base_poly, -orientation_deg, origin="centroid", use_radians=False))
    if rotated is None:
        raise HTTPException(status_code=400, detail="Subdivision failed: unable to rotate mother parcel.")

    pieces: list[Polygon] = []
    remaining = rotated
    remaining_weight = float(sum(normalized_weights))
    if remaining_weight <= 0:
        raise HTTPException(status_code=400, detail="Fraction subdivision weights are invalid.")

    for weight in normalized_weights[:-1]:
        weight = float(weight)
        if remaining_weight <= 1e-9:
            break
        share = max(0.0, min(1.0, weight / remaining_weight))
        target_area = max(1e-9, float(remaining.area) * share)
        left_piece, right_piece = _split_polygon_once_by_area(remaining, target_area)
        pieces.append(left_piece)
        remaining = right_piece
        remaining_weight -= weight

    pieces.append(remaining)

    out: list[Polygon] = []
    for piece in pieces:
        restored = _clean_single_polygon(rotate(piece, orientation_deg, origin=base_poly.centroid, use_radians=False))
        if restored is None or restored.area <= 1e-8:
            raise HTTPException(status_code=400, detail="Subdivision failed: generated a degenerate parcel.")
        out.append(restored)
    return out


def _compute_subdivision_payload(
    parent_plot_id: int,
    parent_geom_wgs84: Polygon,
    *,
    method: str,
    split_count: int | None,
    target_area_m2: float | None,
    orientation_deg: float,
    lot_prefix: str,
    fraction_weights: list[float] | None = None,
    fraction_breaks: list[float] | None = None,
    custom_areas_m2: list[float] | None = None,
    lot_names: list[str] | None = None,
) -> dict:
    method_key = (method or "by_count").strip().lower()
    if method_key not in {"by_count", "by_area", "by_fraction", "by_custom_area"}:
        raise HTTPException(
            status_code=400,
            detail="Invalid subdivision method. Use 'by_count', 'by_area', 'by_fraction', or 'by_custom_area'.",
        )

    metric_epsg = _metric_epsg_for_wgs84_polygon(parent_geom_wgs84)
    gdf_metric = gpd.GeoDataFrame(geometry=[parent_geom_wgs84], crs="EPSG:4326").to_crs(epsg=metric_epsg)
    parent_metric = _clean_single_polygon(gdf_metric.geometry.iloc[0])
    if parent_metric is None or parent_metric.area <= 1e-6:
        raise HTTPException(status_code=400, detail="Mother parcel area is too small for subdivision.")

    total_area_m2 = float(parent_metric.area)
    resolved_count = _coerce_positive_int(split_count)
    target_area = _coerce_float(target_area_m2, 0.0)
    effective_fraction_weights: list[float] = []
    effective_fraction_breaks: list[float] = []
    effective_custom_areas_m2: list[float] = []
    if method_key == "by_count":
        if resolved_count is None or resolved_count < 2:
            raise HTTPException(status_code=400, detail="For 'by_count', set derived plot count to 2 or more.")
        pieces_wgs84 = _subdivide_polygon_equal_count(parent_metric, int(resolved_count), orientation_deg)
    else:
        if method_key == "by_area":
            if target_area <= 0:
                raise HTTPException(status_code=400, detail="For 'by_area', provide a positive target plot size (sqm).")
            approx_count = int(round(total_area_m2 / target_area))
            resolved_count = max(2, approx_count)
            resolved_count = min(int(resolved_count or 2), 500)
            pieces_wgs84 = _subdivide_polygon_equal_count(parent_metric, resolved_count, orientation_deg)
        elif method_key == "by_fraction":
            normalized_breaks = _normalize_fraction_breaks(fraction_breaks)
            normalized_weights = _normalize_fraction_weights(fraction_weights)
            if normalized_breaks:
                effective_fraction_breaks = normalized_breaks
                effective_fraction_weights = _breaks_to_weights(normalized_breaks)
            elif len(normalized_weights) >= 2:
                effective_fraction_weights = normalized_weights
                effective_fraction_breaks = _weights_to_breaks(normalized_weights)
            else:
                raise HTTPException(
                    status_code=400,
                    detail="For 'by_fraction', provide fraction values (for example: [2,3,5]) or break positions (for example: [0.3,0.65]).",
                )
            if len(effective_fraction_weights) < 2:
                raise HTTPException(status_code=400, detail="Fraction subdivision requires at least two resulting plots.")
            resolved_count = min(len(effective_fraction_weights), 500)
            effective_fraction_weights = effective_fraction_weights[:resolved_count]
            effective_fraction_breaks = _weights_to_breaks(effective_fraction_weights)
            pieces_wgs84 = _subdivide_polygon_weighted(parent_metric, effective_fraction_weights, orientation_deg)
        else:
            normalized_custom_areas = _normalize_fraction_weights(custom_areas_m2)
            if resolved_count is not None and resolved_count >= 2 and len(normalized_custom_areas) != resolved_count:
                raise HTTPException(
                    status_code=400,
                    detail=f"For 'by_custom_area', provide exactly {resolved_count} custom lot areas.",
                )
            if len(normalized_custom_areas) < 2:
                raise HTTPException(
                    status_code=400,
                    detail="For 'by_custom_area', provide at least two positive custom lot areas (sqm).",
                )

            allocated_sum = float(sum(normalized_custom_areas))
            tolerance_m2 = 0.01
            if allocated_sum > total_area_m2 + tolerance_m2:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Custom areas exceed mother parcel area by "
                        f"{allocated_sum - total_area_m2:.2f} sqm. Reduce allocations."
                    ),
                )
            if allocated_sum < total_area_m2 - tolerance_m2:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Custom areas do not fully allocate mother parcel. Remaining "
                        f"{total_area_m2 - allocated_sum:.2f} sqm. Adjust allocations to match total area."
                    ),
                )

            resolved_count = min(len(normalized_custom_areas), 500)
            effective_custom_areas_m2 = normalized_custom_areas[:resolved_count]
            pieces_wgs84 = _subdivide_polygon_weighted(parent_metric, effective_custom_areas_m2, orientation_deg)

    resolved_count = min(int(resolved_count or 2), 500)
    gdf_out = gpd.GeoDataFrame(geometry=pieces_wgs84, crs=f"EPSG:{metric_epsg}").to_crs(epsg=4326)

    safe_prefix = re.sub(r"[^A-Za-z0-9]+", "", str(lot_prefix or "LOT").upper()) or "LOT"
    custom_lot_names = list(lot_names or [])
    plots: list[dict] = []
    derived_total = 0.0
    for idx, (poly_wgs, poly_metric) in enumerate(zip(gdf_out.geometry.tolist(), pieces_wgs84), start=1):
        area_m2 = float(max(poly_metric.area, 0.0))
        derived_total += area_m2
        ring = [[float(x), float(y)] for x, y in list(poly_wgs.exterior.coords)]
        custom_name = str(custom_lot_names[idx - 1] or "").strip() if idx - 1 < len(custom_lot_names) else ""
        plot_no = custom_name or f"{safe_prefix}-{idx:03d}"
        plots.append(
            {
                "index": idx,
                "lot_no": plot_no,
                "area_m2": round(area_m2, 2),
                "area_hectares": round(area_m2 / 10000.0, 4),
                "geometry": {"type": "Polygon", "coordinates": [ring]},
                "station_names": [_station_name(i) for i in range(max(0, len(ring) - 1))],
            }
        )

    feature_collection = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "lot_no": p["lot_no"],
                    "index": p["index"],
                    "area_m2": p["area_m2"],
                    "area_hectares": p["area_hectares"],
                },
                "geometry": p["geometry"],
            }
            for p in plots
        ],
    }

    return {
        "parent_plot_id": int(parent_plot_id),
        "method": method_key,
        "metric_epsg": int(metric_epsg),
        "orientation_deg": round(float(orientation_deg), 3),
        "requested_count": int(split_count or 0) if split_count is not None else None,
        "target_area_m2": round(float(target_area), 2) if target_area > 0 else None,
        "fraction_weights": [round(float(w), 6) for w in effective_fraction_weights] if effective_fraction_weights else None,
        "fraction_breaks": [round(float(v), 6) for v in effective_fraction_breaks] if effective_fraction_breaks else None,
        "custom_areas_m2": [round(float(v), 4) for v in effective_custom_areas_m2] if effective_custom_areas_m2 else None,
        "resolved_count": len(plots),
        "total_area_m2": round(total_area_m2, 2),
        "derived_total_area_m2": round(derived_total, 2),
        "area_imbalance_m2": round(total_area_m2 - derived_total, 4),
        "plots": plots,
        "preview_geojson": feature_collection,
    }


def _ensure_plot_meta_row(db: Session, plot_id: int):
    db.execute(
        text(
            """
            INSERT INTO plot_meta (plot_id)
            VALUES (:plot_id)
            ON CONFLICT (plot_id) DO NOTHING
            """
        ),
        {"plot_id": int(plot_id)},
    )


def _set_plot_subdivision_meta(
    db: Session,
    *,
    child_plot_id: int,
    parent_plot_id: int,
    batch_id: int,
    lot_no: str,
    estate_name: str | None,
):
    _ensure_plot_meta_row(db, child_plot_id)
    db.execute(
        text(
            """
            UPDATE plot_meta
            SET parent_plot_id = :parent_plot_id,
                subdivision_batch_id = :batch_id,
                subdivision_lot_no = :lot_no,
                estate_name = :estate_name,
                updated_at = NOW()
            WHERE plot_id = :plot_id
            """
        ),
        {
            "plot_id": int(child_plot_id),
            "parent_plot_id": int(parent_plot_id),
            "batch_id": int(batch_id),
            "lot_no": str(lot_no or ""),
            "estate_name": str(estate_name or ""),
        },
    )


def _apply_child_plot_meta(
    db: Session,
    *,
    plot_id: int,
    title_text: str,
    parent_meta: dict,
    adamawa_owner_name: str | None = None,
):
    _ensure_plot_meta_row(db, plot_id)
    db.execute(
        text(
            """
            UPDATE plot_meta
            SET title_text = :title_text,
                location_text = :location_text,
                lga_text = :lga_text,
                state_text = :state_text,
                surveyor_name = :surveyor_name,
                surveyor_rank = :surveyor_rank,
                certification_statement = :certification_statement,
                scale_text = :scale_text,
                paper_size = :paper_size,
                coordinate_system = :coordinate_system,
                template_name = :template_name,
                adamawa_rof_no = :adamawa_rof_no,
                adamawa_owner_name = :adamawa_owner_name,
                adamawa_authority_title = :adamawa_authority_title,
                adamawa_authority_date_text = :adamawa_authority_date_text,
                adamawa_control_point_name = :adamawa_control_point_name,
                adamawa_northing = :adamawa_northing,
                adamawa_easting = :adamawa_easting,
                adamawa_elevation = :adamawa_elevation,
                adamawa_origin_text = :adamawa_origin_text,
                adamawa_topo_sheet_text = :adamawa_topo_sheet_text,
                adamawa_computation_no = :adamawa_computation_no,
                adamawa_cadastral_sheet_no = :adamawa_cadastral_sheet_no,
                adamawa_plan_no = :adamawa_plan_no,
                adamawa_surveyed_by_text = :adamawa_surveyed_by_text,
                adamawa_disclaimer_text = :adamawa_disclaimer_text,
                updated_at = NOW()
            WHERE plot_id = :plot_id
            """
        ),
        {
            "plot_id": int(plot_id),
            "title_text": title_text,
            "location_text": parent_meta.get("location_text") or None,
            "lga_text": parent_meta.get("lga_text") or None,
            "state_text": parent_meta.get("state_text") or None,
            "surveyor_name": parent_meta.get("surveyor_name") or None,
            "surveyor_rank": parent_meta.get("surveyor_rank") or None,
            "certification_statement": parent_meta.get("certification_statement") or DEFAULT_CERTIFICATION_STATEMENT,
            "scale_text": parent_meta.get("scale_text") or "1 : 1000",
            "paper_size": parent_meta.get("paper_size") or "A4",
            "coordinate_system": parent_meta.get("coordinate_system") or "wgs84",
            "template_name": parent_meta.get("template_name") or DEFAULT_TEMPLATE_NAME,
            "adamawa_rof_no": parent_meta.get("adamawa_rof_no") or "",
            "adamawa_owner_name": (
                str(adamawa_owner_name or "").strip()
                or parent_meta.get("adamawa_owner_name")
                or ""
            ),
            "adamawa_authority_title": parent_meta.get("adamawa_authority_title") or DEFAULT_ADAMAWA_AUTHORITY_TITLE,
            "adamawa_authority_date_text": parent_meta.get("adamawa_authority_date_text") or DEFAULT_ADAMAWA_AUTHORITY_DATE,
            "adamawa_control_point_name": "",
            "adamawa_northing": "",
            "adamawa_easting": "",
            "adamawa_elevation": "",
            "adamawa_origin_text": parent_meta.get("adamawa_origin_text") or DEFAULT_ADAMAWA_ORIGIN_TEXT,
            "adamawa_topo_sheet_text": parent_meta.get("adamawa_topo_sheet_text") or DEFAULT_ADAMAWA_TOPO_SHEET_TEXT,
            "adamawa_computation_no": parent_meta.get("adamawa_computation_no") or "",
            "adamawa_cadastral_sheet_no": parent_meta.get("adamawa_cadastral_sheet_no") or "",
            "adamawa_plan_no": parent_meta.get("adamawa_plan_no") or "",
            "adamawa_surveyed_by_text": parent_meta.get("adamawa_surveyed_by_text") or "",
            "adamawa_disclaimer_text": parent_meta.get("adamawa_disclaimer_text") or DEFAULT_ADAMAWA_DISCLAIMER_TEXT,
        },
    )


def _run_plot_feature_detection(db: Session, plot_id: int):
    db.execute(
        text(
            """
            INSERT INTO plot_buffers (plot_id, geom)
            SELECT :plot_id,
                   ST_Buffer(geom::geography, 50)::geometry
            FROM plots
            WHERE id = :plot_id
            ON CONFLICT DO NOTHING
            """
        ),
        {"plot_id": int(plot_id)},
    )

    db.execute(
        text(
            """
            DELETE FROM detected_features
            WHERE plot_id = :plot_id
            """
        ),
        {"plot_id": int(plot_id)},
    )

    # Buildings
    db.execute(
        text(
            """
            INSERT INTO detected_features (plot_id, feature_type, location, geom)
            SELECT :plot_id, 'building', 'inside', m.geom
            FROM multipolygons m
            JOIN plots p ON p.id = :plot_id
            WHERE m.building IS NOT NULL
              AND ST_Intersects(m.geom, p.geom)
            """
        ),
        {"plot_id": int(plot_id)},
    )
    db.execute(
        text(
            """
            INSERT INTO detected_features (plot_id, feature_type, location, geom)
            SELECT :plot_id, 'building', 'buffer', m.geom
            FROM multipolygons m
            JOIN plot_buffers b ON b.plot_id = :plot_id
            JOIN plots p ON p.id = :plot_id
            WHERE m.building IS NOT NULL
              AND ST_Intersects(m.geom, b.geom)
              AND NOT ST_Intersects(m.geom, p.geom)
            """
        ),
        {"plot_id": int(plot_id)},
    )

    # Roads
    db.execute(
        text(
            """
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
            """
        ),
        {"plot_id": int(plot_id)},
    )
    db.execute(
        text(
            """
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
            """
        ),
        {"plot_id": int(plot_id)},
    )

    # Rivers
    db.execute(
        text(
            """
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
            """
        ),
        {"plot_id": int(plot_id)},
    )
    db.execute(
        text(
            """
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
            """
        ),
        {"plot_id": int(plot_id)},
    )


def _load_plot_polygon_wgs84(db: Session, plot_id: int) -> Polygon:
    plot_wkb = db.execute(text("SELECT geom FROM plots WHERE id = :id"), {"id": int(plot_id)}).scalar()
    if plot_wkb is None:
        raise HTTPException(status_code=404, detail="Mother parcel not found.")
    try:
        geom = wkb.loads(plot_wkb)
    except Exception:
        raise HTTPException(status_code=400, detail="Mother parcel geometry is invalid.")
    poly = _clean_single_polygon(geom)
    if poly is None:
        raise HTTPException(status_code=400, detail="Mother parcel geometry is invalid.")
    return poly


def _safe_filename_fragment(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "")).strip("._-")
    return cleaned or fallback


def _format_nigerian_number(value: float, decimals: int) -> str:
    try:
        numeric = float(value)
    except Exception:
        numeric = 0.0
    # Prefix apostrophe keeps exact display in Excel while preserving dot-decimal style.
    return f"'{numeric:,.{decimals}f}"


def _normalize_lot_key(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def _extract_clean_copy_area_overrides(raw_items: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(raw_items, (list, tuple)):
        return out

    for raw in raw_items:
        if not isinstance(raw, dict):
            continue

        label = str(raw.get("label") or "").strip()
        if not label:
            continue

        lot_no_raw = raw.get("lot_no")
        child_plot_id_raw = raw.get("child_plot_id")
        lot_key = _normalize_lot_key(lot_no_raw)

        if lot_key:
            out[f"lot:{lot_key}"] = label
            continue

        try:
            child_plot_id = int(child_plot_id_raw)
        except Exception:
            child_plot_id = 0
        if child_plot_id > 0:
            out[f"child:{child_plot_id}"] = label

    return out


def _resolve_clean_copy_area_label(
    lot_no: str,
    child_plot_id: int,
    area_m2: float,
    overrides: dict[str, str] | None,
) -> str:
    override_map = overrides or {}
    lot_key = _normalize_lot_key(lot_no)
    if lot_key:
        label = override_map.get(f"lot:{lot_key}")
        if label:
            return label
    label = override_map.get(f"child:{int(child_plot_id)}")
    if label:
        return label
    return f"{(float(area_m2) / 10000.0):.4f} Hectares"


def _iter_line_geometries_for_clean_copy(geom: Any):
    if geom is None or getattr(geom, "is_empty", False):
        return
    gtype = getattr(geom, "geom_type", "")
    if gtype in ("LineString", "LinearRing"):
        try:
            yield geom
        except Exception:
            return
        return
    if gtype == "Polygon":
        try:
            yield geom.exterior
            for ring in geom.interiors:
                yield ring
        except Exception:
            pass
        return
    if hasattr(geom, "geoms"):
        for part in geom.geoms:
            yield from _iter_line_geometries_for_clean_copy(part)


def _render_subdivision_clean_copy_pdf(
    db: Session,
    parent_plot_id: int,
    parent_poly_wgs84: Polygon,
    child_rows: list[dict],
    output_pdf_path: str,
    *,
    title_text: str,
    paper_size: str,
    scale_text: str,
    coordinate_system: str,
    epsg_code: int,
    station_names: list[str] | None,
    north_arrow_style: str,
    north_arrow_color: str,
    beacon_style: str,
    road_width_m: float | None,
    area_overrides: dict[str, str] | None = None,
):
    paper_name = str(paper_size or "A4").upper()
    if paper_name not in {"A4", "A3", "A2", "A1", "A0"}:
        paper_name = "A4"

    display_epsg = int(epsg_code or 4326)
    if str(coordinate_system or "").strip().lower() == "wgs84" or display_epsg == 4326:
        display_epsg = _metric_epsg_for_wgs84_polygon(parent_poly_wgs84)

    parent_metric = gpd.GeoDataFrame(geometry=[parent_poly_wgs84], crs="EPSG:4326").to_crs(epsg=display_epsg).geometry.iloc[0]

    child_metric_rows: list[dict] = []
    for row in child_rows:
        geom_geojson_raw = row.get("geom_geojson")
        if not geom_geojson_raw:
            continue
        try:
            geom_wgs = _clean_single_polygon(shape(json.loads(geom_geojson_raw)))
        except Exception:
            geom_wgs = None
        if geom_wgs is None or geom_wgs.is_empty:
            continue
        geom_metric = gpd.GeoDataFrame(geometry=[geom_wgs], crs="EPSG:4326").to_crs(epsg=display_epsg).geometry.iloc[0]
        child_metric_rows.append(
            {
                "child_plot_id": int(row.get("child_plot_id") or 0),
                "lot_no": str(row.get("lot_no") or "").strip() or f"LOT-{int(row.get('child_plot_id') or 0)}",
                "area_m2": float(row.get("area_m2") or 0.0),
                "geometry": geom_metric,
            }
        )

    if not child_metric_rows:
        raise HTTPException(status_code=404, detail="Subdivision batch has no valid lot geometries.")

    detected_rows = db.execute(
        text("SELECT geom, feature_type FROM detected_features WHERE plot_id=:id"),
        {"id": int(parent_plot_id)},
    ).fetchall()
    override_rows = db.execute(
        text(
            """
            SELECT feature_type, action, name, width_m, ST_AsGeoJSON(geom) AS geojson
            FROM plot_feature_overrides
            WHERE plot_id = :id
            """
        ),
        {"id": int(parent_plot_id)},
    ).fetchall()
    roads_auto_rows = db.execute(
        text(
            """
            WITH roads AS (
                SELECT
                    CASE
                        WHEN ST_SRID(r.geom) = 4326 THEN r.geom
                        WHEN ST_SRID(r.geom) = 0 THEN ST_SetSRID(r.geom, 4326)
                        ELSE ST_Transform(r.geom, 4326)
                    END AS geom
                FROM lines r
                WHERE r.highway IS NOT NULL
            )
            SELECT roads.geom
            FROM roads
            JOIN plot_buffers b ON b.plot_id = :plot_id
            WHERE ST_Intersects(roads.geom, b.geom)
            """
        ),
        {"plot_id": int(parent_plot_id)},
    ).fetchall()

    buildings_wgs: list[Any] = []
    rivers_wgs: list[Any] = []
    fences_wgs: list[Any] = []
    for row in detected_rows:
        try:
            geom = wkb.loads(row.geom)
        except Exception:
            continue
        feature_type = str(row.feature_type or "").strip().lower()
        if feature_type == "building":
            buildings_wgs.append(geom)
        elif feature_type == "river":
            rivers_wgs.append(geom)
        elif feature_type == "fence":
            fences_wgs.append(geom)

    overrides: list[dict[str, Any]] = []
    for row in override_rows:
        geom = None
        if row.geojson:
            try:
                geom = shape(json.loads(row.geojson))
            except Exception:
                geom = None
        overrides.append(
            {
                "feature_type": str(row.feature_type or "").strip().lower(),
                "action": str(row.action or "").strip().lower(),
                "name": row.name,
                "width_m": row.width_m,
                "geom": geom,
            }
        )

    def apply_overrides(base_list: list[Any], feature_type: str):
        result = list(base_list)
        added: list[Any] = []
        delete_geoms: list[Any] = []
        for ov in overrides:
            if ov.get("feature_type") != feature_type:
                continue
            geom = ov.get("geom")
            if geom is None:
                continue
            try:
                if hasattr(geom, "is_valid") and not geom.is_valid:
                    geom = geom.buffer(0)
            except Exception:
                pass
            if ov.get("action") in ("delete", "update"):
                result = [g for g in result if not g.intersects(geom)]
                delete_geoms.append(geom)
            if ov.get("action") in ("add", "update"):
                result.append(geom)
                added.append(geom)
        if delete_geoms:
            added = [g for g in added if not any(g.intersects(dg) for dg in delete_geoms)]
        return result, added, delete_geoms

    buildings_wgs, added_buildings_wgs, _ = apply_overrides(buildings_wgs, "building")
    rivers_wgs, _, _ = apply_overrides(rivers_wgs, "river")
    fences_wgs, added_fences_wgs, _ = apply_overrides(fences_wgs, "fence")

    roads_wgs: list[Any] = []
    for row in roads_auto_rows:
        try:
            roads_wgs.append(wkb.loads(row.geom))
        except Exception:
            continue
    _, added_roads_wgs, road_delete_geoms = apply_overrides([], "road")
    roads_wgs = [g for g in roads_wgs if not any(g.intersects(dg) for dg in road_delete_geoms)] + list(added_roads_wgs)

    paper_config = get_paper_config(paper_name)
    font_scale = float(paper_config.get("scale", 1.0))
    dpi = 220 if paper_name in {"A4", "A3"} else (170 if paper_name == "A2" else 130)
    scale_ratio = parse_scale_ratio(scale_text)
    parent_area_m2 = float(parent_metric.area or 0.0)
    old_font_family = matplotlib.rcParams.get("font.family")
    fig = None
    try:
        matplotlib.rcParams["font.family"] = "DejaVu Serif"
        fig = plt.figure(figsize=(paper_config["width"], paper_config["height"]), dpi=dpi)
        fig.patch.set_facecolor("white")

        # Clean-copy page frame (outer + inner) to match the template feel.
        fig.add_artist(
            patches.Rectangle(
                (0.02, 0.02),
                0.96,
                0.96,
                transform=fig.transFigure,
                fill=False,
                lw=1.0,
                edgecolor="black",
                zorder=300,
                clip_on=False,
            )
        )
        fig.add_artist(
            patches.Rectangle(
                (0.03, 0.03),
                0.94,
                0.94,
                transform=fig.transFigure,
                fill=False,
                lw=0.85,
                edgecolor="black",
                zorder=300,
                clip_on=False,
            )
        )

        raw_title = str(title_text or "").strip()
        wrap_width = 54 if paper_name in {"A0", "A1", "A2"} else 48
        title_lines: list[str] = []
        if raw_title:
            for segment in [s.strip() for s in str(raw_title).upper().splitlines() if s.strip()]:
                seg_clean = re.sub(r"\s+", " ", segment)
                title_lines.extend(textwrap.wrap(seg_clean, width=wrap_width, break_long_words=False))
        if not title_lines:
            title_lines = ["SURVEY PLAN CLEAN COPY PLAN"]
        title_lines = title_lines[:5]

        y_title = 0.955
        line_gap = 0.03 if paper_name in {"A0", "A1", "A2"} else 0.028
        title_size = max(9, int(10.6 * font_scale))
        for idx, line in enumerate(title_lines):
            fig.text(
                0.5,
                y_title - idx * line_gap,
                line,
                ha="center",
                va="top",
                fontsize=title_size,
                weight="bold",
                color="black",
                fontfamily="DejaVu Serif",
            )

        if parent_area_m2 > 0:
            area_y = y_title - len(title_lines) * line_gap - 0.006
            fig.text(
                0.5,
                area_y,
                f"AREA={parent_area_m2 / 10000.0:.2f}Ha",
                ha="center",
                va="top",
                fontsize=max(8, int(9.4 * font_scale)),
                weight="bold",
                color="black",
                fontfamily="DejaVu Serif",
            )

        map_left, map_bottom, map_right = 0.055, 0.075, 0.945
        header_clearance = 0.085 + (len(title_lines) * line_gap)
        map_top = max(0.66, 0.96 - header_clearance)
        map_width = map_right - map_left
        map_height = map_top - map_bottom
        fig.add_artist(
            patches.Rectangle(
                (map_left, map_bottom),
                map_width,
                map_height,
                transform=fig.transFigure,
                fill=False,
                lw=0.9,
                edgecolor="black",
                zorder=250,
                clip_on=False,
            )
        )
        ax = fig.add_axes([map_left + 0.004, map_bottom + 0.004, map_width - 0.008, map_height - 0.008])
        ax.set_aspect("equal", adjustable="box")
        ax.set_anchor("C")
        ax.set_facecolor("white")

        # Fit map to parent parcel bounds; lock limits so later plots cannot stretch it.
        minx, miny, maxx, maxy = parent_metric.bounds
        span_x = max(maxx - minx, 1.0)
        span_y = max(maxy - miny, 1.0)
        pad_x = max(1.0, span_x * 0.1)
        pad_y = max(1.0, span_y * 0.1)
        target_xlim = (minx - pad_x, maxx + pad_x)
        target_ylim = (miny - pad_y, maxy + pad_y)
        ax.set_xlim(*target_xlim)
        ax.set_ylim(*target_ylim)
        ax.set_autoscale_on(False)
        extent_poly = box(target_xlim[0], target_ylim[0], target_xlim[1], target_ylim[1])
        clip_buffer = max(1.0, min(span_x, span_y) * 0.05)

        if rivers_wgs:
            gpd.GeoDataFrame(geometry=rivers_wgs, crs="EPSG:4326").to_crs(epsg=display_epsg).plot(
                ax=ax, color="#1d4ed8", lw=max(0.8, 1.0 * font_scale), zorder=5
            )

        road_lw = max(0.8, ((float(road_width_m) if road_width_m else 10.0) / 12.0) * font_scale)
        for road_geom in roads_wgs:
            try:
                projected = gpd.GeoSeries([road_geom], crs="EPSG:4326").to_crs(epsg=display_epsg).iloc[0]
            except Exception:
                continue
            clipped = projected.intersection(extent_poly.buffer(clip_buffer))
            if clipped.is_empty:
                continue
            for line_part in _iter_line_geometries_for_clean_copy(clipped):
                try:
                    x_vals, y_vals = line_part.xy
                    ax.plot(
                        x_vals,
                        y_vals,
                        color="black",
                        lw=road_lw,
                        linestyle=(0, (7, 4)),
                        zorder=6,
                    )
                except Exception:
                    continue

        all_buildings = list(buildings_wgs) + list(added_buildings_wgs or [])
        if all_buildings:
            draw_building_hatch(
                ax,
                all_buildings,
                display_epsg,
                scale_ratio=scale_ratio,
                font_scale=font_scale,
            )
            try:
                gpd.GeoDataFrame(geometry=all_buildings, crs="EPSG:4326").to_crs(epsg=display_epsg).plot(
                    ax=ax, facecolor="none", edgecolor="black", lw=max(0.8, 0.9 * font_scale), zorder=8
                )
            except Exception:
                pass

        all_fences = list(fences_wgs or []) + list(added_fences_wgs or [])
        if all_fences:
            draw_fences(
                ax,
                all_fences,
                display_epsg=display_epsg,
                scale_ratio=scale_ratio,
                font_scale=font_scale,
            )
        fence_avoid_geom = build_fence_avoid_geom(all_fences, display_epsg=display_epsg, scale_ratio=scale_ratio)

        for row in child_metric_rows:
            geom_metric = _clean_single_polygon(row["geometry"])
            if geom_metric is None or geom_metric.is_empty:
                continue
            try:
                x_vals, y_vals = geom_metric.exterior.xy
                ax.plot(x_vals, y_vals, color="black", linewidth=max(0.85, 0.95 * font_scale), zorder=17)
            except Exception:
                continue

        boundary_mm = 0.7 if paper_name in ["A0"] else 0.5 if paper_name in ["A1"] else 0.35
        boundary_lw_pts = boundary_mm * 72.0 / 25.4
        gpd.GeoDataFrame(geometry=[parent_metric], crs=f"EPSG:{display_epsg}").plot(
            ax=ax, facecolor="none", edgecolor="black", lw=boundary_lw_pts, zorder=20
        )
        ax.set_xlim(target_xlim)
        ax.set_ylim(target_ylim)

        min_label_mm = 12
        min_label_length_m = (min_label_mm / 1000.0) * scale_ratio
        annotate_vertices(
            ax,
            parent_metric,
            int(parent_plot_id),
            station_names=station_names if station_names else None,
            font_scale=font_scale,
            min_label_length_m=min_label_length_m,
            avoid_geom=fence_avoid_geom,
            scale_ratio=scale_ratio,
            boundary_poly=parent_metric,
            beacon_style=beacon_style,
        )

        for row in child_metric_rows:
            geom_metric = _clean_single_polygon(row["geometry"])
            if geom_metric is None or geom_metric.is_empty:
                continue
            lot_no = str(row.get("lot_no") or "").strip() or "LOT"
            child_plot_id = int(row.get("child_plot_id") or 0)
            area_m2 = float(row.get("area_m2") or 0.0)
            area_label = _resolve_clean_copy_area_label(lot_no, child_plot_id, area_m2, area_overrides)
            label_pt = geom_metric.representative_point()
            ax.text(
                label_pt.x,
                label_pt.y,
                f"{lot_no}\n{area_label}",
                ha="center",
                va="center",
                fontsize=max(7, int(6.8 * font_scale)),
                color="black",
                zorder=22,
                fontfamily="DejaVu Serif",
                weight="bold",
            )

        # Place clean-copy north arrow above the map frame and tight to right page edge,
        # matching the requested template look.
        clean_anchor_x = 0.958  # inside inner page border (x=0.97)
        clean_anchor_y = min(0.915, max(map_top + 0.055, 0.86))
        add_north_arrow(
            ax,
            font_scale=font_scale,
            style=str(north_arrow_style or "one_side_stem"),
            color=str(north_arrow_color or "blue"),
            anchor_x=clean_anchor_x,
            anchor_y=clean_anchor_y,
        )

        ax.set_aspect("equal")
        ax.axis("off")
        fig.savefig(output_pdf_path, format="pdf", dpi=dpi, facecolor=fig.get_facecolor())
    finally:
        if fig is not None:
            plt.close(fig)
        matplotlib.rcParams["font.family"] = old_font_family


def _compose_child_title(parent_meta: dict, lot_no: str, estate_name: str | None) -> str:
    estate = str(estate_name or parent_meta.get("estate_name") or "").strip()
    base_title = str(parent_meta.get("title_text") or "SURVEY PLAN").strip()
    if estate:
        return f"{estate} - {lot_no}".strip()
    if base_title:
        return f"{base_title} - {lot_no}".strip()
    return f"SURVEY PLAN - {lot_no}".strip()


def _render_survey_plan_pdf_for_plot(db: Session, plot_id: int, output_pdf_path: str):
    meta = get_plot_meta(db, plot_id)
    epsg_code = COORDINATE_SYSTEMS.get(meta["coordinate_system"], 4326)
    crs_name = COORDINATE_SYSTEM_NAMES.get(meta["coordinate_system"], "WGS84")

    tmp_map = tempfile.NamedTemporaryFile(suffix="_map.png", delete=False)
    map_path = tmp_map.name
    tmp_map.close()

    render_plot_map_layout(
        db=db,
        plot_id=plot_id,
        output_path=map_path,
        title_text=meta["title_text"],
        location_text=meta["location_text"],
        lga_text=meta["lga_text"],
        state_text=meta["state_text"],
        scale_text=meta["scale_text"],
        surveyor_name=meta["surveyor_name"],
        surveyor_rank=meta["surveyor_rank"],
        certification_statement=meta.get("certification_statement"),
        station_names=None,
        coordinate_system=meta["coordinate_system"],
        epsg_code=epsg_code,
        crs_footer_text=f"COORDINATE SYSTEM: {crs_name}",
        paper_size=meta["paper_size"],
        north_arrow_style="one_side_stem",
        north_arrow_color="blue",
        beacon_style="cross",
        road_width_m=None,
        road_width_override_m=None,
        template_name=meta.get("template_name") or DEFAULT_TEMPLATE_NAME,
        adamawa_rof_no=meta.get("adamawa_rof_no") or "",
        adamawa_owner_name=meta.get("adamawa_owner_name") or "",
        adamawa_authority_title=meta.get("adamawa_authority_title") or DEFAULT_ADAMAWA_AUTHORITY_TITLE,
        adamawa_authority_date_text=meta.get("adamawa_authority_date_text") or DEFAULT_ADAMAWA_AUTHORITY_DATE,
        adamawa_control_point_name=meta.get("adamawa_control_point_name") or "",
        adamawa_northing=meta.get("adamawa_northing") or "",
        adamawa_easting=meta.get("adamawa_easting") or "",
        adamawa_elevation=meta.get("adamawa_elevation") or "",
        adamawa_origin_text=meta.get("adamawa_origin_text") or DEFAULT_ADAMAWA_ORIGIN_TEXT,
        adamawa_topo_sheet_text=meta.get("adamawa_topo_sheet_text") or DEFAULT_ADAMAWA_TOPO_SHEET_TEXT,
        adamawa_computation_no=meta.get("adamawa_computation_no") or "",
        adamawa_cadastral_sheet_no=meta.get("adamawa_cadastral_sheet_no") or "",
        adamawa_plan_no=meta.get("adamawa_plan_no") or "",
        adamawa_surveyed_by_text=meta.get("adamawa_surveyed_by_text") or "",
        adamawa_disclaimer_text=meta.get("adamawa_disclaimer_text") or DEFAULT_ADAMAWA_DISCLAIMER_TEXT,
    )
    report = get_plot_report(plot_id, db)
    generate_plot_report_pdf(report, output_pdf_path, map_path, paper_size=meta["paper_size"])
    safe_remove(map_path)


@router.post("/{plot_id}/subdivision/preview")
def preview_plot_subdivision(
    plot_id: int,
    db: Session = Depends(get_db),
    method: str = Body("by_count"),
    split_count: int | None = Body(None),
    target_area_m2: float | None = Body(None),
    orientation_deg: float = Body(0.0),
    lot_prefix: str = Body("LOT"),
    estate_name: str = Body(""),
    fraction_weights: list[float] | None = Body(None),
    fraction_breaks: list[float] | None = Body(None),
    custom_areas_m2: list[float] | None = Body(None),
    lot_names: list[str] | None = Body(None),
):
    parent_geom_wgs84 = _load_plot_polygon_wgs84(db, plot_id)
    payload = _compute_subdivision_payload(
        plot_id,
        parent_geom_wgs84,
        method=method,
        split_count=split_count,
        target_area_m2=target_area_m2,
        orientation_deg=_coerce_float(orientation_deg, 0.0),
        lot_prefix=lot_prefix,
        fraction_weights=fraction_weights,
        fraction_breaks=fraction_breaks,
        custom_areas_m2=custom_areas_m2,
        lot_names=lot_names,
    )
    payload["estate_name"] = str(estate_name or "").strip()
    return payload


@router.post("/{plot_id}/subdivision/apply")
def apply_plot_subdivision(
    plot_id: int,
    db: Session = Depends(get_db),
    method: str = Body("by_count"),
    split_count: int | None = Body(None),
    target_area_m2: float | None = Body(None),
    orientation_deg: float = Body(0.0),
    lot_prefix: str = Body("LOT"),
    estate_name: str = Body(""),
    fraction_weights: list[float] | None = Body(None),
    fraction_breaks: list[float] | None = Body(None),
    custom_areas_m2: list[float] | None = Body(None),
    lot_names: list[str] | None = Body(None),
    include_feature_detection: bool = Body(False),
):
    parent_geom_wgs84 = _load_plot_polygon_wgs84(db, plot_id)
    safe_estate_name = str(estate_name or "").strip()
    payload = _compute_subdivision_payload(
        plot_id,
        parent_geom_wgs84,
        method=method,
        split_count=split_count,
        target_area_m2=target_area_m2,
        orientation_deg=_coerce_float(orientation_deg, 0.0),
        lot_prefix=lot_prefix,
        fraction_weights=fraction_weights,
        fraction_breaks=fraction_breaks,
        custom_areas_m2=custom_areas_m2,
        lot_names=lot_names,
    )
    parent_meta = get_plot_meta(db, plot_id)

    batch_id = db.execute(
        text(
            """
            INSERT INTO plot_subdivision_batches (
                parent_plot_id, estate_name, method, requested_count, target_area_m2,
                orientation_deg, generated_count, total_area_m2, status
            )
            VALUES (
                :parent_plot_id, :estate_name, :method, :requested_count, :target_area_m2,
                :orientation_deg, 0, 0, 'processing'
            )
            RETURNING id
            """
        ),
        {
            "parent_plot_id": int(plot_id),
            "estate_name": safe_estate_name or None,
            "method": payload["method"],
            "requested_count": payload.get("requested_count"),
            "target_area_m2": payload.get("target_area_m2"),
            "orientation_deg": payload.get("orientation_deg") or 0.0,
        },
    ).scalar()
    if batch_id is None:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to create subdivision batch.")

    created_items: list[dict] = []
    try:
        for row in payload["plots"]:
            geom_obj = _clean_single_polygon(shape(row["geometry"]))
            if geom_obj is None:
                raise HTTPException(status_code=400, detail=f"Invalid generated geometry for lot {row['lot_no']}.")

            child_plot = Plot(geom=from_shape(geom_obj, srid=4326))
            db.add(child_plot)
            db.flush()
            child_plot_id = int(child_plot.id)
            lot_no = str(row.get("lot_no") or f"LOT-{child_plot_id}")

            _apply_child_plot_meta(
                db,
                plot_id=child_plot_id,
                title_text=_compose_child_title(parent_meta, lot_no, safe_estate_name),
                parent_meta=parent_meta,
                adamawa_owner_name=lot_no,
            )
            _set_plot_subdivision_meta(
                db,
                child_plot_id=child_plot_id,
                parent_plot_id=int(plot_id),
                batch_id=int(batch_id),
                lot_no=lot_no,
                estate_name=safe_estate_name,
            )
            db.execute(
                text(
                    """
                    INSERT INTO plot_subdivision_items (batch_id, child_plot_id, lot_no, area_m2)
                    VALUES (:batch_id, :child_plot_id, :lot_no, :area_m2)
                    """
                ),
                {
                    "batch_id": int(batch_id),
                    "child_plot_id": child_plot_id,
                    "lot_no": lot_no,
                    "area_m2": float(row.get("area_m2") or 0.0),
                },
            )

            if include_feature_detection:
                _run_plot_feature_detection(db, child_plot_id)

            created_items.append(
                {
                    "child_plot_id": child_plot_id,
                    "lot_no": lot_no,
                    "area_m2": float(row.get("area_m2") or 0.0),
                    "area_hectares": float(row.get("area_hectares") or 0.0),
                }
            )

        db.execute(
            text(
                """
                UPDATE plot_subdivision_batches
                SET generated_count = :generated_count,
                    total_area_m2 = :total_area_m2,
                    status = 'completed',
                    updated_at = NOW()
                WHERE id = :batch_id
                """
            ),
            {
                "batch_id": int(batch_id),
                "generated_count": len(created_items),
                "total_area_m2": float(payload.get("derived_total_area_m2") or 0.0),
            },
        )
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Subdivision apply failed: {exc}")

    return {
        "batch_id": int(batch_id),
        "parent_plot_id": int(plot_id),
        "estate_name": safe_estate_name,
        "method": payload["method"],
        "generated_count": len(created_items),
        "total_area_m2": payload.get("derived_total_area_m2"),
        "plots": created_items,
    }


@router.get("/{plot_id}/subdivision/batches")
def list_plot_subdivision_batches(plot_id: int, db: Session = Depends(get_db)):
    rows = db.execute(
        text(
            """
            SELECT b.id,
                   b.parent_plot_id,
                   b.estate_name,
                   b.method,
                   b.requested_count,
                   b.target_area_m2,
                   b.orientation_deg,
                   b.generated_count,
                   b.total_area_m2,
                   b.status,
                   b.created_at,
                   b.updated_at,
                   COALESCE(COUNT(i.id), 0) AS item_count
            FROM plot_subdivision_batches b
            LEFT JOIN plot_subdivision_items i ON i.batch_id = b.id
            WHERE b.parent_plot_id = :plot_id
            GROUP BY b.id
            ORDER BY b.created_at DESC, b.id DESC
            """
        ),
        {"plot_id": int(plot_id)},
    ).mappings().all()
    return [dict(r) for r in rows]


@router.get("/subdivision/batches/{batch_id}")
def get_plot_subdivision_batch(batch_id: int, include_geojson: bool = Query(False), db: Session = Depends(get_db)):
    batch_row = db.execute(
        text(
            """
            SELECT id, parent_plot_id, estate_name, method, requested_count, target_area_m2,
                   orientation_deg, generated_count, total_area_m2, status, created_at, updated_at
            FROM plot_subdivision_batches
            WHERE id = :batch_id
            """
        ),
        {"batch_id": int(batch_id)},
    ).mappings().first()
    if not batch_row:
        raise HTTPException(status_code=404, detail="Subdivision batch not found.")

    if include_geojson:
        item_rows = db.execute(
            text(
                """
                SELECT i.id,
                       i.batch_id,
                       i.child_plot_id,
                       i.lot_no,
                       i.area_m2,
                       i.created_at,
                       ST_AsGeoJSON(p.geom) AS geojson
                FROM plot_subdivision_items i
                JOIN plots p ON p.id = i.child_plot_id
                WHERE i.batch_id = :batch_id
                ORDER BY i.id ASC
                """
            ),
            {"batch_id": int(batch_id)},
        ).mappings().all()
    else:
        item_rows = db.execute(
            text(
                """
                SELECT id, batch_id, child_plot_id, lot_no, area_m2, created_at
                FROM plot_subdivision_items
                WHERE batch_id = :batch_id
                ORDER BY id ASC
                """
            ),
            {"batch_id": int(batch_id)},
        ).mappings().all()

    return {
        "batch": dict(batch_row),
        "items": [dict(r) for r in item_rows],
    }


@router.get("/subdivision/batches/{batch_id}/export/survey-plans.zip")
def export_subdivision_batch_survey_plans(
    batch_id: int,
    db: Session = Depends(get_db),
    background_tasks: BackgroundTasks = None,
):
    batch_row = db.execute(
        text(
            """
            SELECT id, parent_plot_id, estate_name, method, created_at
            FROM plot_subdivision_batches
            WHERE id = :batch_id
            """
        ),
        {"batch_id": int(batch_id)},
    ).mappings().first()
    if not batch_row:
        raise HTTPException(status_code=404, detail="Subdivision batch not found.")

    items = db.execute(
        text(
            """
            SELECT child_plot_id, lot_no, area_m2
            FROM plot_subdivision_items
            WHERE batch_id = :batch_id
            ORDER BY id ASC
            """
        ),
        {"batch_id": int(batch_id)},
    ).mappings().all()
    if not items:
        raise HTTPException(status_code=404, detail="Subdivision batch has no generated plots.")

    estate_tag = _safe_filename_fragment(str(batch_row.get("estate_name") or ""), f"batch_{batch_id}")
    zip_name = f"{estate_tag}_survey_plans_batch_{batch_id}.zip"
    cache_dir = os.path.join(REPORTS_DIR, "subdivision_batches")
    os.makedirs(cache_dir, exist_ok=True)
    cached_zip_path = os.path.join(cache_dir, zip_name)
    if os.path.isfile(cached_zip_path):
        return FileResponse(
            cached_zip_path,
            media_type="application/zip",
            filename=zip_name,
        )

    tmp_dir = tempfile.mkdtemp(prefix=f"subdivision_batch_{batch_id}_")
    export_rows: list[list[str]] = [["sep=,"], ["lot_no", "child_plot_id", "area_m2"]]
    setting_out_rows: list[list[str]] = [["sep=,"], [
        "lot_no",
        "child_plot_id",
        "point_index",
        "station",
        "longitude",
        "latitude",
        "easting",
        "northing",
        "utm_epsg",
    ]]
    setting_out_rows_raw: list[list[str]] = [[
        "lot_no",
        "child_plot_id",
        "point_index",
        "station",
        "longitude",
        "latitude",
        "easting",
        "northing",
        "utm_epsg",
    ]]
    pdf_files: list[str] = []

    try:
        for item in items:
            child_plot_id = int(item["child_plot_id"])
            lot_no = str(item.get("lot_no") or f"LOT-{child_plot_id}")
            safe_lot = _safe_filename_fragment(lot_no, f"LOT_{child_plot_id}")
            pdf_name = f"{safe_lot}_survey_plan.pdf"
            pdf_path = os.path.join(tmp_dir, pdf_name)
            _render_survey_plan_pdf_for_plot(db, child_plot_id, pdf_path)
            pdf_files.append(pdf_path)
            export_rows.append([lot_no, str(child_plot_id), f"{float(item.get('area_m2') or 0.0):.2f}"])

            try:
                geom_geojson_raw = db.execute(
                    text("SELECT ST_AsGeoJSON(geom) FROM plots WHERE id = :id"),
                    {"id": child_plot_id},
                ).scalar()
                if geom_geojson_raw:
                    geom_obj = _clean_single_polygon(shape(json.loads(geom_geojson_raw)))
                    if geom_obj is not None:
                        coords_wgs = list(geom_obj.exterior.coords)
                        if len(coords_wgs) >= 2 and coords_wgs[0] == coords_wgs[-1]:
                            coords_wgs = coords_wgs[:-1]
                        utm_epsg = _metric_epsg_for_wgs84_polygon(geom_obj)
                        metric_poly = gpd.GeoDataFrame(geometry=[geom_obj], crs="EPSG:4326").to_crs(epsg=utm_epsg).geometry.iloc[0]
                        coords_metric = list(metric_poly.exterior.coords)
                        if len(coords_metric) >= 2 and coords_metric[0] == coords_metric[-1]:
                            coords_metric = coords_metric[:-1]
                        for idx, (wgs_pt, metric_pt) in enumerate(zip(coords_wgs, coords_metric), start=1):
                            lng_val = float(wgs_pt[0])
                            lat_val = float(wgs_pt[1])
                            easting_val = float(metric_pt[0])
                            northing_val = float(metric_pt[1])
                            setting_out_rows.append([
                                lot_no,
                                str(child_plot_id),
                                str(idx),
                                _station_name(idx - 1),
                                _format_nigerian_number(lng_val, 8),
                                _format_nigerian_number(lat_val, 8),
                                _format_nigerian_number(easting_val, 3),
                                _format_nigerian_number(northing_val, 3),
                                str(int(utm_epsg)),
                            ])
                            setting_out_rows_raw.append([
                                lot_no,
                                str(child_plot_id),
                                str(idx),
                                _station_name(idx - 1),
                                f"{lng_val:.8f}",
                                f"{lat_val:.8f}",
                                f"{easting_val:.3f}",
                                f"{northing_val:.3f}",
                                str(int(utm_epsg)),
                            ])
            except Exception:
                # Continue export even if setting-out rows for one lot fail.
                pass

        manifest_path = os.path.join(tmp_dir, "batch_manifest.csv")
        with open(manifest_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(
                f,
                delimiter=",",
                quotechar='"',
                quoting=csv.QUOTE_MINIMAL,
                lineterminator="\n",
            )
            writer.writerows(export_rows)

        setting_out_path = os.path.join(tmp_dir, "setting_out_points_dgps.csv")
        with open(setting_out_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(
                f,
                delimiter=",",
                quotechar='"',
                quoting=csv.QUOTE_MINIMAL,
                lineterminator="\n",
            )
            writer.writerows(setting_out_rows)
        setting_out_raw_path = os.path.join(tmp_dir, "setting_out_points_dgps_raw.csv")
        with open(setting_out_raw_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(
                f,
                delimiter=",",
                quotechar='"',
                quoting=csv.QUOTE_MINIMAL,
                lineterminator="\n",
            )
            writer.writerows(setting_out_rows_raw)

        zip_path = os.path.join(tmp_dir, zip_name)

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(manifest_path, arcname="batch_manifest.csv")
            zf.write(setting_out_path, arcname="setting_out_points_dgps.csv")
            zf.write(setting_out_raw_path, arcname="setting_out_points_dgps_raw.csv")
            for fp in pdf_files:
                if os.path.isfile(fp):
                    zf.write(fp, arcname=os.path.basename(fp))
        try:
            shutil.copyfile(zip_path, cached_zip_path)
        except Exception:
            pass

        if background_tasks is None:
            background_tasks = BackgroundTasks()
        background_tasks.add_task(safe_rmtree, tmp_dir)

        return FileResponse(
            zip_path,
            media_type="application/zip",
            filename=zip_name,
            background=background_tasks,
        )
    except HTTPException:
        safe_rmtree(tmp_dir)
        raise
    except Exception as exc:
        safe_rmtree(tmp_dir)
        raise HTTPException(status_code=500, detail=f"Failed to export subdivision batch: {exc}")


@router.post("/subdivision/batches/{batch_id}/export/clean-copy.pdf")
def export_subdivision_batch_clean_copy_pdf(
    batch_id: int,
    db: Session = Depends(get_db),
    title_text: str = Body(""),
    area_labels: list[dict] | None = Body(None),
    paper_size: str | None = Body(None),
    scale_text: str | None = Body(None),
    coordinate_system: str | None = Body(None),
    station_names: list[str] | None = Body(None),
    north_arrow_style: str = Body("one_side_stem"),
    north_arrow_color: str = Body("blue"),
    beacon_style: str = Body("cross"),
    road_width_m: float | None = Body(None),
):
    batch_row = db.execute(
        text(
            """
            SELECT id, parent_plot_id, estate_name, method, created_at
            FROM plot_subdivision_batches
            WHERE id = :batch_id
            """
        ),
        {"batch_id": int(batch_id)},
    ).mappings().first()
    if not batch_row:
        raise HTTPException(status_code=404, detail="Subdivision batch not found.")

    child_rows = db.execute(
        text(
            """
            SELECT
                i.child_plot_id,
                i.lot_no,
                i.area_m2,
                ST_AsGeoJSON(p.geom) AS geom_geojson
            FROM plot_subdivision_items i
            JOIN plots p ON p.id = i.child_plot_id
            WHERE i.batch_id = :batch_id
            ORDER BY i.id ASC
            """
        ),
        {"batch_id": int(batch_id)},
    ).mappings().all()
    if not child_rows:
        raise HTTPException(status_code=404, detail="Subdivision batch has no generated plots.")

    parent_plot_id = int(batch_row.get("parent_plot_id") or 0)
    if parent_plot_id <= 0:
        raise HTTPException(status_code=400, detail="Subdivision batch parent parcel is invalid.")

    parent_meta = get_plot_meta(db, parent_plot_id)
    effective_paper_size = str(paper_size or parent_meta.get("paper_size") or "A4").upper()
    if effective_paper_size not in {"A4", "A3", "A2", "A1", "A0"}:
        effective_paper_size = "A4"
    effective_scale_text = str(scale_text or parent_meta.get("scale_text") or "1 : 1000")
    effective_coordinate_system = str(coordinate_system or parent_meta.get("coordinate_system") or "wgs84")
    effective_epsg = COORDINATE_SYSTEMS.get(effective_coordinate_system, 4326)

    clean_title = str(title_text or "").strip()
    if not clean_title:
        estate_name = str(batch_row.get("estate_name") or "").strip()
        clean_title = f"{estate_name} CLEAN COPY PLAN" if estate_name else "SURVEY PLAN"

    area_override_map = _extract_clean_copy_area_overrides(area_labels)
    cache_key_payload = {
        "render_version": CLEAN_COPY_RENDER_VERSION,
        "batch_id": int(batch_id),
        "title_text": clean_title,
        "paper_size": effective_paper_size,
        "scale_text": effective_scale_text,
        "coordinate_system": effective_coordinate_system,
        "station_names": list(station_names or []),
        "north_arrow_style": str(north_arrow_style or "one_side_stem"),
        "north_arrow_color": str(north_arrow_color or "blue"),
        "beacon_style": str(beacon_style or "cross"),
        "road_width_m": float(road_width_m or 0.0),
        "area_overrides": sorted(area_override_map.items()),
    }
    cache_hash = hashlib.sha1(json.dumps(cache_key_payload, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    estate_tag = _safe_filename_fragment(str(batch_row.get("estate_name") or ""), f"batch_{batch_id}")
    pdf_name = f"{estate_tag}_clean_copy_batch_{batch_id}_{cache_hash}.pdf"
    cache_dir = os.path.join(REPORTS_DIR, "subdivision_clean_copy")
    os.makedirs(cache_dir, exist_ok=True)
    cached_pdf_path = os.path.join(cache_dir, pdf_name)

    if not os.path.isfile(cached_pdf_path):
        parent_poly_wgs84 = _load_plot_polygon_wgs84(db, parent_plot_id)
        try:
            _render_subdivision_clean_copy_pdf(
                db=db,
                parent_plot_id=int(parent_plot_id),
                parent_poly_wgs84=parent_poly_wgs84,
                child_rows=[dict(r) for r in child_rows],
                output_pdf_path=cached_pdf_path,
                title_text=clean_title,
                paper_size=effective_paper_size,
                scale_text=effective_scale_text,
                coordinate_system=effective_coordinate_system,
                epsg_code=int(effective_epsg),
                station_names=list(station_names or []),
                north_arrow_style=str(north_arrow_style or "one_side_stem"),
                north_arrow_color=str(north_arrow_color or "blue"),
                beacon_style=str(beacon_style or "cross"),
                road_width_m=road_width_m,
                area_overrides=area_override_map,
            )
        except HTTPException:
            raise
        except Exception as exc:
            safe_remove(cached_pdf_path)
            raise HTTPException(status_code=500, detail=f"Failed to export clean copy PDF: {exc}")

    return _pdf_response_with_r2(
        cached_pdf_path,
        pdf_name,
        category="survey_subdivision_clean_copy",
    )


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


@router.get("/{plot_id}/features/geojson")
def get_plot_features_geojson(plot_id: int, db: Session = Depends(get_db)):
    # Buildings and rivers from detected_features
    feature_rows = db.execute(text("""
        SELECT feature_type, ST_AsGeoJSON(geom) AS geojson
        FROM detected_features
        WHERE plot_id = :plot_id
    """), {"plot_id": plot_id}).fetchall()

    # Roads from lines (same logic as renderer)
    road_rows = db.execute(text("""
        WITH roads AS (
            SELECT
                CASE
                    WHEN ST_SRID(r.geom) = 4326 THEN r.geom
                    WHEN ST_SRID(r.geom) = 0 THEN ST_SetSRID(r.geom, 4326)
                    ELSE ST_Transform(r.geom, 4326)
                END AS geom,
                r.highway,
                r.name
            FROM lines r
            WHERE r.highway IS NOT NULL
        )
        SELECT ST_AsGeoJSON(roads.geom) AS geojson, roads.name
        FROM roads
        JOIN plot_buffers b ON b.plot_id = :plot_id
        WHERE ST_Intersects(roads.geom, b.geom)
    """), {"plot_id": plot_id}).fetchall()

    # Overrides (adds only for display)
    override_rows = db.execute(text("""
        SELECT feature_type, action, name, width_m, ST_AsGeoJSON(geom) AS geojson
        FROM plot_feature_overrides
        WHERE plot_id = :plot_id
    """), {"plot_id": plot_id}).fetchall()

    def to_feature(geojson_str, props):
        return {
            "type": "Feature",
            "geometry": json.loads(geojson_str) if geojson_str else None,
            "properties": props,
        }

    import json

    buildings = []
    rivers = []
    fences = []
    for r in feature_rows:
        if not r.geojson:
            continue
        if r.feature_type == "building":
            buildings.append(to_feature(r.geojson, {"source": "detected"}))
        elif r.feature_type == "river":
            rivers.append(to_feature(r.geojson, {"source": "detected"}))
        elif r.feature_type == "fence":
            fences.append(to_feature(r.geojson, {"source": "detected"}))

    roads = []
    for r in road_rows:
        if not r.geojson:
            continue
        roads.append(to_feature(r.geojson, {"source": "detected", "name": r.name}))

    for r in override_rows:
        if not r.geojson:
            continue
        if r.action not in ("add", "update"):
            continue
        feat = to_feature(r.geojson, {"source": "override", "name": r.name, "width_m": r.width_m})
        if r.feature_type == "road":
            roads.append(feat)
        elif r.feature_type == "building":
            buildings.append(feat)
        elif r.feature_type == "river":
            rivers.append(feat)
        elif r.feature_type == "fence":
            fences.append(feat)

    return {
        "roads": {"type": "FeatureCollection", "features": roads},
        "buildings": {"type": "FeatureCollection", "features": buildings},
        "rivers": {"type": "FeatureCollection", "features": rivers},
        "fences": {"type": "FeatureCollection", "features": fences},
    }


# ---------------- FEATURE OVERRIDES ----------------

@router.get("/{plot_id}/feature-overrides")
def get_feature_overrides(plot_id: int, db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT id, feature_type, action, name, width_m, ST_AsGeoJSON(geom) AS geojson, created_at, updated_at
        FROM plot_feature_overrides
        WHERE plot_id = :plot_id
        ORDER BY id DESC
    """), {"plot_id": plot_id}).fetchall()

    return [
        {
            "id": r.id,
            "feature_type": r.feature_type,
            "action": r.action,
            "name": r.name,
            "width_m": r.width_m,
            "geojson": r.geojson,
            "created_at": r.created_at,
            "updated_at": r.updated_at,
        }
        for r in rows
    ]


@router.post("/{plot_id}/feature-overrides")
def add_feature_override(
    plot_id: int,
    db: Session = Depends(get_db),
    feature_type: str = Body(...),
    action: str = Body(...),
    name: str = Body(default=""),
    width_m: float | None = Body(default=None),
    wkt: str | None = Body(default=None),
    geojson: dict | None = Body(default=None),
):
    if feature_type not in {"road", "building", "river", "fence"}:
        raise HTTPException(status_code=400, detail="Invalid feature_type")
    if action not in {"add", "delete", "update"}:
        raise HTTPException(status_code=400, detail="Invalid action")

    geom_wkt = None
    geom_geojson = None
    if wkt:
        geom_wkt = wkt
    elif geojson:
        try:
            geom_geojson = geojson
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid geojson")
    if not geom_wkt:
        if not geom_geojson:
            raise HTTPException(status_code=400, detail="Geometry is required")

    if geom_geojson:
        import json
        db.execute(text("""
            INSERT INTO plot_feature_overrides (plot_id, feature_type, action, name, width_m, geom)
            VALUES (:plot_id, :feature_type, :action, :name, :width_m, ST_SetSRID(ST_GeomFromGeoJSON(:geojson), 4326))
        """), {
            "plot_id": plot_id,
            "feature_type": feature_type,
            "action": action,
            "name": name or None,
            "width_m": width_m,
            "geojson": json.dumps(geom_geojson),
        })
    else:
        db.execute(text("""
            INSERT INTO plot_feature_overrides (plot_id, feature_type, action, name, width_m, geom)
            VALUES (:plot_id, :feature_type, :action, :name, :width_m, ST_SetSRID(ST_GeomFromText(:wkt), 4326))
        """), {
            "plot_id": plot_id,
            "feature_type": feature_type,
            "action": action,
            "name": name or None,
            "width_m": width_m,
            "wkt": geom_wkt,
        })
    db.commit()
    return {"status": "ok"}


@router.delete("/{plot_id}/feature-overrides/{override_id}")
def delete_feature_override(plot_id: int, override_id: int, db: Session = Depends(get_db)):
    res = db.execute(text("""
        DELETE FROM plot_feature_overrides
        WHERE id = :id AND plot_id = :plot_id
    """), {"id": override_id, "plot_id": plot_id})
    db.commit()
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="Override not found")
    return {"status": "ok"}


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

@router.post("/{plot_id}/meta")
def save_plot_metadata(
    plot_id: int,
    db: Session = Depends(get_db),
    title_text: str = Body("SURVEY PLAN"),
    location_text: str = Body(""),
    lga_text: str = Body(""),
    state_text: str = Body(""),
    scale_text: str = Body("1 : 1000"),
    surveyor_name: str = Body(""),
    surveyor_rank: str = Body(""),
    certification_statement: str = Body(DEFAULT_CERTIFICATION_STATEMENT),
    coordinate_system: str = Body("wgs84"),
    paper_size: str = Body("A4"),
    template_name: str = Body(DEFAULT_TEMPLATE_NAME),
    adamawa_rof_no: str = Body(""),
    adamawa_owner_name: str = Body(""),
    adamawa_authority_title: str = Body(DEFAULT_ADAMAWA_AUTHORITY_TITLE),
    adamawa_authority_date_text: str = Body(DEFAULT_ADAMAWA_AUTHORITY_DATE),
    adamawa_control_point_name: str = Body(""),
    adamawa_northing: str = Body(""),
    adamawa_easting: str = Body(""),
    adamawa_elevation: str = Body(""),
    adamawa_origin_text: str = Body(DEFAULT_ADAMAWA_ORIGIN_TEXT),
    adamawa_topo_sheet_text: str = Body(DEFAULT_ADAMAWA_TOPO_SHEET_TEXT),
    adamawa_computation_no: str = Body(""),
    adamawa_cadastral_sheet_no: str = Body(""),
    adamawa_plan_no: str = Body(""),
    adamawa_surveyed_by_text: str = Body(""),
    adamawa_disclaimer_text: str = Body(DEFAULT_ADAMAWA_DISCLAIMER_TEXT),
):
    upsert_plot_meta(
        db=db,
        plot_id=plot_id,
        title_text=title_text,
        location_text=location_text,
        lga_text=lga_text,
        state_text=state_text,
        surveyor_name=surveyor_name,
        surveyor_rank=surveyor_rank,
        certification_statement=certification_statement,
        scale_text=scale_text,
        paper_size=paper_size,
        coordinate_system=coordinate_system,
        template_name=template_name,
        adamawa_rof_no=adamawa_rof_no,
        adamawa_owner_name=adamawa_owner_name,
        adamawa_authority_title=adamawa_authority_title,
        adamawa_authority_date_text=adamawa_authority_date_text,
        adamawa_control_point_name=adamawa_control_point_name,
        adamawa_northing=adamawa_northing,
        adamawa_easting=adamawa_easting,
        adamawa_elevation=adamawa_elevation,
        adamawa_origin_text=adamawa_origin_text,
        adamawa_topo_sheet_text=adamawa_topo_sheet_text,
        adamawa_computation_no=adamawa_computation_no,
        adamawa_cadastral_sheet_no=adamawa_cadastral_sheet_no,
        adamawa_plan_no=adamawa_plan_no,
        adamawa_surveyed_by_text=adamawa_surveyed_by_text,
        adamawa_disclaimer_text=adamawa_disclaimer_text,
    )
    return {"ok": True, "plot_id": int(plot_id)}


@router.post("/{plot_id}/report/pdf")
def download_plot_report_pdf(plot_id: int, db: Session = Depends(get_db), background_tasks: BackgroundTasks = None,
    title_text: str = Body("SURVEY PLAN"),
    location_text: str = Body(""),
    lga_text: str = Body(""),
    state_text: str = Body(""),
    scale_text: str = Body("1 : 1000"),
    surveyor_name: str = Body(""),
    surveyor_rank: str = Body(""),
    certification_statement: str = Body(DEFAULT_CERTIFICATION_STATEMENT),
    station_names: list[str] = Body(default=[]),
    coordinate_system: str = Body("wgs84"),
    paper_size: str = Body("A4"),
    north_arrow_style: str = Body("one_side_stem"),
    north_arrow_color: str = Body("blue"),
    beacon_style: str = Body("cross"),
    road_width_m: float | None = Body(None),
    road_width_override_m: float | None = Body(None),
    template_name: str = Body(DEFAULT_TEMPLATE_NAME),
    adamawa_rof_no: str = Body(""),
    adamawa_owner_name: str = Body(""),
    adamawa_authority_title: str = Body(DEFAULT_ADAMAWA_AUTHORITY_TITLE),
    adamawa_authority_date_text: str = Body(DEFAULT_ADAMAWA_AUTHORITY_DATE),
    adamawa_control_point_name: str = Body(""),
    adamawa_northing: str = Body(""),
    adamawa_easting: str = Body(""),
    adamawa_elevation: str = Body(""),
    adamawa_origin_text: str = Body(DEFAULT_ADAMAWA_ORIGIN_TEXT),
    adamawa_topo_sheet_text: str = Body(DEFAULT_ADAMAWA_TOPO_SHEET_TEXT),
    adamawa_computation_no: str = Body(""),
    adamawa_cadastral_sheet_no: str = Body(""),
    adamawa_plan_no: str = Body(""),
    adamawa_surveyed_by_text: str = Body(""),
    adamawa_disclaimer_text: str = Body(DEFAULT_ADAMAWA_DISCLAIMER_TEXT)):

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
        certification_statement=certification_statement,
        scale_text=scale_text,
        paper_size=paper_size,
        coordinate_system=coordinate_system,
        template_name=template_name,
        adamawa_rof_no=adamawa_rof_no,
        adamawa_owner_name=adamawa_owner_name,
        adamawa_authority_title=adamawa_authority_title,
        adamawa_authority_date_text=adamawa_authority_date_text,
        adamawa_control_point_name=adamawa_control_point_name,
        adamawa_northing=adamawa_northing,
        adamawa_easting=adamawa_easting,
        adamawa_elevation=adamawa_elevation,
        adamawa_origin_text=adamawa_origin_text,
        adamawa_topo_sheet_text=adamawa_topo_sheet_text,
        adamawa_computation_no=adamawa_computation_no,
        adamawa_cadastral_sheet_no=adamawa_cadastral_sheet_no,
        adamawa_plan_no=adamawa_plan_no,
        adamawa_surveyed_by_text=adamawa_surveyed_by_text,
        adamawa_disclaimer_text=adamawa_disclaimer_text,
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
        certification_statement=certification_statement,
        station_names=station_names if station_names else None,
        coordinate_system=coordinate_system,
        epsg_code=epsg_code,
        crs_footer_text=f"COORDINATE SYSTEM: {crs_name}",
        paper_size=paper_size,
        north_arrow_style=north_arrow_style,
        north_arrow_color=north_arrow_color,
        beacon_style=beacon_style,
        road_width_m=road_width_m,
        road_width_override_m=road_width_override_m,
        template_name=template_name,
        adamawa_rof_no=adamawa_rof_no,
        adamawa_owner_name=adamawa_owner_name,
        adamawa_authority_title=adamawa_authority_title,
        adamawa_authority_date_text=adamawa_authority_date_text,
        adamawa_control_point_name=adamawa_control_point_name,
        adamawa_northing=adamawa_northing,
        adamawa_easting=adamawa_easting,
        adamawa_elevation=adamawa_elevation,
        adamawa_origin_text=adamawa_origin_text,
        adamawa_topo_sheet_text=adamawa_topo_sheet_text,
        adamawa_computation_no=adamawa_computation_no,
        adamawa_cadastral_sheet_no=adamawa_cadastral_sheet_no,
        adamawa_plan_no=adamawa_plan_no,
        adamawa_surveyed_by_text=adamawa_surveyed_by_text,
        adamawa_disclaimer_text=adamawa_disclaimer_text,
    )

    report = get_plot_report(plot_id, db)
    generate_plot_report_pdf(report, pdf_path, map_path, paper_size=paper_size)

    safe_remove(map_path)

    filename = f"plot_{plot_id}_report.pdf"
    return _pdf_response_with_r2(
        pdf_path,
        filename,
        category="survey-plan",
        project_id=plot_id,
    )


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

    filename = f"plot_{plot_id}_report.pdf"
    return _pdf_response_with_r2(
        pdf_path,
        filename,
        category="survey-plan",
        project_id=plot_id,
    )


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
    certification_statement: str = Body(DEFAULT_CERTIFICATION_STATEMENT),
    station_names: list[str] = Body(default=[]),
    coordinate_system: str = Body("wgs84"),
    paper_size: str = Body("A4"),
    north_arrow_style: str = Body("one_side_stem"),
    north_arrow_color: str = Body("blue"),
    beacon_style: str = Body("cross"),
    road_width_m: float | None = Body(None),
    road_width_override_m: float | None = Body(None),
    template_name: str = Body(DEFAULT_TEMPLATE_NAME),
    adamawa_rof_no: str = Body(""),
    adamawa_owner_name: str = Body(""),
    adamawa_authority_title: str = Body(DEFAULT_ADAMAWA_AUTHORITY_TITLE),
    adamawa_authority_date_text: str = Body(DEFAULT_ADAMAWA_AUTHORITY_DATE),
    adamawa_control_point_name: str = Body(""),
    adamawa_northing: str = Body(""),
    adamawa_easting: str = Body(""),
    adamawa_elevation: str = Body(""),
    adamawa_origin_text: str = Body(DEFAULT_ADAMAWA_ORIGIN_TEXT),
    adamawa_topo_sheet_text: str = Body(DEFAULT_ADAMAWA_TOPO_SHEET_TEXT),
    adamawa_computation_no: str = Body(""),
    adamawa_cadastral_sheet_no: str = Body(""),
    adamawa_plan_no: str = Body(""),
    adamawa_surveyed_by_text: str = Body(""),
    adamawa_disclaimer_text: str = Body(DEFAULT_ADAMAWA_DISCLAIMER_TEXT)):

    payload_for_cache = {
        "_layout_version": PREVIEW_LAYOUT_VERSION,
        "title_text": title_text,
        "location_text": location_text,
        "lga_text": lga_text,
        "state_text": state_text,
        "scale_text": scale_text,
        "surveyor_name": surveyor_name,
        "surveyor_rank": surveyor_rank,
        "certification_statement": certification_statement,
        "station_names": station_names or [],
        "coordinate_system": coordinate_system,
        "paper_size": paper_size,
        "north_arrow_style": north_arrow_style,
        "north_arrow_color": north_arrow_color,
        "beacon_style": beacon_style,
        "road_width_m": road_width_m,
        "road_width_override_m": road_width_override_m,
        "template_name": template_name,
        "adamawa_rof_no": adamawa_rof_no,
        "adamawa_owner_name": adamawa_owner_name,
        "adamawa_authority_title": adamawa_authority_title,
        "adamawa_authority_date_text": adamawa_authority_date_text,
        "adamawa_control_point_name": adamawa_control_point_name,
        "adamawa_northing": adamawa_northing,
        "adamawa_easting": adamawa_easting,
        "adamawa_elevation": adamawa_elevation,
        "adamawa_origin_text": adamawa_origin_text,
        "adamawa_topo_sheet_text": adamawa_topo_sheet_text,
        "adamawa_computation_no": adamawa_computation_no,
        "adamawa_cadastral_sheet_no": adamawa_cadastral_sheet_no,
        "adamawa_plan_no": adamawa_plan_no,
        "adamawa_surveyed_by_text": adamawa_surveyed_by_text,
        "adamawa_disclaimer_text": adamawa_disclaimer_text,
    }
    revision_token = build_preview_revision_token(db, plot_id)
    cache_key = build_preview_cache_key(plot_id, payload_for_cache, revision_token)
    prune_preview_cache(plot_id, variant="survey")
    cached_path = get_cached_preview_path(plot_id, cache_key, variant="survey")
    if cached_path:
        return FileResponse(
            cached_path,
            media_type="image/png",
            headers={"Cache-Control": "no-store"},
        )

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
        certification_statement=certification_statement,
        scale_text=scale_text,
        paper_size=paper_size,
        coordinate_system=coordinate_system,
        template_name=template_name,
        adamawa_rof_no=adamawa_rof_no,
        adamawa_owner_name=adamawa_owner_name,
        adamawa_authority_title=adamawa_authority_title,
        adamawa_authority_date_text=adamawa_authority_date_text,
        adamawa_control_point_name=adamawa_control_point_name,
        adamawa_northing=adamawa_northing,
        adamawa_easting=adamawa_easting,
        adamawa_elevation=adamawa_elevation,
        adamawa_origin_text=adamawa_origin_text,
        adamawa_topo_sheet_text=adamawa_topo_sheet_text,
        adamawa_computation_no=adamawa_computation_no,
        adamawa_cadastral_sheet_no=adamawa_cadastral_sheet_no,
        adamawa_plan_no=adamawa_plan_no,
        adamawa_surveyed_by_text=adamawa_surveyed_by_text,
        adamawa_disclaimer_text=adamawa_disclaimer_text,
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
        certification_statement=certification_statement,
        station_names=station_names if station_names else None,
        coordinate_system=coordinate_system,
        epsg_code=epsg_code,
        crs_footer_text=f"COORDINATE SYSTEM: {crs_name}",
        paper_size=paper_size,
        north_arrow_style=north_arrow_style,
        north_arrow_color=north_arrow_color,
        beacon_style=beacon_style,
        road_width_m=road_width_m,
        road_width_override_m=road_width_override_m,
        preview_mode=True,
        template_name=template_name,
        adamawa_rof_no=adamawa_rof_no,
        adamawa_owner_name=adamawa_owner_name,
        adamawa_authority_title=adamawa_authority_title,
        adamawa_authority_date_text=adamawa_authority_date_text,
        adamawa_control_point_name=adamawa_control_point_name,
        adamawa_northing=adamawa_northing,
        adamawa_easting=adamawa_easting,
        adamawa_elevation=adamawa_elevation,
        adamawa_origin_text=adamawa_origin_text,
        adamawa_topo_sheet_text=adamawa_topo_sheet_text,
        adamawa_computation_no=adamawa_computation_no,
        adamawa_cadastral_sheet_no=adamawa_cadastral_sheet_no,
        adamawa_plan_no=adamawa_plan_no,
        adamawa_surveyed_by_text=adamawa_surveyed_by_text,
        adamawa_disclaimer_text=adamawa_disclaimer_text,
    )

    cache_path = preview_cache_path(plot_id, cache_key, variant="survey")
    served_path = map_path
    served_background = background_tasks
    try:
        shutil.copyfile(map_path, cache_path)
        safe_remove(map_path)
        served_path = cache_path
        served_background = None
    except Exception:
        pass

    if served_path == map_path:
        if served_background is None:
            served_background = BackgroundTasks()
        served_background.add_task(safe_remove, map_path)

    return FileResponse(
        served_path,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
        background=served_background,
    )


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

    filename = f"plot_{plot_id}_back_computation.pdf"
    return _pdf_response_with_r2(
        pdf_path,
        filename,
        category="back-computation",
        project_id=plot_id,
    )


# ---------------- ORTHOPHOTO ----------------

@router.post("/{plot_id}/orthophoto/preview")
def orthophoto_preview(plot_id: int, db: Session = Depends(get_db), background_tasks: BackgroundTasks = None,
    scale_text: str = Body("1 : 1000"),
    station_names: list[str] = Body(default=[]),
    coordinate_system: str = Body("wgs84"),
    paper_size: str = Body("A4"),
    use_topo_map: bool = Body(False),
    topo_source: str = Body("opentopomap"),
    north_arrow_style: str = Body("one_side_stem"),
    north_arrow_color: str = Body("blue")):

    payload_for_cache = {
        "scale_text": scale_text,
        "station_names": station_names or [],
        "coordinate_system": coordinate_system,
        "paper_size": paper_size,
        "use_topo_map": bool(use_topo_map),
        "topo_source": topo_source or "opentopomap",
        "north_arrow_style": north_arrow_style,
        "north_arrow_color": north_arrow_color,
    }
    revision_token = build_plot_geom_revision_token(db, plot_id)
    cache_key = build_preview_cache_key(plot_id, payload_for_cache, revision_token)
    cache_variant = "topomap" if use_topo_map else "orthophoto"
    prune_preview_cache(plot_id, variant=cache_variant)
    cached_path = get_cached_preview_path(plot_id, cache_key, variant=cache_variant)
    if cached_path:
        return FileResponse(
            cached_path,
            media_type="image/png",
            headers={"Cache-Control": "no-store"},
        )

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
        preview_mode=True,
    )

    cache_path = preview_cache_path(plot_id, cache_key, variant=cache_variant)
    served_path = png_path
    served_background = background_tasks
    try:
        shutil.copyfile(png_path, cache_path)
        safe_remove(png_path)
        served_path = cache_path
        served_background = None
    except Exception:
        pass

    if served_path == png_path:
        if served_background is None:
            served_background = BackgroundTasks()
        served_background.add_task(safe_remove, png_path)

    return FileResponse(
        served_path,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
        background=served_background,
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
    north_arrow_style: str = Body("one_side_stem"),
    north_arrow_color: str = Body("blue")):

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

    return _pdf_response_with_r2(
        pdf_path,
        filename,
        category="orthophoto" if not use_topo_map else "topo-map",
        project_id=plot_id,
    )
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


@router.get("/{plot_id}/survey-plan/shapefile")
def download_survey_plan_shapefile(
    plot_id: int,
    db: Session = Depends(get_db),
    background_tasks: BackgroundTasks = None,
):
    plot_wkb = db.execute(text("SELECT geom FROM plots WHERE id=:id"), {"id": plot_id}).scalar()
    if not plot_wkb:
        raise HTTPException(status_code=404, detail="Plot not found")

    meta = get_plot_meta(db, plot_id)
    plot_geom = wkb.loads(plot_wkb)
    feature_sets = get_plot_features_geojson(plot_id, db)

    tmp_dir = tempfile.mkdtemp(prefix=f"plot_{plot_id}_shp_")
    base_name = f"plot_{plot_id}_survey_plan"
    zip_path = os.path.join(tmp_dir, f"{base_name}_shapefile.zip")

    try:
        def station_name(idx: int) -> str:
            name = ""
            num = idx
            while True:
                name = chr(65 + (num % 26)) + name
                num = (num // 26) - 1
                if num < 0:
                    break
            return name

        def write_layer(layer_suffix: str, records: list[dict]):
            if not records:
                return
            layer_base = f"plot_{plot_id}_{layer_suffix}"
            layer_path = os.path.join(tmp_dir, f"{layer_base}.shp")
            gdf = gpd.GeoDataFrame(records, crs="EPSG:4326")
            if gdf.empty:
                return
            gdf.to_file(layer_path, driver="ESRI Shapefile")

        # 1) Plot boundary polygon (main parcel geometry)
        write_layer("boundary", [{
            "plot_id": int(plot_id),
            "title": (meta.get("title_text") or "SURVEY PLAN")[:80],
            "loc_text": (meta.get("location_text") or "")[:80],
            "lga_text": (meta.get("lga_text") or "")[:80],
            "state_txt": (meta.get("state_text") or "")[:80],
            "surveyr": (meta.get("surveyor_name") or "")[:80],
            "rank_txt": (meta.get("surveyor_rank") or "")[:80],
            "scale_txt": (meta.get("scale_text") or "")[:40],
            "geometry": plot_geom,
        }])

        # 2) Station/vertex points (A, B, C...)
        vertex_records = []
        coords = list(getattr(plot_geom, "exterior", plot_geom).coords)
        if coords and len(coords) > 1 and coords[0] == coords[-1]:
            coords = coords[:-1]
        for idx, (lng, lat, *_) in enumerate(coords):
            vertex_records.append({
                "plot_id": int(plot_id),
                "stn": station_name(idx),
                "ord_no": idx + 1,
                "lng": float(lng),
                "lat": float(lat),
                "geometry": Point(float(lng), float(lat)),
            })
        write_layer("stations", vertex_records)

        # 3) Map features shown in plan (roads/buildings/rivers/fences)
        layer_map = [
            ("roads", feature_sets.get("roads", {}), "road"),
            ("buildings", feature_sets.get("buildings", {}), "building"),
            ("rivers", feature_sets.get("rivers", {}), "river"),
            ("fences", feature_sets.get("fences", {}), "fence"),
        ]
        for suffix, collection, default_type in layer_map:
            feat_records = []
            for feat in (collection or {}).get("features", []):
                geom_json = feat.get("geometry")
                if not geom_json:
                    continue
                try:
                    geom_obj = shape(geom_json)
                except Exception:
                    continue
                props = feat.get("properties") or {}
                feat_records.append({
                    "plot_id": int(plot_id),
                    "ftype": default_type[:10],
                    "src": str(props.get("source") or "")[:20],
                    "name": str(props.get("name") or "")[:80],
                    "width_m": float(props.get("width_m")) if props.get("width_m") is not None else None,
                    "geometry": geom_obj,
                })
            write_layer(suffix, feat_records)
    except Exception as e:
        safe_rmtree(tmp_dir)
        raise HTTPException(status_code=500, detail=f"Failed to generate shapefile: {e}")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zip_name = os.path.basename(zip_path)
        for fname in os.listdir(tmp_dir):
            if fname == zip_name:
                continue
            fp = os.path.join(tmp_dir, fname)
            if os.path.isfile(fp):
                zf.write(fp, arcname=fname)

    if background_tasks is None:
        background_tasks = BackgroundTasks()
    background_tasks.add_task(safe_rmtree, tmp_dir)

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"{base_name}_shapefile.zip",
        background=background_tasks,
    )


@router.get("/{plot_id}/reports/survey-plan")
def get_saved_survey_plan_pdf(plot_id: int, refresh: bool = False, db: Session = Depends(get_db)):
    pdf_path = resolve_existing_path([
        os.path.join(REPORTS_DIR, f"plot_{plot_id}_report.pdf"),
        f"app/reports/plot_{plot_id}_report.pdf",
    ])
    if refresh or not pdf_path:
        meta = get_plot_meta(db, plot_id)
        maps_dir = os.path.join(REPORTS_DIR, "maps")
        os.makedirs(REPORTS_DIR, exist_ok=True)
        os.makedirs(maps_dir, exist_ok=True)
        pdf_path = os.path.join(REPORTS_DIR, f"plot_{plot_id}_report.pdf")
        tmp_map = tempfile.NamedTemporaryFile(suffix="_map.png", delete=False)
        map_path = tmp_map.name
        tmp_map.close()
        epsg_code = COORDINATE_SYSTEMS.get(meta["coordinate_system"], 4326)
        crs_name = COORDINATE_SYSTEM_NAMES.get(meta["coordinate_system"], "WGS84")
        render_plot_map_layout(
            db=db,
            plot_id=plot_id,
            output_path=map_path,
            title_text=meta["title_text"],
            location_text=meta["location_text"],
            lga_text=meta["lga_text"],
            state_text=meta["state_text"],
            scale_text=meta["scale_text"],
            surveyor_name=meta["surveyor_name"],
            surveyor_rank=meta["surveyor_rank"],
            certification_statement=meta.get("certification_statement"),
            station_names=None,
            coordinate_system=meta["coordinate_system"],
            epsg_code=epsg_code,
            crs_footer_text=f"COORDINATE SYSTEM: {crs_name}",
            paper_size=meta["paper_size"],
            north_arrow_style="one_side_stem",
            north_arrow_color="blue",
            beacon_style="cross",
            road_width_m=None,
            road_width_override_m=None,
            template_name=meta.get("template_name") or DEFAULT_TEMPLATE_NAME,
            adamawa_rof_no=meta.get("adamawa_rof_no") or "",
            adamawa_owner_name=meta.get("adamawa_owner_name") or "",
            adamawa_authority_title=meta.get("adamawa_authority_title") or DEFAULT_ADAMAWA_AUTHORITY_TITLE,
            adamawa_authority_date_text=meta.get("adamawa_authority_date_text") or DEFAULT_ADAMAWA_AUTHORITY_DATE,
            adamawa_control_point_name=meta.get("adamawa_control_point_name") or "",
            adamawa_northing=meta.get("adamawa_northing") or "",
            adamawa_easting=meta.get("adamawa_easting") or "",
            adamawa_elevation=meta.get("adamawa_elevation") or "",
            adamawa_origin_text=meta.get("adamawa_origin_text") or DEFAULT_ADAMAWA_ORIGIN_TEXT,
            adamawa_topo_sheet_text=meta.get("adamawa_topo_sheet_text") or DEFAULT_ADAMAWA_TOPO_SHEET_TEXT,
            adamawa_computation_no=meta.get("adamawa_computation_no") or "",
            adamawa_cadastral_sheet_no=meta.get("adamawa_cadastral_sheet_no") or "",
            adamawa_plan_no=meta.get("adamawa_plan_no") or "",
            adamawa_surveyed_by_text=meta.get("adamawa_surveyed_by_text") or "",
            adamawa_disclaimer_text=meta.get("adamawa_disclaimer_text") or DEFAULT_ADAMAWA_DISCLAIMER_TEXT,
        )
        report = get_plot_report(plot_id, db)
        generate_plot_report_pdf(report, pdf_path, map_path, paper_size=meta["paper_size"])
        safe_remove(map_path)
    if not pdf_path or not os.path.exists(pdf_path):
        raise HTTPException(status_code=404, detail="Survey plan PDF not found")
    filename = f"plot_{plot_id}_survey_plan.pdf"
    return _pdf_response_with_r2(
        pdf_path,
        filename,
        category="survey-plan",
        project_id=plot_id,
    )


@router.get("/{plot_id}/reports/orthophoto")
def get_saved_orthophoto_pdf(plot_id: int, map_type: str = "satellite", refresh: bool = False, db: Session = Depends(get_db)):
    safe_type = "topo" if str(map_type).lower() in ["topo", "topomap", "topo_map"] else "satellite"
    pdf_path = resolve_existing_path([
        os.path.join(REPORTS_DIR, "orthophoto", f"plot_{plot_id}_orthophoto_{safe_type}.pdf"),
        f"app/reports/orthophoto/plot_{plot_id}_orthophoto_{safe_type}.pdf",
    ])
    if refresh or not pdf_path:
        meta = get_plot_meta(db, plot_id)
        out_dir = os.path.join(REPORTS_DIR, "orthophoto")
        os.makedirs(out_dir, exist_ok=True)
        pdf_path = os.path.join(out_dir, f"plot_{plot_id}_orthophoto_{safe_type}.pdf")
        tmp_png = tempfile.NamedTemporaryFile(suffix=f"_{safe_type}.png", delete=False)
        png_path = tmp_png.name
        tmp_png.close()
        epsg_code = COORDINATE_SYSTEMS.get(meta["coordinate_system"], 4326)
        crs_name = COORDINATE_SYSTEM_NAMES.get(meta["coordinate_system"], "WGS84")
        render_orthophoto_png(
            db=db,
            plot_id=plot_id,
            output_path=png_path,
            title_text=meta["title_text"] if safe_type == "satellite" else "TOPO MAP",
            location_text=meta["location_text"],
            lga_text=meta["lga_text"],
            state_text=meta["state_text"],
            scale_text=meta["scale_text"],
            surveyor_name=meta["surveyor_name"],
            surveyor_rank=meta["surveyor_rank"],
            station_names=None,
            coordinate_system=meta["coordinate_system"],
            epsg_code=epsg_code,
            crs_footer_text=f"COORDINATE SYSTEM: {crs_name}",
            source_footer_text="SOURCE: OpenTopoMap" if safe_type == "topo" else "SOURCE: Satellite Imagery",
            use_topo_map=(safe_type == "topo"),
            paper_size=meta["paper_size"],
            north_arrow_style="one_side_stem",
            north_arrow_color="blue",
        )
        render_orthophoto_pdf_from_png(png_path, pdf_path, paper_size=meta["paper_size"])
        safe_remove(png_path)
    if not pdf_path or not os.path.exists(pdf_path):
        raise HTTPException(status_code=404, detail="Orthophoto PDF not found")
    filename = f"plot_{plot_id}_{'topomap' if safe_type == 'topo' else 'orthophoto'}.pdf"
    return _pdf_response_with_r2(
        pdf_path,
        filename,
        category="orthophoto" if safe_type != "topo" else "topo-map",
        project_id=plot_id,
    )


@router.get("/{plot_id}/reports/back-computation")
def get_saved_back_computation_pdf(plot_id: int, refresh: bool = False, db: Session = Depends(get_db)):
    pdf_path = resolve_existing_path([
        os.path.join(REPORTS_DIR, f"plot_{plot_id}_back_computation.pdf"),
        f"app/reports/plot_{plot_id}_back_computation.pdf",
    ])
    if refresh or not pdf_path:
        meta = get_plot_meta(db, plot_id)
        pdf_path = os.path.join(REPORTS_DIR, f"plot_{plot_id}_back_computation.pdf")
        os.makedirs(REPORTS_DIR, exist_ok=True)
        # Get plot geometry
        plot_wkb = db.execute(text("SELECT geom FROM plots WHERE id=:id"), {"id": plot_id}).scalar()
        plot_geom = wkb.loads(plot_wkb)
        area_m2 = db.execute(
            text("SELECT ST_Area(geom::geography) FROM plots WHERE id=:id"),
            {"id": plot_id}
        ).scalar() or 0
        gdf = gpd.GeoDataFrame(geometry=[plot_geom], crs="EPSG:4326")
        epsg_code = COORDINATE_SYSTEMS.get(meta["coordinate_system"], 4326)
        crs_name = COORDINATE_SYSTEM_NAMES.get(meta["coordinate_system"], "WGS84")
        if meta["coordinate_system"] == "wgs84":
            centroid = plot_geom.centroid
            utm_zone = int((centroid.x + 180) / 6) + 1
            hemisphere = "north" if centroid.y >= 0 else "south"
            epsg_code = 32600 + utm_zone if hemisphere == "north" else 32700 + utm_zone
            crs_name = f"UTM Zone {utm_zone}{'N' if hemisphere == 'north' else 'S'}"
        gdf_projected = gdf.to_crs(epsg=epsg_code)
        poly = gdf_projected.geometry.iloc[0]
        rows, sum_de, sum_dn = compute_back_computation(poly, None)
        render_back_computation_pdf(rows, sum_de, sum_dn, area_m2, plot_id, pdf_path, crs_name)
    if not pdf_path or not os.path.exists(pdf_path):
        raise HTTPException(status_code=404, detail="Back computation PDF not found")
    filename = f"plot_{plot_id}_back_computation.pdf"
    return _pdf_response_with_r2(
        pdf_path,
        filename,
        category="back-computation",
        project_id=plot_id,
    )
