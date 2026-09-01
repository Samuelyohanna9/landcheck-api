from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

# Same free-tier-friendly setup as plan_reader.py / tree_health_ai.py / green_impact_narrative.py -
# independent, optional API key so this feature's quota/billing isn't shared with, or taken down
# by, the other AI features.
GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

MAX_UPLOAD_BYTES = 2 * 1024 * 1024  # 2 MB - a raw text coordinate file is small; this is generous
# Temporarily raised for the AI Field-to-Survey-Plan flow testing pass - restore to a real daily
# cap (e.g. 3) before this ships, same as the Plan Reader's DAILY_READING_LIMIT.
DAILY_IMPORT_LIMIT = 1000

# Feature-code categories this app recognizes today. This is a NEW vocabulary (nothing in the
# codebase classified points beyond boundary-vs-spot-height before this) - deliberately small and
# generic rather than an exhaustive surveying code table, since the near-term use is showing the
# surveyor what LandCheck understood, not yet driving map symbology (no renderer for these
# categories exists yet - see this module's docstring note in field_to_finish router).
POINT_CATEGORIES = [
    "boundary", "spot_height", "tree", "electric_pole", "drain",
    "building_corner", "fence", "road_edge", "other",
]


def _gemini_api_key() -> Optional[str]:
    dedicated = str(os.getenv("GEMINI_FIELD_TO_FINISH_API_KEY") or "").strip()
    if dedicated:
        return dedicated
    plan_reader_key = str(os.getenv("GEMINI_PLAN_READER_API_KEY") or "").strip()
    if plan_reader_key:
        return plan_reader_key
    return str(os.getenv("GEMINI_API_KEY") or "").strip() or None


def _gemini_model() -> str:
    return str(os.getenv("GEMINI_FIELD_TO_FINISH_MODEL") or "gemini-3.6-flash").strip() or "gemini-3.6-flash"


class FieldToFinishError(Exception):
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
                    "point_number": {"type": "STRING", "nullable": True},
                    "x": {"type": "NUMBER", "description": "Easting if projected, otherwise longitude - as printed, never converted"},
                    "y": {"type": "NUMBER", "description": "Northing if projected, otherwise latitude - as printed, never converted"},
                    "elevation_m": {"type": "NUMBER", "nullable": True},
                    "feature_code_raw": {
                        "type": "STRING", "nullable": True,
                        "description": "The exact feature/point code column value as printed, e.g. 'EP', 'TR', 'DR' - null if this row has no code column.",
                    },
                    "category": {"type": "STRING", "enum": POINT_CATEGORIES},
                    "description": {"type": "STRING", "nullable": True},
                    "confidence": {"type": "NUMBER", "description": "0 to 1 - how confident you are in this row's column interpretation and category"},
                },
                "required": ["x", "y", "category", "confidence"],
            },
        },
        "column_mapping_summary": {
            "type": "STRING",
            "description": "One or two plain-language sentences describing which raw column you interpreted as which field, e.g. 'Column 1 is the point number, column 2 is easting, column 3 is northing, column 4 is elevation, column 5 is the feature code.'",
        },
        "coordinate_system_guess": {
            "type": "STRING",
            "enum": ["wgs84", "minna_31", "minna_32", "minna_33", "utm_31n", "utm_32n", "utm_33n", "unknown"],
            "description": "Guessed from coordinate magnitude/format (small decimal degrees ~2-15 range => wgs84; large 6-7 digit metre values => a projected system) and any explicit datum/zone label in the file. Use 'unknown' rather than guessing a specific zone with no real evidence.",
        },
        "coordinate_system_evidence": {
            "type": "STRING", "nullable": True,
            "description": "Short reason for the coordinate_system_guess, e.g. 'Easting/Northing values in the 300000-800000 range, typical of Minna/UTM metres' or an explicit zone label found in the file.",
        },
        "extraction_notes": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": "Anything ambiguous, inconsistent, or unusual a human should double check - e.g. rows that didn't match the pattern of the rest of the file.",
        },
    },
    "required": ["points", "column_mapping_summary", "coordinate_system_guess"],
}

