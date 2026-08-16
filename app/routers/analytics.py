# app/routers/analytics.py

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from sqlalchemy import text, func, bindparam
from datetime import datetime, timedelta
import json
import os
import glob

from app.db import SessionLocal
from app.utils.auth_security import require_super_admin_request
from app.utils.survey_auth_security import ensure_survey_auth_schema

router = APIRouter(prefix="/analytics", tags=["analytics"])

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
REPORTS_DIR = os.path.join(BASE_DIR, "reports")


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
            template_name VARCHAR(40) DEFAULT 'general',
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
        ("template_name", "VARCHAR(40) DEFAULT 'general'"),
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


def ensure_plot_export_jobs_table(db: Session):
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS plot_export_jobs (
            id TEXT PRIMARY KEY,
            export_type TEXT NOT NULL,
            plot_id INTEGER REFERENCES plots(id) ON DELETE CASCADE,
            subdivision_batch_id INTEGER REFERENCES plot_subdivision_batches(id) ON DELETE CASCADE,
            cache_key TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            file_name TEXT,
            local_path TEXT,
            request_payload JSONB,
            error_text TEXT,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """))
    columns_to_add = [
        ("cache_key", "TEXT"),
        ("status", "TEXT NOT NULL DEFAULT 'queued'"),
        ("file_name", "TEXT"),
        ("local_path", "TEXT"),
        ("request_payload", "JSONB"),
        ("error_text", "TEXT"),
        ("started_at", "TIMESTAMP"),
        ("completed_at", "TIMESTAMP"),
        ("created_at", "TIMESTAMP DEFAULT NOW()"),
        ("updated_at", "TIMESTAMP DEFAULT NOW()"),
    ]
    for col_name, col_type in columns_to_add:
        try:
            db.execute(text(f"ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS {col_name} {col_type}"))
        except Exception:
            pass
    db.commit()


def ensure_plots_created_at(db: Session):
    try:
        db.execute(text("ALTER TABLE plots ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()"))
        db.commit()
    except Exception:
        db.rollback()


def build_report_flags(plot_id: int):
    base_dir = REPORTS_DIR
    legacy_dir = os.path.join("app", "reports")
    orthophoto_dir = os.path.join(base_dir, "orthophoto")
    previews_dir = os.path.join(base_dir, "previews")
    dwg_dir = os.path.join(base_dir, "dwg")
    legacy_orthophoto_dir = os.path.join(legacy_dir, "orthophoto")
    legacy_previews_dir = os.path.join(legacy_dir, "previews")
    legacy_dwg_dir = os.path.join(legacy_dir, "dwg")

    def exists(path: str) -> bool:
        return os.path.exists(path) and os.path.getsize(path) > 0

    def exists_any(paths: list[str]) -> bool:
        return any(exists(p) for p in paths)

    orthophoto_preview_glob = glob.glob(
        os.path.join(orthophoto_dir, f"plot_{plot_id}_orthophoto_satellite_preview*.png")
    ) + glob.glob(
        os.path.join(orthophoto_dir, f"plot_{plot_id}_orthophoto_preview*.png")
    ) + glob.glob(
        os.path.join(legacy_orthophoto_dir, f"plot_{plot_id}_orthophoto_satellite_preview*.png")
    ) + glob.glob(
        os.path.join(legacy_orthophoto_dir, f"plot_{plot_id}_orthophoto_preview*.png")
    )
    topo_preview_glob = glob.glob(
        os.path.join(orthophoto_dir, f"plot_{plot_id}_orthophoto_topo_preview*.png")
    ) + glob.glob(
        os.path.join(legacy_orthophoto_dir, f"plot_{plot_id}_orthophoto_topo_preview*.png")
    )

    return {
        "survey_plan_pdf": exists_any([
            os.path.join(base_dir, f"plot_{plot_id}_report.pdf"),
            os.path.join(legacy_dir, f"plot_{plot_id}_report.pdf"),
        ]),
        "survey_plan_preview": exists_any([
            os.path.join(previews_dir, f"plot_{plot_id}_preview.png"),
            os.path.join(legacy_previews_dir, f"plot_{plot_id}_preview.png"),
        ]),
        "orthophoto_pdf": (
            exists_any([
                os.path.join(orthophoto_dir, f"plot_{plot_id}_orthophoto_satellite.pdf"),
                os.path.join(legacy_orthophoto_dir, f"plot_{plot_id}_orthophoto_satellite.pdf"),
            ])
            or exists_any([
                os.path.join(orthophoto_dir, f"plot_{plot_id}_orthophoto.pdf"),
                os.path.join(legacy_orthophoto_dir, f"plot_{plot_id}_orthophoto.pdf"),
            ])
        ),
        "topo_map_pdf": exists_any([
            os.path.join(orthophoto_dir, f"plot_{plot_id}_orthophoto_topo.pdf"),
            os.path.join(legacy_orthophoto_dir, f"plot_{plot_id}_orthophoto_topo.pdf"),
        ]),
        "orthophoto_preview": len(orthophoto_preview_glob) > 0,
        "topo_map_preview": len(topo_preview_glob) > 0,
        "dwg": exists_any([
            os.path.join(dwg_dir, f"plot_{plot_id}_survey_plan.dxf"),
            os.path.join(legacy_dwg_dir, f"plot_{plot_id}_survey_plan.dxf"),
        ]),
        "back_computation_pdf": exists_any([
            os.path.join(base_dir, f"plot_{plot_id}_back_computation.pdf"),
            os.path.join(legacy_dir, f"plot_{plot_id}_back_computation.pdf"),
        ]),
    }


@router.get("/overview")
def get_analytics_overview(db: Session = Depends(get_db)):
    """Get overview analytics for admin dashboard."""

    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=7)
    month_start = today_start - timedelta(days=30)

    # Total plots
    total_plots = 0
    plots_today = 0
    plots_week = 0
    plots_month = 0

    try:
        total_plots = db.execute(text("SELECT COUNT(*) FROM plots")).scalar() or 0

        # Try to get time-based stats (only if created_at column exists)
        try:
            plots_today = db.execute(
                text("SELECT COUNT(*) FROM plots WHERE created_at >= :start"),
                {"start": today_start}
            ).scalar() or 0

            plots_week = db.execute(
                text("SELECT COUNT(*) FROM plots WHERE created_at >= :start"),
                {"start": week_start}
            ).scalar() or 0

            plots_month = db.execute(
                text("SELECT COUNT(*) FROM plots WHERE created_at >= :start"),
                {"start": month_start}
            ).scalar() or 0
        except Exception:
            # created_at column doesn't exist, use total for all
            plots_today = total_plots
            plots_week = total_plots
            plots_month = total_plots
    except Exception:
        pass

    # Total features detected
    total_features = 0
    features_by_type = {"building": 0, "road": 0, "river": 0}
    try:
        # Check if detected_features table exists
        table_check = db.execute(text("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_name = 'detected_features'
            )
        """)).scalar()

        if table_check:
            total_features = db.execute(text("SELECT COUNT(*) FROM detected_features")).scalar() or 0

            rows = db.execute(text("""
                SELECT feature_type, COUNT(*) as count
                FROM detected_features
                GROUP BY feature_type
            """)).fetchall()
            for row in rows:
                if row[0]:
                    features_by_type[row[0]] = row[1]
    except Exception as e:
        print(f"Error fetching features: {e}")

    return {
        "total_plots": total_plots,
        "plots_today": plots_today,
        "plots_week": plots_week,
        "plots_month": plots_month,
        "total_features": total_features,
        "features_by_type": features_by_type,
        "generated_at": now.isoformat()
    }


