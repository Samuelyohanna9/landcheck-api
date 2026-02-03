# app/routers/analytics.py

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import text, func
from datetime import datetime, timedelta
import json
import os
import glob

from app.db import SessionLocal

router = APIRouter(prefix="/analytics", tags=["analytics"])


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


def build_report_flags(plot_id: int):
    base_dir = os.path.join("app", "reports")
    orthophoto_dir = os.path.join(base_dir, "orthophoto")
    previews_dir = os.path.join(base_dir, "previews")
    dwg_dir = os.path.join(base_dir, "dwg")

    def exists(path: str) -> bool:
        return os.path.exists(path) and os.path.getsize(path) > 0

    orthophoto_preview_glob = glob.glob(
        os.path.join(orthophoto_dir, f"plot_{plot_id}_orthophoto_satellite_preview*.png")
    ) + glob.glob(
        os.path.join(orthophoto_dir, f"plot_{plot_id}_orthophoto_preview*.png")
    )
    topo_preview_glob = glob.glob(
        os.path.join(orthophoto_dir, f"plot_{plot_id}_orthophoto_topo_preview*.png")
    )

    return {
        "survey_plan_pdf": exists(os.path.join(base_dir, f"plot_{plot_id}_report.pdf")),
        "survey_plan_preview": exists(os.path.join(previews_dir, f"plot_{plot_id}_preview.png")),
        "orthophoto_pdf": (
            exists(os.path.join(orthophoto_dir, f"plot_{plot_id}_orthophoto_satellite.pdf"))
            or exists(os.path.join(orthophoto_dir, f"plot_{plot_id}_orthophoto.pdf"))
        ),
        "topo_map_pdf": exists(os.path.join(orthophoto_dir, f"plot_{plot_id}_orthophoto_topo.pdf")),
        "orthophoto_preview": len(orthophoto_preview_glob) > 0,
        "topo_map_preview": len(topo_preview_glob) > 0,
        "dwg": exists(os.path.join(dwg_dir, f"plot_{plot_id}_survey_plan.dxf")),
        "back_computation_pdf": exists(os.path.join(base_dir, f"plot_{plot_id}_back_computation.pdf")),
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
                p.id,
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
                m.created_at AS meta_created_at,
                m.updated_at AS meta_updated_at
            FROM plots p
            LEFT JOIN plot_meta m ON m.plot_id = p.id
            ORDER BY p.id DESC
        """)).fetchall()
    except Exception as e:
        print(f"Plot details query failed: {e}")
        # Fallback without plot_meta join
        try:
            rows = db.execute(text(f"""
                SELECT
                    p.id,
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
                    NULL AS meta_created_at,
                    NULL AS meta_updated_at
                FROM plots p
                ORDER BY p.id DESC
            """)).fetchall()
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

    plot_list = []
    for row in rows:
        geojson = None
        if row[1]:
            if isinstance(row[1], dict):
                geojson = row[1]
            else:
                try:
                    geojson = json.loads(row[1])
                except Exception:
                    geojson = None
        coords = []
        if geojson and geojson.get("type") == "Polygon":
            rings = geojson.get("coordinates", [])
            coords = rings[0] if rings else []

        created_at = row[2] or row[12]

        plot_list.append({
            "plot_id": row[0],
            "created_at": created_at.isoformat() if created_at else None,
            "title_text": row[3],
            "location_text": row[4],
            "lga_text": row[5],
            "state_text": row[6],
            "surveyor_name": row[7],
            "surveyor_rank": row[8],
            "scale_text": row[9],
            "paper_size": row[10],
            "coordinate_system": row[11],
            "geometry": geojson,
            "coords": coords,
            "detected_features": features_by_plot.get(row[0], {"inside": {}, "buffer": {}}),
            "reports_generated": build_report_flags(row[0]),
            "meta_updated_at": row[13].isoformat() if row[13] else None
        })

    return plot_list