_PROMPT_TEMPLATE = """You are cleaning up a raw coordinate data file exported from a GNSS receiver, \
total station, or similar field survey instrument, for a Nigerian land surveyor. These files often \
have NO real header row, or an inconsistent one, and columns in varying order. A typical row looks \
like: "P001  329110.22  1028183.41  212.3  EP" (point number, easting, northing, elevation, feature \
code) or "P002,329119.61,1028191.32,211.8,TR,large mango tree" (with an added free-text description).

Work out what each column actually represents from the DATA itself (magnitude/format), not just \
column position, since different instruments export in different orders. Extract every row as one \
point. Never invent a value you cannot actually read; if a column is genuinely ambiguous, say so in \
extraction_notes rather than guessing silently.

Classify each point's category using its feature code (if present) and any description text, from \
exactly these categories: boundary, spot_height, tree, electric_pole, drain, building_corner, fence, \
road_edge, other. Common Nigerian/international field-code conventions: EP/POLE -> electric_pole, \
TR/TREE -> tree, DR/DRAIN -> drain, BLD/BC/BLDG -> building_corner, FN/FENCE -> fence, RE/EDGE -> \
road_edge, BP/PB/BDY -> boundary. A point with NO feature code and no description is almost always \
spot_height (a plain elevation sample), not boundary - only mark a point "boundary" when its code or \
description actually says so (e.g. "BP1", "BDY", "boundary peg"), since most raw field files are \
mostly ordinary elevation/detail shots, not the boundary corners themselves.

Also determine the coordinate system from the x/y magnitudes across the whole file: values roughly \
in the 2-15 range for x and 4-15 for y are WGS84 latitude/longitude (degrees); values in the tens or \
hundreds of thousands to low millions are a projected Easting/Northing in metres, on the Minna Datum \
or WGS84 UTM, zone 31N, 32N, or 33N (Nigeria spans all three - zone boundaries at 6 deg E and 12 deg \
E). Prefer an explicit datum/zone label in the file over guessing from magnitude alone, and quote it \
in coordinate_system_evidence. Use "unknown" if there's genuinely no basis to tell.

Raw file content:
{raw_text}

Return ONLY the structured JSON described by the response schema - no prose."""


def parse_field_data(raw_text: str) -> Dict[str, Any]:
    """Sends the raw text content of an uploaded field-data file to Gemini (text-only, no vision -
    this is already plain text/CSV, not a scan) and returns its cleaned, classified reading.
    Mirrors plan_reader.py's extract_survey_plan call shape/retry logic exactly.
    """
    api_key = _gemini_api_key()
    if not api_key:
        raise FieldToFinishError("AI field data import isn't configured yet (GEMINI_API_KEY is not set on the server).")

    prompt = _PROMPT_TEMPLATE.format(raw_text=raw_text[:20000])
    request_body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
            "responseSchema": _RESPONSE_SCHEMA,
        },
    }
    url = f"{GEMINI_API_BASE_URL}/models/{_gemini_model()}:generateContent"

    max_attempts = 3
    last_error: Optional[str] = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.post(url, params={"key": api_key}, json=request_body, timeout=(6, 25))
        except requests.RequestException as exc:
            last_error = f"Could not reach the AI service ({exc})."
            logger.warning("Field-to-finish Gemini call failed to reach the API (attempt %s/%s): %s", attempt, max_attempts, exc)
            time.sleep(1.0)
            continue
        if response.status_code == 429:
            last_error = "The AI service is rate-limited right now - please try again shortly."
            retry_after_header = str(response.headers.get("Retry-After") or "").strip()
            wait_seconds = float(retry_after_header) if retry_after_header.replace(".", "", 1).isdigit() else 2.0 * attempt
            logger.warning(
                "Field-to-finish Gemini call hit 429 (attempt %s/%s) - waiting %.1fs. Body: %s",
                attempt, max_attempts, min(wait_seconds, 8.0), response.text[:300],
            )
            time.sleep(min(wait_seconds, 8.0))
            continue
        if not response.ok:
            raise FieldToFinishError(f"AI field data import failed ({response.status_code}): {response.text[:300]}")
        try:
            data = response.json()
        except ValueError as exc:
            raise FieldToFinishError("The AI service returned an unreadable response.") from exc
        candidates = data.get("candidates") or []
        if not candidates:
            block_reason = (data.get("promptFeedback") or {}).get("blockReason")
            raise FieldToFinishError(f"The AI service returned no result{f' ({block_reason})' if block_reason else ''}.")
        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        text = "".join(str(p.get("text") or "") for p in parts).strip()
        if not text:
            raise FieldToFinishError("The AI service returned an empty result.")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise FieldToFinishError("The AI service's response wasn't valid structured data.") from exc
        return parsed
    raise FieldToFinishError(last_error or "AI field data import failed after multiple attempts.")