@router.get("/plots/daily")
def get_daily_plot_counts(db: Session = Depends(get_db), days: int = 30):
    """Get daily plot creation counts for the last N days."""

    result = []
    now = datetime.now()

    # Check if created_at column exists
    has_created_at = True
    try:
        db.execute(text("SELECT created_at FROM plots LIMIT 1")).fetchone()
    except Exception:
        has_created_at = False

    for i in range(days):
        day = now - timedelta(days=i)
        day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)

        count = 0
        if has_created_at:
            try:
                count = db.execute(
                    text("SELECT COUNT(*) FROM plots WHERE created_at >= :start AND created_at < :end"),
                    {"start": day_start, "end": day_end}
                ).scalar() or 0
            except Exception:
                pass

        result.append({
            "date": day_start.strftime("%Y-%m-%d"),
            "count": count
        })

    return list(reversed(result))


@router.get("/feedback")
def get_feedback_summary(db: Session = Depends(get_db)):
    """Get feedback summary for the feedback dashboard."""

    # Check if feedback table exists
    try:
        total_feedback = db.execute(text("SELECT COUNT(*) FROM feedback")).scalar() or 0

        # Profession breakdown
        professions = {}
        rows = db.execute(text("""
            SELECT profession, COUNT(*) as count
            FROM feedback
            GROUP BY profession
            ORDER BY count DESC
        """)).fetchall()
        for row in rows:
            professions[row[0]] = row[1]

        # Average satisfaction
        avg_satisfaction = db.execute(
            text("SELECT AVG(satisfaction) FROM feedback")
        ).scalar() or 0

        # Willingness to pay
        willing_to_pay = {}
        rows = db.execute(text("""
            SELECT willing_to_pay, COUNT(*) as count
            FROM feedback
            GROUP BY willing_to_pay
        """)).fetchall()
        for row in rows:
            willing_to_pay[row[0]] = row[1]

        return {
            "total_feedback": total_feedback,
            "professions": professions,
            "avg_satisfaction": round(float(avg_satisfaction), 2) if avg_satisfaction else 0,
            "willing_to_pay": willing_to_pay
        }
    except Exception:
        # Table doesn't exist yet
        return {
            "total_feedback": 0,
            "professions": {},
            "avg_satisfaction": 0,
            "willing_to_pay": {}
        }


