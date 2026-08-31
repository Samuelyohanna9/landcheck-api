from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.routers.plots import get_db
from app.utils.plan_reader import (
    ALLOWED_CONTENT_TYPES,
    DAILY_READING_LIMIT,
    MAX_UPLOAD_BYTES,
    PlanReaderError,
    check_survey_plan,
    consume_daily_reading,
    extract_survey_plan,
)
from app.utils.survey_auth_security import resolve_survey_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/plan-reader", tags=["plan-reader"])


def _rate_limit_identity(request: Request, db: Session) -> str:
    """Signed-in surveyor -> their user id (stable across devices/IPs); otherwise the caller's IP.
    IP-based fallback is a soft limit (shared NATs/proxies can undercount distinct real users, and
    it resets if someone changes network) - fine for its actual job here, protecting a shared,
    genuinely small daily AI budget from one enthusiastic user, not a security control.
    """
    try:
        survey_session = resolve_survey_session(db, request)
    except Exception:
        survey_session = None
    if survey_session:
        return f"user:{survey_session.user_id}"
    client_host = request.client.host if request.client else "unknown"
    return f"ip:{client_host}"


@router.post("/extract")
async def plan_reader_extract(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Reads an uploaded scan/photo of an existing survey plan and returns the AI's structured
    reading plus a rule-based sanity check on it - see app/utils/plan_reader.py's module docstring
    for why extraction and checking are deliberately separate (checking must be exact/reproducible
    math, not a second AI call). Nothing here is persisted - the frontend treats the response as a
    pre-fill for the normal coordinate-entry/plan-metadata flow, which the surveyor still reviews
    and confirms before anything is saved.
    """
    content_type = str(file.content_type or "").strip().lower()
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="Upload a JPEG, PNG, WEBP, or PDF of the survey plan.")

    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail=f"File exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit.")

    # Quota is consumed here, AFTER basic file validation (so an obviously wrong upload doesn't
    # burn one of the day's 3 reads) but BEFORE the actual Gemini call (so a blurry/unreadable photo
    # still counts - it consumed the same real API quota either way).
    identity_key = _rate_limit_identity(request, db)
    new_count = consume_daily_reading(db, identity_key, DAILY_READING_LIMIT)
    if new_count is None:
        raise HTTPException(
            status_code=429,
            detail=f"You've used all {DAILY_READING_LIMIT} AI plan readings for today. Please try again tomorrow.",
        )

    try:
        # extract_survey_plan is a synchronous, blocking call (requests.post + time.sleep retries)
        # - running it directly on this async route would block the whole event loop for the
        # entire Gemini round trip, stalling every other request the server is handling
        # concurrently, and risking a worker-timeout kill on a slow/retried call (which surfaces
        # to the client as a proxy 502, indistinguishable from a real network failure). Off-loading
        # it to a thread is the fix either way, not just a maybe.
        extracted = await run_in_threadpool(extract_survey_plan, payload, content_type)
    except PlanReaderError as exc:
        logger.warning("Plan reader extraction failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Plan reader extraction crashed")
        raise HTTPException(status_code=500, detail="Something went wrong reading this plan.") from exc

    checks = check_survey_plan(extracted)
    return {
        "extracted": extracted,
        "checks": checks,
        "readings_used_today": new_count,
        "readings_remaining_today": max(0, DAILY_READING_LIMIT - new_count),
    }
