# app/routers/plots.py

from fastapi import APIRouter, Depends, Body, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from geoalchemy2.shape import from_shape
from shapely.geometry import Polygon, shape, Point
from sqlalchemy import text
from fastapi.responses import FileResponse
from app.schemas.plot_create import PlotCreateRequest
from typing import Optional, Union, List

import os
import tempfile
import glob
import re
import shutil
import zipfile
import time
import json
import hashlib
from threading import Lock

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
PREVIEW_CACHE_DIR = os.path.join(REPORTS_DIR, "previews_cache")
PREVIEW_CACHE_TTL_SECONDS = max(30, int(os.getenv("PLOT_PREVIEW_CACHE_TTL_SECONDS", "180")))
PREVIEW_CACHE_MAX_FILES_PER_PLOT = max(5, int(os.getenv("PLOT_PREVIEW_CACHE_MAX_FILES_PER_PLOT", "24")))
PREVIEW_LAYOUT_VERSION = "survey_layout_2026_03_10_adamawa_v7"

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
    db.commit()


def get_plot_meta(db: Session, plot_id: int) -> dict:
    row = db.execute(text("""
        SELECT title_text, location_text, lga_text, state_text,
               surveyor_name, surveyor_rank, certification_statement, scale_text, paper_size, coordinate_system,
               template_name, adamawa_rof_no, adamawa_owner_name, adamawa_authority_title, adamawa_authority_date_text,
               adamawa_control_point_name, adamawa_northing, adamawa_easting, adamawa_elevation, adamawa_origin_text,
               adamawa_topo_sheet_text, adamawa_computation_no, adamawa_cadastral_sheet_no, adamawa_plan_no,
               adamawa_surveyed_by_text, adamawa_disclaimer_text
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
    }


def safe_remove(path: str):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except Exception:
        pass


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
    north_arrow_style: str = Body("classic"),
    north_arrow_color: str = Body("black"),
    beacon_style: str = Body("circle"),
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
    certification_statement: str = Body(DEFAULT_CERTIFICATION_STATEMENT),
    station_names: list[str] = Body(default=[]),
    coordinate_system: str = Body("wgs84"),
    paper_size: str = Body("A4"),
    north_arrow_style: str = Body("classic"),
    north_arrow_color: str = Body("black"),
    beacon_style: str = Body("circle"),
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

    return FileResponse(pdf_path, filename=f"plot_{plot_id}_back_computation.pdf")


# ---------------- ORTHOPHOTO ----------------

@router.post("/{plot_id}/orthophoto/preview")
def orthophoto_preview(plot_id: int, db: Session = Depends(get_db), background_tasks: BackgroundTasks = None,
    scale_text: str = Body("1 : 1000"),
    station_names: list[str] = Body(default=[]),
    coordinate_system: str = Body("wgs84"),
    paper_size: str = Body("A4"),
    use_topo_map: bool = Body(False),
    topo_source: str = Body("opentopomap"),
    north_arrow_style: str = Body("classic"),
    north_arrow_color: str = Body("black")):

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
            north_arrow_style="classic",
            north_arrow_color="black",
            beacon_style="circle",
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
    return FileResponse(pdf_path, media_type="application/pdf", filename=f"plot_{plot_id}_survey_plan.pdf")


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
            north_arrow_style="classic",
            north_arrow_color="black",
        )
        render_orthophoto_pdf_from_png(png_path, pdf_path, paper_size=meta["paper_size"])
        safe_remove(png_path)
    if not pdf_path or not os.path.exists(pdf_path):
        raise HTTPException(status_code=404, detail="Orthophoto PDF not found")
    filename = f"plot_{plot_id}_{'topomap' if safe_type == 'topo' else 'orthophoto'}.pdf"
    return FileResponse(pdf_path, media_type="application/pdf", filename=filename)


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
    return FileResponse(pdf_path, media_type="application/pdf", filename=f"plot_{plot_id}_back_computation.pdf")