@router.get("/plots/details")
def get_plot_details(db: Session = Depends(get_db)):
    """Get full plot details for admin view."""
    ensure_plots_created_at(db)
    ensure_plot_meta_table(db)
    ensure_plot_export_jobs_table(db)

    # Check if created_at exists on plots table
    has_created_at = True
    try:
        db.execute(text("SELECT created_at FROM plots LIMIT 1")).fetchone()
    except Exception:
        has_created_at = False

    created_at_col = "p.created_at" if has_created_at else "NULL"

    rows = []
    try:
        rows = db.execute(text(f"""
            SELECT
                p.id AS plot_id,
                ST_AsGeoJSON(p.geom) AS geojson,
                {created_at_col} AS created_at,
                m.title_text,
                m.location_text,
                m.lga_text,
                m.state_text,
                m.surveyor_name,
                m.surveyor_rank,
                m.scale_text,
                m.paper_size,
                m.coordinate_system,
                m.template_name,
                m.parent_plot_id,
                m.subdivision_batch_id,
                m.subdivision_lot_no,
                m.estate_name,
                m.created_at AS meta_created_at,
                m.updated_at AS meta_updated_at
            FROM plots p
            LEFT JOIN plot_meta m ON m.plot_id = p.id
            ORDER BY p.id DESC
        """)).mappings().all()
    except Exception as e:
        print(f"Plot details query failed: {e}")
        # Fallback without plot_meta join
        try:
            rows = db.execute(text(f"""
                SELECT
                    p.id AS plot_id,
                    NULL AS geojson,
                    {created_at_col} AS created_at,
                    NULL AS title_text,
                    NULL AS location_text,
                    NULL AS lga_text,
                    NULL AS state_text,
                    NULL AS surveyor_name,
                    NULL AS surveyor_rank,
                    NULL AS scale_text,
                    NULL AS paper_size,
                    NULL AS coordinate_system,
                    NULL AS template_name,
                    NULL AS parent_plot_id,
                    NULL AS subdivision_batch_id,
                    NULL AS subdivision_lot_no,
                    NULL AS estate_name,
                    NULL AS meta_created_at,
                    NULL AS meta_updated_at
                FROM plots p
                ORDER BY p.id DESC
            """)).mappings().all()
        except Exception as inner_e:
            print(f"Fallback plot query failed: {inner_e}")
            rows = []

    # Preload detected features if table exists
    features_by_plot = {}
    try:
        table_check = db.execute(text("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_name = 'detected_features'
            )
        """)).scalar()

        if table_check:
            feature_rows = db.execute(text("""
                SELECT plot_id, feature_type, location, COUNT(*) as count
                FROM detected_features
                GROUP BY plot_id, feature_type, location
            """)).fetchall()

            for plot_id, feature_type, location, count in feature_rows:
                features_by_plot.setdefault(plot_id, {"inside": {}, "buffer": {}})
                features_by_plot[plot_id][location][feature_type] = count
    except Exception:
        pass

    export_summary_by_plot = {}
    plot_ids = [int(row["plot_id"]) for row in rows if row.get("plot_id") is not None]
    if plot_ids:
        try:
            summary_stmt = text("""
                SELECT
                    plot_id,
                    COUNT(*) AS total_jobs,
                    COUNT(*) FILTER (WHERE status = 'completed') AS completed_jobs,
                    COUNT(*) FILTER (WHERE status = 'failed') AS failed_jobs,
                    COUNT(*) FILTER (WHERE status = 'queued') AS queued_jobs,
                    COUNT(*) FILTER (WHERE status = 'running') AS running_jobs,
                    MAX(COALESCE(completed_at, updated_at, created_at)) AS last_export_at,
                    ARRAY_REMOVE(ARRAY_AGG(DISTINCT export_type), NULL) AS export_types
                FROM plot_export_jobs
                WHERE plot_id IN :plot_ids
                GROUP BY plot_id
            """).bindparams(bindparam("plot_ids", expanding=True))
            summary_rows = db.execute(summary_stmt, {"plot_ids": plot_ids}).mappings().all()
            for row in summary_rows:
                export_summary_by_plot[int(row["plot_id"])] = {
                    "total_jobs": int(row["total_jobs"] or 0),
                    "completed_jobs": int(row["completed_jobs"] or 0),
                    "failed_jobs": int(row["failed_jobs"] or 0),
                    "queued_jobs": int(row["queued_jobs"] or 0),
                    "running_jobs": int(row["running_jobs"] or 0),
                    "last_export_at": row["last_export_at"].isoformat() if row.get("last_export_at") else None,
                    "export_types": list(row.get("export_types") or []),
                }

            latest_stmt = text("""
                SELECT DISTINCT ON (plot_id)
                    plot_id,
                    export_type,
                    status,
                    COALESCE(completed_at, updated_at, created_at) AS activity_at
                FROM plot_export_jobs
                WHERE plot_id IN :plot_ids
                ORDER BY plot_id,
                         COALESCE(completed_at, updated_at, created_at) DESC,
                         updated_at DESC NULLS LAST,
                         created_at DESC NULLS LAST
            """).bindparams(bindparam("plot_ids", expanding=True))
            latest_rows = db.execute(latest_stmt, {"plot_ids": plot_ids}).mappings().all()
            for row in latest_rows:
                plot_id = int(row["plot_id"])
                export_summary = export_summary_by_plot.setdefault(
                    plot_id,
                    {
                        "total_jobs": 0,
                        "completed_jobs": 0,
                        "failed_jobs": 0,
                        "queued_jobs": 0,
                        "running_jobs": 0,
                        "last_export_at": row["activity_at"].isoformat() if row.get("activity_at") else None,
                        "export_types": [],
                    },
                )
                export_summary["last_export_type"] = row.get("export_type")
                export_summary["last_export_status"] = row.get("status")
                if row.get("activity_at"):
                    export_summary["last_export_at"] = row["activity_at"].isoformat()
        except Exception as exc:
            print(f"Failed to load plot export summaries: {exc}")

    plot_list = []
    for row in rows:
        geojson = None
        if row.get("geojson"):
            if isinstance(row["geojson"], dict):
                geojson = row["geojson"]
            else:
                try:
                    geojson = json.loads(row["geojson"])
                except Exception:
                    geojson = None
        coords = []
        if geojson and geojson.get("type") == "Polygon":
            rings = geojson.get("coordinates", [])
            coords = rings[0] if rings else []

        plot_id = int(row["plot_id"])
        created_at = row.get("created_at") or row.get("meta_created_at")
        export_summary = export_summary_by_plot.get(
            plot_id,
            {
                "total_jobs": 0,
                "completed_jobs": 0,
                "failed_jobs": 0,
                "queued_jobs": 0,
                "running_jobs": 0,
                "last_export_type": None,
                "last_export_status": None,
                "last_export_at": None,
                "export_types": [],
            },
        )

        plot_list.append({
            "plot_id": plot_id,
            "created_at": created_at.isoformat() if created_at else None,
            "title_text": row.get("title_text"),
            "location_text": row.get("location_text"),
            "lga_text": row.get("lga_text"),
            "state_text": row.get("state_text"),
            "surveyor_name": row.get("surveyor_name"),
            "surveyor_rank": row.get("surveyor_rank"),
            "scale_text": row.get("scale_text"),
            "paper_size": row.get("paper_size"),
            "coordinate_system": row.get("coordinate_system"),
            "template_name": row.get("template_name"),
            "parent_plot_id": row.get("parent_plot_id"),
            "subdivision_batch_id": row.get("subdivision_batch_id"),
            "subdivision_lot_no": row.get("subdivision_lot_no"),
            "estate_name": row.get("estate_name"),
            "workflow_type": "subdivision" if row.get("parent_plot_id") else "survey_plan",
            "geometry": geojson,
            "coords": coords,
            "detected_features": features_by_plot.get(plot_id, {"inside": {}, "buffer": {}}),
            "reports_generated": build_report_flags(plot_id),
            "meta_created_at": row["meta_created_at"].isoformat() if row.get("meta_created_at") else None,
            "meta_updated_at": row["meta_updated_at"].isoformat() if row.get("meta_updated_at") else None,
            "export_summary": export_summary,
        })

    return plot_list


@router.get("/survey-users")
def get_survey_users(request: Request, db: Session = Depends(get_db)):
    """Registered LandCheck Survey users (for the admin dashboard). Unlike the other endpoints in
    this file, this one enforces a real server-side auth check - it returns user emails, which
    unlike plot counts/analytics is real PII, so unauthenticated access isn't acceptable here."""
    require_super_admin_request(db, request)
    ensure_survey_auth_schema()
    ensure_plot_meta_table(db)

    # Plots nested per user (id/title/location/template/date/raw input coordinates) so the admin
    # dashboard can offer a "redownload" action per plot, and audit exactly what the surveyor
    # typed in, without a second round-trip. survey_input_coordinates/coordinate_system are stored
    # verbatim from the CoordinateInput form (see create_plot/update_plot_meta in plots.py, which
    # just json.dumps() whatever list the client sent) - never reprojected or normalized in
    # storage, so this is the exact station/coordinate-system pairing the user entered, not a
    # converted/derived value.
    rows = db.execute(text("""
        SELECT
            u.id,
            u.email,
            u.full_name,
            u.created_at,
            u.last_login_at,
            COUNT(p.id) AS plot_count,
            COALESCE(
                json_agg(
                    json_build_object(
                        'plot_id', p.id,
                        'title_text', pm.title_text,
                        'location_text', pm.location_text,
                        'template_name', pm.template_name,
                        'created_at', p.created_at,
                        'coordinate_system', pm.coordinate_system,
                        'survey_input_coordinates', pm.survey_input_coordinates,
                        'parent_plot_id', pm.parent_plot_id,
                        'estate_name', pm.estate_name
                    ) ORDER BY p.created_at DESC
                ) FILTER (WHERE p.id IS NOT NULL),
                '[]'
            ) AS plots
        FROM survey_users u
        LEFT JOIN plots p ON p.owner_user_id = u.id
        LEFT JOIN plot_meta pm ON pm.plot_id = p.id
        GROUP BY u.id
        ORDER BY u.created_at DESC
    """)).mappings().all()

    return [
        {
            "id": int(row["id"]),
            "email": row["email"],
            "full_name": row["full_name"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "last_login_at": row["last_login_at"].isoformat() if row["last_login_at"] else None,
            "plot_count": int(row["plot_count"] or 0),
            "plots": [
                {
                    "plot_id": int(plot["plot_id"]),
                    "title_text": plot.get("title_text"),
                    "location_text": plot.get("location_text"),
                    "template_name": plot.get("template_name"),
                    "created_at": plot.get("created_at"),
                    "coordinate_system": plot.get("coordinate_system"),
                    "survey_input_coordinates": plot.get("survey_input_coordinates") or [],
                    "parent_plot_id": plot.get("parent_plot_id"),
                    "estate_name": plot.get("estate_name"),
                    "workflow_type": "subdivision" if plot.get("parent_plot_id") else "survey_plan",
                }
                for plot in (row["plots"] or [])
            ],
        }
        for row in rows
    ]


@router.get("/georeference-sessions")
def get_georeference_sessions(request: Request, db: Session = Depends(get_db)):
    """Georeference sessions (for the admin dashboard). Unlike Survey Plan/Subdivision plots,
    these live in a standalone `survey_georeference_sessions` table with no owner_user_id or
    plot_id linkage at all (see survey_georeference.py) - so unlike get_survey_users above, this
    can't be grouped per-user, it's just every session that exists, newest first. Same real
    server-side auth check as get_survey_users, since title_text/source_file_name can carry the
    same kind of identifying info."""
    require_super_admin_request(db, request)

    table_exists = db.execute(text("""
        SELECT EXISTS (
            SELECT FROM information_schema.tables
            WHERE table_name = 'survey_georeference_sessions'
        )
    """)).scalar()
    if not table_exists:
        return []

    rows = db.execute(text("""
        SELECT
            id, title_text, status, target_coordinate_system, target_epsg,
            source_file_name, source_content_type, created_at, updated_at, finalized_at
        FROM survey_georeference_sessions
        ORDER BY created_at DESC
    """)).mappings().all()

    return [
        {
            "id": str(row["id"]),
            "title_text": row.get("title_text"),
            "status": row.get("status"),
            "target_coordinate_system": row.get("target_coordinate_system"),
            "target_epsg": row.get("target_epsg"),
            "source_file_name": row.get("source_file_name"),
            "source_content_type": row.get("source_content_type"),
            "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
            "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
            "finalized_at": row["finalized_at"].isoformat() if row.get("finalized_at") else None,
        }
        for row in rows
    ]
