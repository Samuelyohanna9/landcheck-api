from __future__ import annotations

import logging

from fastapi import APIRouter, File, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

from app.utils.plan_reader import (
    ALLOWED_CONTENT_TYPES,
    MAX_UPLOAD_BYTES,
    PlanReaderError,
    check_survey_plan,
    extract_survey_plan,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/plan-reader", tags=["plan-reader"])


@router.post("/extract")
async def plan_reader_extract(file: UploadFile = File(...)):
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
    return {"extracted": extracted, "checks": checks}
