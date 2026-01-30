# app/routers/analytics.py

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import text, func
from datetime import datetime, timedelta

from app.db import SessionLocal

router = APIRouter(prefix="/analytics", tags=["analytics"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


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
