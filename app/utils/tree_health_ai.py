from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Any, Dict, Optional

import requests
from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Same free-tier-friendly setup as plan_reader.py (independent, optional API key so this feature's
# quota/billing isn't shared with, or taken down by, the Plan Reader or the green.py chat assistant).
GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

# Vision assessment from a single photo is inherently rougher than the Plan Reader's text/number
# extraction - kept to 2/day/user (vs. the Plan Reader's 3) purely to stretch the shared free-tier
# daily request budget across both AI features while still on the free tier.
TREE_HEALTH_DAILY_LIMIT = 2


def _gemini_api_key() -> Optional[str]:
    dedicated = str(os.getenv("GEMINI_TREE_HEALTH_API_KEY") or "").strip()
    if dedicated:
        return dedicated
    plan_reader_key = str(os.getenv("GEMINI_PLAN_READER_API_KEY") or "").strip()
    if plan_reader_key:
        return plan_reader_key
    return str(os.getenv("GEMINI_API_KEY") or "").strip() or None


def _gemini_model() -> str:
    return str(os.getenv("GEMINI_TREE_HEALTH_MODEL") or "gemini-3.6-flash").strip() or "gemini-3.6-flash"


class TreeHealthError(Exception):
    """Raised for anything that should surface as a clear message in the UI, as opposed to an
    unexpected crash - the router turns this into a 502 with the message intact."""


# Health class + score are modelled on the vocabulary/methodology actually used by USDA Forest
# Service FIA crown-condition surveys and i-Tree Eco's condition-class rating: both grade a tree
# chiefly by estimating crown dieback (% of the crown that is dead/bare branches, judged to the
# nearest 5%) alongside foliage density/transparency, then bucket that into Excellent/Good/Fair/
# Poor/Critical/Dead classes. A single photo can only approximate this (no DBH, no all-round view,
# no arborist in person), so health_score here is explicitly "100 - estimated crown dieback %" - a
# defensible, named methodology rather than an invented black-box number - and confidence/
# photo_quality_note exist specifically to be honest about that limitation.
_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "crown_dieback_percent": {
            "type": "NUMBER",
            "description": "Estimated % of the visible crown/canopy that is dead, bare, or leafless branches, to the nearest 5%.",
        },
        "health_score": {
            "type": "NUMBER",
            "description": "100 minus crown_dieback_percent, i.e. an overall vigor score from 0 (dead) to 100 (excellent).",
        },
        "health_class": {
            "type": "STRING",
            "enum": ["excellent", "good", "fair", "poor", "critical", "dead", "unknown"],
        },
        "foliage_density_assessment": {"type": "STRING", "nullable": True},
        "leaf_color_assessment": {"type": "STRING", "nullable": True},
        "signs_of_pests_or_disease": {"type": "BOOLEAN"},
        "pest_or_disease_notes": {"type": "STRING", "nullable": True},
        "trunk_bark_condition": {"type": "STRING", "nullable": True},
        "structural_concerns": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": "e.g. leaning trunk, major broken limb, exposed roots - empty array if none visible.",
        },
        "summary": {
            "type": "STRING",
            "description": "2-4 plain-language sentences a non-arborist supervisor can read at a glance.",
        },
        "recommended_action": {"type": "STRING"},
        "confidence": {
            "type": "NUMBER",
            "description": "0 to 1 - the model's own confidence in this reading given photo quality/angle/distance.",
        },
        "photo_quality_note": {
            "type": "STRING", "nullable": True,
            "description": "Set only if the photo is blurry, too distant, poorly lit, or doesn't clearly show the tree.",
        },
    },
    "required": ["crown_dieback_percent", "health_score", "health_class", "summary", "recommended_action", "confidence"],
}

_PROMPT_TEMPLATE = """You are assessing the visible health of a single tree from one field photo, \
using the same visual vocabulary as USDA Forest Service FIA crown-condition surveys and i-Tree \
Eco's condition-class rating: estimate crown dieback (the % of the crown/canopy that is dead, \
bare, or leafless branches, to the nearest 5%), foliage density/color, and any visible pest, \
disease, or structural problems (leaning trunk, major broken limbs, exposed/damaged roots, bark \
wounds or cankers).

Set health_score to 100 minus your crown_dieback_percent estimate, then choose health_class from: \
excellent (0-10% dieback), good (11-30%), fair (31-50%), poor (51-75%), critical (76-99%), dead \
(100%, no live foliage) - or "unknown" if the tree itself isn't clearly visible enough to judge.

Be honest about the limits of a single photo: if it's blurry, too distant, badly lit, or shows \
only part of the tree, say so in photo_quality_note and lower your confidence score accordingly - \
do not invent detail you cannot actually see. This is an advisory visual screening, not a \
certified arborist diagnosis, so recommended_action should be practical field guidance (e.g. \
"Continue routine watering/monitoring", "Flag for an in-person inspection - possible pest \
damage", "Prune dead limbs at next maintenance visit"), not a medical-sounding verdict.
{species_line}
Return ONLY the structured JSON described by the response schema - no prose outside it."""


