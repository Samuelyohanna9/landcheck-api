from __future__ import annotations

"""Daily.co video-room client for the live GPS-verified site inspection feature - a small,
independent client mirroring app/utils/estate_paystack.py's shape (env-var secret, "not
configured" raises a clear 503 rather than failing silently, one call_* wrapper for request/
error handling). Only room create/delete is needed - the call itself happens entirely in the
browser via the Daily prebuilt/call-object JS client, not through this backend.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from fastapi import HTTPException

logger = logging.getLogger(__name__)

DAILY_API_BASE_URL = "https://api.daily.co/v1"
ROOM_LIFETIME = timedelta(hours=6)  # a session is one site visit, not an ongoing room


def _daily_api_key() -> str | None:
    value = str(os.getenv("DAILY_API_KEY") or "").strip()
    return value or None


def configured() -> bool:
    return bool(_daily_api_key())


def _headers() -> dict[str, str]:
    api_key = _daily_api_key()
    if not api_key:
        raise HTTPException(status_code=503, detail="Live video inspections are not configured on this server")
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _call(method: str, path: str, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{DAILY_API_BASE_URL}{path}"
    try:
        response = requests.request(method, url, headers=_headers(), json=payload, timeout=15)
    except requests.RequestException as exc:
        logger.exception("Could not reach Daily.co (%s %s)", method, path)
        raise HTTPException(status_code=502, detail="Could not reach the video service. Please try again.") from exc
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code >= 400:
        detail = str(body.get("error") or body.get("info") or "The video service rejected the request.")
        raise HTTPException(status_code=502, detail=detail)
    return body


def create_room(*, session_uid: str) -> tuple[str, str]:
    """Creates a short-lived Daily room for one live inspection session. Returns (room_name,
    room_url). `exp` makes Daily expire/delete the room on its own if `end_room` is never
    called (a dropped call, a crashed browser tab)."""
    expires_at = int((datetime.now(timezone.utc) + ROOM_LIFETIME).timestamp())
    body = _call(
        "POST",
        "/rooms",
        payload={
            "name": f"estate-inspection-{session_uid}",
            "privacy": "public",
            "properties": {
                "exp": expires_at,
                "eject_at_room_exp": True,
                "enable_screenshare": False,
                "enable_chat": True,
                "max_participants": 2,
                # Daily's default prebuilt call shows its own "enter your name / check camera"
                # lobby screen before actually joining the room - on a 2-person ad-hoc call that
                # just read as "ask to join, then stuck on waiting for others" with no indication
                # anything was wrong (both sides were really just sitting on that lobby screen).
                # Both agent and buyer should land directly in the live call the moment the page
                # opens and camera/mic permission is granted.
                "enable_prejoin_ui": False,
                "enable_knocking": False,
            },
        },
    )
    return str(body.get("name") or ""), str(body.get("url") or "")


def end_room(room_name: str) -> None:
    """Best-effort room teardown when a session is explicitly ended - never raises, since the
    room's own `exp` is the guaranteed cleanup and a failed delete here shouldn't block the
    session from ending in our own database."""
    if not room_name or not configured():
        return
    try:
        requests.request("DELETE", f"{DAILY_API_BASE_URL}/rooms/{room_name}", headers=_headers(), timeout=15)
    except requests.RequestException:
        logger.warning("Could not delete Daily room %s (it will still expire on its own)", room_name)
