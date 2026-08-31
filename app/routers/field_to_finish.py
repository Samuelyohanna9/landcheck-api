from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.routers.plots import get_db
from app.utils.field_to_finish import (
    DAILY_IMPORT_LIMIT,
    MAX_UPLOAD_BYTES,
    FieldToFinishError,
    parse_field_data,
)
from app.utils.plan_reader import consume_daily_reading
from app.utils.survey_auth_security import resolve_survey_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/field-to-finish", tags=["field-to-finish"])

# Raw field-data files are already plain text, unlike Plan Reader's photos/scans - decode as text
# rather than requiring a specific content-type, since GNSS/total-station export utilities rarely
# set a consistent MIME type for their .txt/.csv/.dat exports.
_ALLOWED_EXTENSIONS = (".txt", ".csv", ".dat", ".asc", ".tsv")


def _rate_limit_identity(request: Request, db: Session) -> str:
    """Same identity resolution as plan_reader.py's _rate_limit_identity - signed-in surveyor's
    user id when available, otherwise the caller's IP as a soft per-machine limit."""
    try:
        survey_session = resolve_survey_session(db, request)
    except Exception:
        survey_session = None
    if survey_session:
        return f"fieldimport:user:{survey_session.user_id}"
    client_host = request.client.host if request.client else "unknown"
    return f"fieldimport:ip:{client_host}"


@router.post("/import")
async def field_to_finish_import(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Reads an uploaded raw GNSS/total-station export file and returns AI's best reading of each
    row (point number, coordinates, elevation, feature category) - the surveyor still reviews and
    confirms every row before anything is imported into their plot, same discipline as the Plan
    Reader. This never touches map rendering: classified points feed into the exact same boundary/
    spot-height coordinate list the manual CSV import already produces (see CoordinateInput.tsx) -
    category is shown to the surveyor as a helpful label, not yet drawn as a map symbol (no
    renderer for poles/trees/drains/etc. exists in this app yet).
    """
    filename = str(file.filename or "").strip().lower()
    if not filename.endswith(_ALLOWED_EXTENSIONS):
        raise HTTPException(status_code=400, detail="Upload a .txt, .csv, .dat, or .tsv raw coordinate export file.")

    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail=f"File exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit.")

    try:
        raw_text = payload.decode("utf-8", errors="replace")
    except Exception:
        raise HTTPException(status_code=400, detail="Could not read this file as text.")
    if not raw_text.strip():
        raise HTTPException(status_code=400, detail="The uploaded file has no readable text content.")

    identity_key = _rate_limit_identity(request, db)
    new_count = consume_daily_reading(db, identity_key, DAILY_IMPORT_LIMIT)
    if new_count is None:
        raise HTTPException(
            status_code=429,
            detail=f"You've used all {DAILY_IMPORT_LIMIT} AI field data imports for today. Please try again tomorrow.",
        )

    try:
        # Blocking requests.post call - same event-loop-blocking risk plan_reader.py's router
        # comment documents, so it's offloaded to a thread the same way.
        parsed = await run_in_threadpool(parse_field_data, raw_text)
    except FieldToFinishError as exc:
        logger.warning("Field-to-finish parse failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception:
        logger.exception("Field-to-finish parse crashed")
        raise HTTPException(status_code=500, detail="Something went wrong reading this file.") from exc

    return {
        "parsed": parsed,
        "imports_used_today": new_count,
        "imports_remaining_today": max(0, DAILY_IMPORT_LIMIT - new_count),
    }