def assess_tree_health(image_bytes: bytes, content_type: str, species: Optional[str] = None) -> Dict[str, Any]:
    """Sends one tree photo to Gemini vision and returns its structured health reading. Mirrors
    plan_reader.py's extract_survey_plan call shape/retry logic exactly (same base URL, same
    inline_data + responseSchema pattern, same 429/Retry-After handling) - proven live against the
    same free-tier quota quirks, so no need for a second, divergent way of calling Gemini.
    """
    api_key = _gemini_api_key()
    if not api_key:
        raise TreeHealthError("AI tree health checks aren't configured yet (GEMINI_API_KEY is not set on the server).")

    species_line = f"\nSpecies (if known - use only as context, not as the basis for your visual reading): {species}\n" if species else ""
    prompt = _PROMPT_TEMPLATE.format(species_line=species_line)

    request_body = {
        "contents": [{
            "parts": [
                {"inline_data": {"mime_type": content_type, "data": base64.b64encode(image_bytes).decode("ascii")}},
                {"text": prompt},
            ],
        }],
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
            logger.warning("Tree health Gemini call failed to reach the API (attempt %s/%s): %s", attempt, max_attempts, exc)
            time.sleep(1.0)
            continue
        if response.status_code == 429:
            last_error = "The AI service is rate-limited right now - please try again shortly."
            retry_after_header = str(response.headers.get("Retry-After") or "").strip()
            wait_seconds = float(retry_after_header) if retry_after_header.replace(".", "", 1).isdigit() else 2.0 * attempt
            logger.warning(
                "Tree health Gemini call hit 429 (attempt %s/%s) - waiting %.1fs. Body: %s",
                attempt, max_attempts, min(wait_seconds, 8.0), response.text[:300],
            )
            time.sleep(min(wait_seconds, 8.0))
            continue
        if not response.ok:
            raise TreeHealthError(f"AI tree health check failed ({response.status_code}): {response.text[:300]}")
        try:
            data = response.json()
        except ValueError as exc:
            raise TreeHealthError("The AI service returned an unreadable response.") from exc
        candidates = data.get("candidates") or []
        if not candidates:
            block_reason = (data.get("promptFeedback") or {}).get("blockReason")
            raise TreeHealthError(f"The AI service returned no result{f' ({block_reason})' if block_reason else ''}.")
        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        text = "".join(str(p.get("text") or "") for p in parts).strip()
        if not text:
            raise TreeHealthError("The AI service returned an empty result.")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise TreeHealthError("The AI service's response wasn't valid structured data.") from exc
        return parsed
    raise TreeHealthError(last_error or "AI tree health check failed after multiple attempts.")


def ensure_tree_health_schema(db: Session) -> None:
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS green_tree_health_checks (
            id SERIAL PRIMARY KEY,
            tree_id INTEGER NOT NULL,
            project_id INTEGER,
            checked_by TEXT,
            crown_dieback_percent DOUBLE PRECISION,
            health_score DOUBLE PRECISION,
            health_class TEXT,
            foliage_density_assessment TEXT,
            leaf_color_assessment TEXT,
            signs_of_pests_or_disease BOOLEAN,
            pest_or_disease_notes TEXT,
            trunk_bark_condition TEXT,
            structural_concerns JSONB,
            summary TEXT,
            recommended_action TEXT,
            confidence DOUBLE PRECISION,
            photo_quality_note TEXT,
            object_key TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """))
    db.execute(text("CREATE INDEX IF NOT EXISTS ix_green_tree_health_checks_tree ON green_tree_health_checks (tree_id, created_at DESC)"))
    db.commit()


def record_tree_health_check(
    db: Session, *, tree_id: int, project_id: Optional[int], checked_by: Optional[str],
    object_key: Optional[str], result: Dict[str, Any],
) -> Dict[str, Any]:
    row = db.execute(
        text("""
            INSERT INTO green_tree_health_checks (
                tree_id, project_id, checked_by, crown_dieback_percent, health_score, health_class,
                foliage_density_assessment, leaf_color_assessment, signs_of_pests_or_disease,
                pest_or_disease_notes, trunk_bark_condition, structural_concerns, summary,
                recommended_action, confidence, photo_quality_note, object_key
            ) VALUES (
                :tree_id, :project_id, :checked_by, :crown_dieback_percent, :health_score, :health_class,
                :foliage_density_assessment, :leaf_color_assessment, :signs_of_pests_or_disease,
                :pest_or_disease_notes, :trunk_bark_condition, CAST(:structural_concerns AS JSONB), :summary,
                :recommended_action, :confidence, :photo_quality_note, :object_key
            )
            RETURNING id, created_at
        """),
        {
            "tree_id": tree_id, "project_id": project_id, "checked_by": checked_by,
            "crown_dieback_percent": result.get("crown_dieback_percent"),
            "health_score": result.get("health_score"),
            "health_class": result.get("health_class"),
            "foliage_density_assessment": result.get("foliage_density_assessment"),
            "leaf_color_assessment": result.get("leaf_color_assessment"),
            "signs_of_pests_or_disease": bool(result.get("signs_of_pests_or_disease")),
            "pest_or_disease_notes": result.get("pest_or_disease_notes"),
            "trunk_bark_condition": result.get("trunk_bark_condition"),
            "structural_concerns": json.dumps(result.get("structural_concerns") or []),
            "summary": result.get("summary"),
            "recommended_action": result.get("recommended_action"),
            "confidence": result.get("confidence"),
            "photo_quality_note": result.get("photo_quality_note"),
            "object_key": object_key,
        },
    ).mappings().first()
    db.commit()
    return {
        "id": row["id"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "tree_id": tree_id,
        **result,
    }


def get_latest_tree_health_check(db: Session, tree_id: int) -> Optional[Dict[str, Any]]:
    row = db.execute(
        text("""
            SELECT id, tree_id, created_at, crown_dieback_percent, health_score, health_class,
                   foliage_density_assessment, leaf_color_assessment, signs_of_pests_or_disease,
                   pest_or_disease_notes, trunk_bark_condition, structural_concerns, summary,
                   recommended_action, confidence, photo_quality_note
            FROM green_tree_health_checks
            WHERE tree_id = :tree_id
            ORDER BY created_at DESC
            LIMIT 1
        """),
        {"tree_id": tree_id},
    ).mappings().first()
    if not row:
        return None
    item = dict(row)
    item["created_at"] = row["created_at"].isoformat() if row["created_at"] else None
    return item
