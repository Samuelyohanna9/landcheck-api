from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

# Same free-tier-friendly setup as plan_reader.py / field_to_finish.py / tree_health_ai.py /
# green_impact_narrative.py - independent, optional API key so this feature's quota/billing isn't
# shared with, or taken down by, the other AI features.
GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

# Real launch cap, matching every other AI feature's per-user daily limit (Plan Reader,
# Field-to-Finish, Tree Health, Impact Narrative) - see each module's own constant.
AI_DIGITIZE_DAILY_LIMIT = 2


def _gemini_api_key() -> Optional[str]:
    dedicated = str(os.getenv("GEMINI_GEOREFERENCE_DIGITIZE_API_KEY") or "").strip()
    if dedicated:
        return dedicated
    plan_reader_key = str(os.getenv("GEMINI_PLAN_READER_API_KEY") or "").strip()
    if plan_reader_key:
        return plan_reader_key
    return str(os.getenv("GEMINI_API_KEY") or "").strip() or None


def _gemini_model() -> str:
    return str(os.getenv("GEMINI_GEOREFERENCE_DIGITIZE_MODEL") or "gemini-3.6-flash").strip() or "gemini-3.6-flash"


class GeoreferenceAiDigitizeError(Exception):
    """Raised for anything that should surface as a clear message in the UI, as opposed to an
    unexpected crash - the router turns this into a 502 with the message intact."""


_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "points": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "label": {
                        "type": "STRING", "nullable": True,
                        "description": "The station/beacon label printed next to this point, if legible, e.g. 'PB1', 'A'.",
                    },
                    "feature_type": {
                        "type": "STRING",
                        "enum": ["boundary_corner", "stake", "other"],
                        "description": (
                            "'boundary_corner' for a point on the parcel's own boundary outline; "
                            "'stake' for a separate survey/reference point marked on the drawing but "
                            "not part of the boundary ring; 'other' for anything else worth flagging."
                        ),
                    },
                    "x_frac": {"type": "NUMBER", "description": "0 to 1 - fraction of the image width from the left edge."},
                    "y_frac": {"type": "NUMBER", "description": "0 to 1 - fraction of the image height from the top edge."},
                    "confidence": {"type": "NUMBER", "description": "0 to 1 - how confident you are in this point's visual position."},
                },
                "required": ["feature_type", "x_frac", "y_frac", "confidence"],
            },
        },
        "notes": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": "Anything ambiguous, illegible, or unusual a human should double check.",
        },
    },
    "required": ["points"],
}

_PROMPT = """You are looking at a scanned or photographed Nigerian land survey plan that has \
already been georeferenced (anchored to real-world coordinates by a human surveyor using control \
points) - your only job is to visually locate marker points on the drawing, NOT to read or \
transcribe any printed coordinate numbers.

Find every boundary-corner marker: a small circle, cross, dot, or similar symbol sitting at a \
corner of the plotted parcel boundary line, often labelled with a station name or beacon number \
next to it (e.g. "PB1", "A", "SC/AK/L 72723"). List these in "boundary_corner" order as they \
appear going around the boundary (clockwise or counterclockwise, whichever matches the traverse \
drawn) - this traversal order matters as much as the positions themselves.

Also find any separate stake or reference points marked on the drawing that are clearly NOT part \
of the parcel's own boundary outline (e.g. a beacon referencing an adjoining road or a nearby \
survey control point) and list those with feature_type "stake". Anything else worth flagging (an \
illegible label, an ambiguous symbol) goes in "other" or in extraction_notes - never invent a \
point you cannot actually see marked on the drawing.

For every point, give its position as a FRACTION of the image's total width and height (0 to 1, \
measured from the top-left corner) - not pixel counts, not printed coordinates.

Return ONLY the structured JSON described by the response schema - no prose."""


def detect_digitized_points(image_bytes: bytes, content_type: str) -> Dict[str, Any]:
    """Sends a single georeferenced survey-plan raster to Gemini and returns its visual reading of
    every boundary-corner/stake marker on the drawing, as fractional image coordinates - never
    pixel or real-world coordinates, which the caller derives afterwards from the session's known
    image dimensions and already-solved transform. Mirrors plan_reader.py's extract_survey_plan
    call shape/retry logic exactly.
    """
    api_key = _gemini_api_key()
    if not api_key:
        raise GeoreferenceAiDigitizeError("AI digitizing isn't configured yet (GEMINI_API_KEY is not set on the server).")

    request_body = {
        "contents": [{
            "parts": [
                {"inline_data": {"mime_type": content_type, "data": base64.b64encode(image_bytes).decode("ascii")}},
                {"text": _PROMPT},
            ],
        }],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
            "responseSchema": _RESPONSE_SCHEMA,
        },
    }
    url = f"{GEMINI_API_BASE_URL}/models/{_gemini_model()}:generateContent"

    # 45s read timeout, 2 attempts - see plan_reader.py's identical retry block for why. Worst case
    # ~91s - the frontend's request timeout must stay comfortably above that.
    max_attempts = 2
    last_error: Optional[str] = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.post(url, params={"key": api_key}, json=request_body, timeout=(6, 45))
        except requests.RequestException as exc:
            last_error = f"Could not reach the AI service ({exc})."
            logger.warning("Georeference AI digitize call failed to reach the API (attempt %s/%s): %s", attempt, max_attempts, exc)
            time.sleep(1.0)
            continue
        if response.status_code == 429:
            last_error = "The AI service is rate-limited right now - please try again shortly."
            retry_after_header = str(response.headers.get("Retry-After") or "").strip()
            wait_seconds = float(retry_after_header) if retry_after_header.replace(".", "", 1).isdigit() else 2.0 * attempt
            logger.warning(
                "Georeference AI digitize call hit 429 (attempt %s/%s) - waiting %.1fs. Body: %s",
                attempt, max_attempts, min(wait_seconds, 8.0), response.text[:300],
            )
            time.sleep(min(wait_seconds, 8.0))
            continue
        if not response.ok:
            raise GeoreferenceAiDigitizeError(f"AI digitizing failed ({response.status_code}): {response.text[:300]}")
        try:
            data = response.json()
        except ValueError as exc:
            raise GeoreferenceAiDigitizeError("The AI service returned an unreadable response.") from exc
        candidates = data.get("candidates") or []
        if not candidates:
            block_reason = (data.get("promptFeedback") or {}).get("blockReason")
            raise GeoreferenceAiDigitizeError(f"The AI service returned no result{f' ({block_reason})' if block_reason else ''}.")
        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        text = "".join(str(p.get("text") or "") for p in parts).strip()
        if not text:
            raise GeoreferenceAiDigitizeError("The AI service returned an empty result.")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise GeoreferenceAiDigitizeError("The AI service's response wasn't valid structured data.") from exc
        return parsed
    raise GeoreferenceAiDigitizeError(last_error or "AI digitizing failed after multiple attempts.")
