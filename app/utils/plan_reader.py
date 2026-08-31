from __future__ import annotations

import base64
import json
import logging
import math
import os
import time
from typing import Any, Dict, List, Optional

import requests
from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Per-identity daily cap on actual AI reads (not on file-validation failures, which never reach
# this far) - protects the shared Gemini quota (as few as 20 requests/day on a free-tier key, see
# the V3.1-style permanent-record discipline this session already applies elsewhere) from one
# enthusiastic user consuming the whole team's daily budget alone.
DAILY_READING_LIMIT = 3


def ensure_plan_reader_usage_schema(db: Session) -> None:
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS plan_reader_usage (
            id SERIAL PRIMARY KEY,
            identity_key TEXT NOT NULL,
            usage_date DATE NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            UNIQUE (identity_key, usage_date)
        )
    """))
    db.commit()


def consume_daily_reading(db: Session, identity_key: str, limit: int = DAILY_READING_LIMIT) -> Optional[int]:
    """Atomically checks-and-increments today's usage count for identity_key. Returns the new
    count if the caller is still within the daily limit, or None if they've already used it up.

    The UPDATE's own WHERE clause (not a separate SELECT-then-UPDATE) is what makes this safe
    under concurrent requests: once count reaches the limit, the conflict branch's WHERE no longer
    matches, so the increment simply doesn't happen and RETURNING yields nothing - a race between
    two simultaneous requests from the same identity can't both sneak past the cap.
    """
    ensure_plan_reader_usage_schema(db)
    row = db.execute(
        text("""
            INSERT INTO plan_reader_usage (identity_key, usage_date, count)
            VALUES (:key, CURRENT_DATE, 1)
            ON CONFLICT (identity_key, usage_date)
            DO UPDATE SET count = plan_reader_usage.count + 1
            WHERE plan_reader_usage.count < :limit
            RETURNING count
        """),
        {"key": identity_key, "limit": limit},
    ).fetchone()
    db.commit()
    return int(row[0]) if row else None

# Reuses the exact call pattern already proven in green.py's _ask_gemini_assistant (same env var,
# same base URL, same REST call - not the SDK) rather than a second, divergent way of talking to
# Gemini. GEMINI_API_KEY is shared across both features; GEMINI_PLAN_READER_MODEL is separate from
# green.py's GEMINI_MODEL because this needs real vision + structured-output quality (2.5 Flash),
# not the lighter model a chat fallback can get away with.
GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB - generous for a phone photo or a scanned PDF page
ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "application/pdf"}


def _gemini_api_key() -> Optional[str]:
    # GEMINI_PLAN_READER_API_KEY is optional and independent of GEMINI_API_KEY (the one green.py's
    # chat fallback already uses) - lets this feature run on a separate, free-tier-only key/project
    # (no billing attached) without sharing a rate-limit budget with, or being taken down by, an
    # unrelated feature's billing account running dry. Falls back to GEMINI_API_KEY if not set, so
    # nothing breaks for a deployment that intentionally shares one key across both features.
    dedicated = str(os.getenv("GEMINI_PLAN_READER_API_KEY") or "").strip()
    return dedicated or str(os.getenv("GEMINI_API_KEY") or "").strip() or None


def _gemini_model() -> str:
    # gemini-2.5-flash was retired for accounts/projects created after Google's cutoff (confirmed
    # live in production: a real 404 "no longer available to new users... use models/gemini-3.6-
    # flash" from Google's own API against the "LC Green" project, created Jul 2026). generateContent
    # (what this module calls) remains fully supported for 3.6-flash - Google's newer "Interactions
    # API" is only recommended for stateful/multi-turn/agentic use, not a single-shot extraction
    # call like this one, so no request-shape migration is needed, just the model id.
    return str(os.getenv("GEMINI_PLAN_READER_MODEL") or "gemini-3.6-flash").strip() or "gemini-3.6-flash"


class PlanReaderError(Exception):
    """Raised for anything that should surface as a clear message to the surveyor, as opposed to
    an unexpected crash - the router turns this into a 502 with the message intact, not a generic
    500."""


_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "plan_number": {"type": "STRING", "nullable": True},
        "title_text": {
            "type": "STRING", "nullable": True,
            "description": "The applicant/owner name printed on the plan, or its title if no name is given.",
        },
        "location_text": {"type": "STRING", "nullable": True},
        "lga_text": {"type": "STRING", "nullable": True},
        "state_text": {"type": "STRING", "nullable": True},
        "scale_text": {"type": "STRING", "nullable": True},
        "surveyor_name": {"type": "STRING", "nullable": True},
        "date_text": {"type": "STRING", "nullable": True},
        "stated_area_m2": {"type": "NUMBER", "nullable": True},
        "coordinate_system_guess": {
            "type": "STRING",
            "enum": ["wgs84", "minna_31", "minna_32", "minna_33", "utm_31n", "utm_32n", "utm_33n", "unknown"],
        },
        "coordinate_system_evidence": {
            "type": "STRING", "nullable": True,
            "description": "Short quote/reason from the plan for the coordinate-system guess, e.g. 'labelled ORIGIN: MINNA DATUM ZONE 32'.",
        },
        "beacons": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "station": {"type": "STRING"},
                    "x": {"type": "NUMBER", "description": "Easting if a projected system, otherwise longitude"},
                    "y": {"type": "NUMBER", "description": "Northing if a projected system, otherwise latitude"},
                    "confidence": {"type": "NUMBER", "description": "0 to 1 - how legible/certain this specific reading is"},
                    "raw_text": {"type": "STRING", "nullable": True, "description": "The exact text as printed, before interpretation"},
                },
                "required": ["station", "x", "y", "confidence"],
            },
        },
        "layout_type": {
            "type": "STRING",
            "enum": ["single_plot", "estate_layout"],
            "description": "'estate_layout' only if the plan clearly shows more than one distinct numbered plot (e.g. a subdivision/estate sheet); otherwise 'single_plot'.",
        },
        "plots": {
            "type": "ARRAY",
            "description": (
                "One entry per distinct plot/parcel on the plan. A single_plot plan still has exactly "
                "one entry here (the SAME beacons as the top-level 'beacons' field). An estate_layout "
                "plan lists every numbered plot here, in addition to putting its first/most prominent "
                "plot's beacons in the top-level 'beacons' field for backward compatibility."
            ),
            "items": {
                "type": "OBJECT",
                "properties": {
                    "plot_number": {"type": "STRING", "nullable": True, "description": "The plot/lot number or label exactly as printed, e.g. 'PLOT 14', 'LOT 7B'."},
                    "beacons": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "station": {"type": "STRING"},
                                "x": {"type": "NUMBER"},
                                "y": {"type": "NUMBER"},
                                "confidence": {"type": "NUMBER"},
                                "raw_text": {"type": "STRING", "nullable": True},
                            },
                            "required": ["station", "x", "y", "confidence"],
                        },
                    },
                },
                "required": ["beacons"],
            },
        },
        "roads": {
            "type": "ARRAY",
            "description": "Road centerlines visible on an estate layout, if any. Empty array for a single_plot plan or any plan with no roads drawn.",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING", "nullable": True, "description": "Road name/label exactly as printed, if any."},
                    "width_m": {"type": "NUMBER", "nullable": True},
                    "points": {
                        "type": "ARRAY",
                        "description": "Reference points along the road centerline/edge, in the same coordinate system as the beacons.",
                        "items": {
                            "type": "OBJECT",
                            "properties": {"x": {"type": "NUMBER"}, "y": {"type": "NUMBER"}},
                            "required": ["x", "y"],
                        },
                    },
                },
                "required": ["points"],
            },
        },
        "extraction_notes": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": "Anything illegible, ambiguous, or unusual that a human should double check",
        },
    },
    "required": ["coordinate_system_guess", "beacons"],
}

_PROMPT = """You are reading a scanned or photographed Nigerian land survey plan. Extract every \
beacon/station coordinate and the plan's identifying details EXACTLY as printed - do not guess or \
invent a value you cannot actually read. If a specific digit is unclear, give your best reading and \
lower that beacon's confidence score accordingly rather than omitting it.

Nigerian survey plans commonly show coordinates in one of: WGS84 latitude/longitude (degrees), or a \
projected Easting/Northing in metres on the Minna Datum or WGS84 UTM, in zone 31N, 32N, or 33N \
(Nigeria spans all three - zone boundaries are at 6 deg E and 12 deg E). Look for an explicit label \
(e.g. "MINNA DATUM ZONE 32", "UTM ZONE 31N", "ORIGIN...") before guessing from coordinate magnitude \
alone, and quote that label in coordinate_system_evidence. If nothing on the plan indicates the \
system, use "unknown" rather than assuming.

List beacons in the order they appear in the plan's coordinate table (this is usually also their \
boundary traverse order). Use the station names/numbers exactly as labelled (e.g. "PB1", "SC/AK/L \
72723", "A").

If this plan shows only ONE parcel/plot, set layout_type to "single_plot" and put that plot's \
beacons in both the top-level "beacons" field and as the single entry in "plots" - leave "roads" \
empty. If it is an ESTATE LAYOUT showing multiple distinct numbered plots (a subdivision sheet, \
scheme layout, or similar), set layout_type to "estate_layout", list EVERY plot in "plots" with its \
own plot_number and beacon set, still put the first/most prominent plot's beacons in the top-level \
"beacons" field too, and list any road centerlines you can read into "roads". Never invent a plot \
or road that isn't actually drawn on the plan.

Return ONLY the structured JSON described by the response schema - no prose."""


def extract_survey_plan(file_bytes: bytes, content_type: str) -> Dict[str, Any]:
    """Sends a single scanned/photographed survey plan to Gemini and returns the model's structured
    reading of it - raw values only (whatever coordinate numbers and units are printed), never
    converted or validated here. Coordinate-system conversion deliberately stays in the frontend's
    existing coordinateConverter.ts (reusing its already-proven Minna/UTM transform code instead of
    a second, divergent implementation in Python); validation happens in check_survey_plan below,
    against the raw numbers this returns.
    """
    api_key = _gemini_api_key()
    if not api_key:
        raise PlanReaderError("AI plan reading isn't configured yet (GEMINI_API_KEY is not set on the server).")

    request_body = {
        "contents": [{
            "parts": [
                {"inline_data": {"mime_type": content_type, "data": base64.b64encode(file_bytes).decode("ascii")}},
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

    # 3 attempts, not 2 - confirmed live (2026-08-31) that this project hits real, transient 429s
    # from Gemini even well under its displayed Tier 1 quota (a documented Google-side issue -
    # billing/tier status not always fully propagated to the rate limiter yet, plus a separate
    # "acceleration limit" that briefly throttles a project/key with no usage history regardless of
    # its stated cap - see plan_reader's calling code comment). Safe to retry a bit harder now that
    # this call runs in a background thread (see plan_reader.py's router) rather than blocking the
    # event loop, so a slightly longer worst case here no longer risks a gunicorn worker-timeout
    # kill the way it would have before that fix.
    max_attempts = 3
    last_error: Optional[str] = None
    for attempt in range(1, max_attempts + 1):
        try:
            # Explicit (connect, read) tuple, not one combined 60s figure - a genuinely blocked/
            # unreachable network path should fail in a few seconds, not hang for a minute; a real
            # but slow Gemini response still gets a generous read window.
            response = requests.post(url, params={"key": api_key}, json=request_body, timeout=(6, 25))
        except requests.RequestException as exc:
            last_error = f"Could not reach the AI service ({exc})."
            logger.warning("Plan reader Gemini call failed to reach the API (attempt %s/%s): %s", attempt, max_attempts, exc)
            time.sleep(1.0)
            continue
        if response.status_code == 429:
            last_error = "The AI service is rate-limited right now - please try again shortly."
            retry_after_header = str(response.headers.get("Retry-After") or "").strip()
            wait_seconds = float(retry_after_header) if retry_after_header.replace(".", "", 1).isdigit() else 2.0 * attempt
            logger.warning(
                "Plan reader Gemini call hit 429 (attempt %s/%s) - waiting %.1fs. Body: %s",
                attempt, max_attempts, min(wait_seconds, 8.0), response.text[:300],
            )
            time.sleep(min(wait_seconds, 8.0))
            continue
        if not response.ok:
            raise PlanReaderError(f"AI plan reading failed ({response.status_code}): {response.text[:300]}")
        try:
            data = response.json()
        except ValueError as exc:
            raise PlanReaderError("The AI service returned an unreadable response.") from exc
        candidates = data.get("candidates") or []
        if not candidates:
            block_reason = (data.get("promptFeedback") or {}).get("blockReason")
            raise PlanReaderError(f"The AI service returned no result{f' ({block_reason})' if block_reason else ''}.")
        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        text = "".join(str(p.get("text") or "") for p in parts).strip()
        if not text:
            raise PlanReaderError("The AI service returned an empty result.")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PlanReaderError("The AI service's response wasn't valid structured data.") from exc
        return parsed
    raise PlanReaderError(last_error or "AI plan reading failed after multiple attempts.")


def _shoelace_area_and_perimeter(points: List[Dict[str, float]]) -> Dict[str, Optional[float]]:
    """Shoelace-formula polygon area (m^2) and perimeter (m) for a planar (projected easting/
    northing) point sequence only - meaningless for raw lat/lon degrees, so the caller must only
    invoke this when coordinate_system_guess is a projected system.
    """
    n = len(points)
    if n < 3:
        return {"area_m2": None, "perimeter_m": None}
    area = 0.0
    perimeter = 0.0
    for i in range(n):
        x1, y1 = points[i]["x"], points[i]["y"]
        x2, y2 = points[(i + 1) % n]["x"], points[(i + 1) % n]["y"]
        area += x1 * y2 - x2 * y1
        perimeter += math.hypot(x2 - x1, y2 - y1)
    return {"area_m2": abs(area) / 2.0, "perimeter_m": perimeter}


_PROJECTED_SYSTEMS = {"minna_31", "minna_32", "minna_33", "utm_31n", "utm_32n", "utm_33n"}
# Loose bounding box covering every Nigerian UTM/Minna zone with generous margin - not a precise
# zone check (that needs a real projection), just enough to catch an obvious OCR digit slip (a
# dropped/extra digit typically throws a coordinate off by 10-100x, far outside this box).
_NIGERIA_PROJECTED_EASTING_RANGE = (60000.0, 940000.0)
_NIGERIA_PROJECTED_NORTHING_RANGE = (350000.0, 1600000.0)
_NIGERIA_LATLON_RANGE = {"lng": (1.0, 16.0), "lat": (2.0, 15.0)}

LOW_CONFIDENCE_THRESHOLD = 0.75
AREA_MISMATCH_TOLERANCE_FRACTION = 0.08  # 8% - generous enough for rounding in the plan's own stated area


def check_survey_plan(extracted: Dict[str, Any]) -> List[Dict[str, str]]:
    """Rule-based sanity checks on the AI's raw extraction - deliberately NOT another AI call.
    Closure/area/outlier math must be exact and reproducible, not a second source of hallucination
    on top of the first. Returns a list of {severity, code, message}; an "ok" entry means nothing
    else was found to flag, not that the data is unavailable.
    """
    checks: List[Dict[str, str]] = []
    beacons = extracted.get("beacons") or []
    system = str(extracted.get("coordinate_system_guess") or "unknown")

    if not beacons:
        return [{"severity": "error", "code": "no_beacons", "message": "No coordinate table could be read from this plan."}]
    if len(beacons) < 3:
        checks.append({
            "severity": "warning", "code": "few_beacons",
            "message": f"Only {len(beacons)} beacon(s) were read - a boundary needs at least 3.",
        })

    if system == "unknown":
        checks.append({
            "severity": "warning", "code": "unknown_coordinate_system",
            "message": "Could not determine the coordinate system from the plan - please select it manually before confirming.",
        })

    seen_stations: Dict[str, int] = {}
    for b in beacons:
        name = str(b.get("station") or "").strip().upper()
        seen_stations[name] = seen_stations.get(name, 0) + 1
    for name, count in seen_stations.items():
        if count > 1:
            checks.append({"severity": "warning", "code": "duplicate_station", "message": f'Station "{name}" appears {count} times.'})

    for b in beacons:
        confidence = b.get("confidence")
        if isinstance(confidence, (int, float)) and confidence < LOW_CONFIDENCE_THRESHOLD:
            checks.append({
                "severity": "warning", "code": "low_confidence",
                "message": f'Beacon "{b.get("station")}" was read with low confidence ({round(confidence * 100)}%) - please verify against the original.',
            })

    numeric_beacons = [b for b in beacons if isinstance(b.get("x"), (int, float)) and isinstance(b.get("y"), (int, float))]

    if system in _PROJECTED_SYSTEMS:
        ex_lo, ex_hi = _NIGERIA_PROJECTED_EASTING_RANGE
        ny_lo, ny_hi = _NIGERIA_PROJECTED_NORTHING_RANGE
        for b in numeric_beacons:
            if not (ex_lo <= b["x"] <= ex_hi and ny_lo <= b["y"] <= ny_hi):
                checks.append({
                    "severity": "warning", "code": "coordinate_out_of_range",
                    "message": f'Beacon "{b.get("station")}" ({b["x"]}, {b["y"]}) looks outside the plausible range for {system} - possible misread digit.',
                })
    elif system == "wgs84":
        lng_lo, lng_hi = _NIGERIA_LATLON_RANGE["lng"]
        lat_lo, lat_hi = _NIGERIA_LATLON_RANGE["lat"]
        for b in numeric_beacons:
            if not (lng_lo <= b["x"] <= lng_hi and lat_lo <= b["y"] <= lat_hi):
                checks.append({
                    "severity": "warning", "code": "coordinate_out_of_range",
                    "message": f'Beacon "{b.get("station")}" ({b["x"]}, {b["y"]}) looks outside Nigeria for WGS84 lat/long - possible misread digit or swapped lat/lng.',
                })

    if system in _PROJECTED_SYSTEMS and len(numeric_beacons) >= 3:
        geometry = _shoelace_area_and_perimeter([{"x": float(b["x"]), "y": float(b["y"])} for b in numeric_beacons])
        computed_area = geometry["area_m2"]
        stated_area = extracted.get("stated_area_m2")
        if computed_area and isinstance(stated_area, (int, float)) and stated_area > 0:
            diff_fraction = abs(computed_area - stated_area) / stated_area
            if diff_fraction > AREA_MISMATCH_TOLERANCE_FRACTION:
                checks.append({
                    "severity": "warning", "code": "area_mismatch",
                    "message": (
                        f"Plan states {stated_area:,.1f} sqm but the boundary as read computes to "
                        f"{computed_area:,.1f} sqm ({diff_fraction * 100:.0f}% difference) - check for a misread coordinate."
                    ),
                })

    if system in _PROJECTED_SYSTEMS and len(numeric_beacons) >= 4:
        cx = sum(b["x"] for b in numeric_beacons) / len(numeric_beacons)
        cy = sum(b["y"] for b in numeric_beacons) / len(numeric_beacons)
        distances = [math.hypot(b["x"] - cx, b["y"] - cy) for b in numeric_beacons]
        median_d = sorted(distances)[len(distances) // 2] or 1.0
        outlier_floor = max(median_d * 4, 50.0)
        for b, d in zip(numeric_beacons, distances):
            if d > outlier_floor:
                checks.append({
                    "severity": "warning", "code": "spatial_outlier",
                    "message": f'Beacon "{b.get("station")}" sits far from the rest of the boundary - possible misread coordinate.',
                })

    if not checks:
        checks.append({"severity": "ok", "code": "no_issues", "message": "No issues detected in the extracted data."})
    return checks
