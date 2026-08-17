# app/routers/feedback.py

from fastapi import APIRouter, Depends, Body, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import text
from datetime import datetime

from app.db import SessionLocal
from app.utils.survey_auth_security import require_survey_session

router = APIRouter(prefix="/feedback", tags=["feedback"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Ensure feedback table exists
def ensure_feedback_table(db: Session):
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS feedback (
            id SERIAL PRIMARY KEY,
            profession VARCHAR(100),
            experience VARCHAR(50),
            useful_features TEXT,
            problems TEXT,
            feature_requests TEXT,
            willing_to_pay VARCHAR(100),
            satisfaction INTEGER,
            email VARCHAR(255),
            created_at TIMESTAMP DEFAULT NOW()
        )
    """))
    db.commit()


@router.post("")
def submit_feedback(
    profession: str = Body(""),
    experience: str = Body(""),
    usefulFeatures: list[str] = Body(default=[]),
    problems: str = Body(""),
    featureRequests: str = Body(""),
    willingToPay: str = Body(""),
    satisfaction: int = Body(0),
    email: str = Body(""),
    db: Session = Depends(get_db)
):
    """Submit user feedback."""

    # Ensure table exists
    ensure_feedback_table(db)

    # Convert list to comma-separated string
    useful_features_str = ", ".join(usefulFeatures) if usefulFeatures else ""

    # Insert feedback
    db.execute(text("""
        INSERT INTO feedback (profession, experience, useful_features, problems, feature_requests, willing_to_pay, satisfaction, email)
        VALUES (:profession, :experience, :useful_features, :problems, :feature_requests, :willing_to_pay, :satisfaction, :email)
    """), {
        "profession": profession,
        "experience": experience,
        "useful_features": useful_features_str,
        "problems": problems,
        "feature_requests": featureRequests,
        "willing_to_pay": willingToPay,
        "satisfaction": satisfaction,
        "email": email
    })
    db.commit()

    return {"status": "success", "message": "Feedback submitted successfully"}


def ensure_support_messages_table(db: Session):
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS survey_support_messages (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            email VARCHAR(255),
            full_name VARCHAR(255),
            subject VARCHAR(255),
            message TEXT NOT NULL,
            page_context VARCHAR(100),
            created_at TIMESTAMP DEFAULT NOW()
        )
    """))
    db.commit()


@router.post("/support")
def submit_support_message(
    request: Request,
    subject: str = Body(""),
    message: str = Body(...),
    page_context: str = Body(""),
    db: Session = Depends(get_db),
):
    """Dashboard's "Help / Support" widget - unlike the profession/satisfaction survey above,
    this is a quick "I have a question or a problem" note tied to the signed-in surveyor's
    account, so we already know who it's from without asking them to type their email again."""
    session = require_survey_session(db, request)
    clean_message = message.strip()
    if not clean_message:
        raise HTTPException(status_code=400, detail="Message can't be empty.")

    ensure_support_messages_table(db)
    db.execute(text("""
        INSERT INTO survey_support_messages (user_id, email, full_name, subject, message, page_context)
        VALUES (:user_id, :email, :full_name, :subject, :message, :page_context)
    """), {
        "user_id": session.user_id,
        "email": session.email,
        "full_name": session.full_name,
        "subject": subject.strip() or "(no subject)",
        "message": clean_message,
        "page_context": page_context.strip() or "dashboard",
    })
    db.commit()

    try:
        from app.utils.survey_email import send_support_message_email
        send_support_message_email(
            subject=subject.strip() or "(no subject)",
            message=clean_message,
            from_email=session.email or "",
            from_name=session.full_name or "A LandCheck Survey user",
            page_context=page_context.strip() or "dashboard",
        )
    except Exception:
        # The message is already saved above - a missing/misconfigured SMTP_HOST shouldn't ever
        # surface as a failure to the user submitting the request.
        pass

    return {"status": "success"}


@router.get("")
def get_all_feedback(db: Session = Depends(get_db)):
    """Get all feedback entries (for admin)."""

    try:
        rows = db.execute(text("""
            SELECT id, profession, experience, useful_features, problems, feature_requests,
                   willing_to_pay, satisfaction, email, created_at
            FROM feedback
            ORDER BY created_at DESC
        """)).fetchall()

        feedback_list = []
        for row in rows:
            feedback_list.append({
                "id": row[0],
                "profession": row[1],
                "experience": row[2],
                "useful_features": row[3],
                "problems": row[4],
                "feature_requests": row[5],
                "willing_to_pay": row[6],
                "satisfaction": row[7],
                "email": row[8],
                "created_at": row[9].isoformat() if row[9] else None
            })

        return feedback_list
    except Exception:
        return []
