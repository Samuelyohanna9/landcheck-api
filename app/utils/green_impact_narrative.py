from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

# Same free-tier-friendly setup as plan_reader.py / tree_health_ai.py - independent, optional API
# key so this feature's quota/billing isn't shared with, or taken down by, the other AI features.
GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

# Report generation is an admin/supervisor action, not a per-field-photo action, so it happens far
# less often than the Plan Reader or Tree Health checks - but it still shares the same free-tier
# daily request budget, so it still gets a modest cap rather than being unlimited.
IMPACT_NARRATIVE_DAILY_LIMIT = 5

# The PDF's "Board Summary" box is a small, fixed-size panel (see green_pdf.py's
# render_green_csr_programme_report_pdf) that today holds ~5 short bullet sentences - roughly this
# many characters fits without the box's own wrapping logic silently cutting text off. Enforced
# server-side as a hard safety net; the prompt also asks for this length, but a model's own
# length compliance is never trustworthy enough to rely on alone.
MAX_NARRATIVE_CHARS = 700


def _gemini_api_key() -> Optional[str]:
    dedicated = str(os.getenv("GEMINI_IMPACT_NARRATIVE_API_KEY") or "").strip()
    if dedicated:
        return dedicated
    plan_reader_key = str(os.getenv("GEMINI_PLAN_READER_API_KEY") or "").strip()
    if plan_reader_key:
        return plan_reader_key
    return str(os.getenv("GEMINI_API_KEY") or "").strip() or None


def _gemini_model() -> str:
    return str(os.getenv("GEMINI_IMPACT_NARRATIVE_MODEL") or "gemini-3.6-flash").strip() or "gemini-3.6-flash"


class ImpactNarrativeError(Exception):
    """Raised for anything that should surface as a clear message in the UI, as opposed to an
    unexpected crash - the router turns this into a 502 with the message intact."""


_PROMPT_TEMPLATE = """You are drafting the "Board Summary" paragraph of a corporate CSR/ESG \
environmental programme report, for a company board and stakeholders to read. Write 2-3 short, \
factual sentences (no more than roughly 600 characters total) that turn the metrics below into a \
plain-language executive narrative - the kind of summary a board member reads in 10 seconds, not \
marketing copy. Use only the numbers given; never invent a figure, trend, or claim that isn't in \
the data. Do not use bullet points, headings, or markdown - return plain prose only, no quotes \
around it.

Programme metrics:
{metrics_text}

Return ONLY the narrative paragraph text - nothing else before or after it."""


def _format_metrics_for_prompt(metrics: Dict[str, Any]) -> str:
    lines = []
    for label, value in metrics.items():
        if value is None or value == "":
            continue
        lines.append(f"- {label}: {value}")
    return "\n".join(lines)


def _truncate_to_sentence_boundary(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    last_boundary = max(truncated.rfind(". "), truncated.rfind(".\n"))
    if last_boundary > max_chars * 0.4:
        return truncated[: last_boundary + 1].strip()
    return truncated.rstrip() + "…"


def generate_impact_narrative(metrics: Dict[str, Any]) -> str:
    """Sends already-computed programme metrics (never raw field data or photos - this is a
    text-only call) to Gemini and returns a short board-ready narrative paragraph. Mirrors plan_
    reader.py's / tree_health_ai.py's call shape and retry/429 handling - proven live against the
    same free-tier quota quirks.
    """
    api_key = _gemini_api_key()
    if not api_key:
        raise ImpactNarrativeError("AI impact narratives aren't configured yet (GEMINI_API_KEY is not set on the server).")

    prompt = _PROMPT_TEMPLATE.format(metrics_text=_format_metrics_for_prompt(metrics))
    request_body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3},
    }
    url = f"{GEMINI_API_BASE_URL}/models/{_gemini_model()}:generateContent"

    max_attempts = 3
    last_error: Optional[str] = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.post(url, params={"key": api_key}, json=request_body, timeout=(6, 25))
        except requests.RequestException as exc:
            last_error = f"Could not reach the AI service ({exc})."
            logger.warning("Impact narrative Gemini call failed to reach the API (attempt %s/%s): %s", attempt, max_attempts, exc)
            time.sleep(1.0)
            continue
        if response.status_code == 429:
            last_error = "The AI service is rate-limited right now - please try again shortly."
            retry_after_header = str(response.headers.get("Retry-After") or "").strip()
            wait_seconds = float(retry_after_header) if retry_after_header.replace(".", "", 1).isdigit() else 2.0 * attempt
            logger.warning(
                "Impact narrative Gemini call hit 429 (attempt %s/%s) - waiting %.1fs. Body: %s",
                attempt, max_attempts, min(wait_seconds, 8.0), response.text[:300],
            )
            time.sleep(min(wait_seconds, 8.0))
            continue
        if not response.ok:
            raise ImpactNarrativeError(f"AI impact narrative generation failed ({response.status_code}): {response.text[:300]}")
        try:
            data = response.json()
        except ValueError as exc:
            raise ImpactNarrativeError("The AI service returned an unreadable response.") from exc
        candidates = data.get("candidates") or []
        if not candidates:
            block_reason = (data.get("promptFeedback") or {}).get("blockReason")
            raise ImpactNarrativeError(f"The AI service returned no result{f' ({block_reason})' if block_reason else ''}.")
        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        text = "".join(str(p.get("text") or "") for p in parts).strip()
        if not text:
            raise ImpactNarrativeError("The AI service returned an empty result.")
        return _truncate_to_sentence_boundary(text, MAX_NARRATIVE_CHARS)
    raise ImpactNarrativeError(last_error or "AI impact narrative generation failed after multiple attempts.")
