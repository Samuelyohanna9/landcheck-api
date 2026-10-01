from __future__ import annotations

"""Small HTTP security primitives shared by the browser authentication flows."""

import os
from typing import Final

from fastapi import Request, Response


ESTATE_SESSION_COOKIE: Final[str] = "lc_estate_session"
GREEN_SESSION_COOKIE: Final[str] = "lc_green_session"
SURVEY_SESSION_COOKIE: Final[str] = "lc_survey_session"


def _is_production() -> bool:
    value = str(os.getenv("LANDCHECK_ENV") or os.getenv("APP_ENV") or "").strip().lower()
    return value in {"prod", "production", "live"}


def request_uses_browser_auth(request: Request) -> bool:
    """Return true for the first-party web clients, not native/mobile callers.

    The browser gets an HttpOnly cookie. Native clients continue receiving a bearer token because
    they store it in the platform secure keychain rather than browser storage.
    """
    label = str(request.headers.get("x-lc-client") or "").strip().lower()
    return label.endswith("-web") or "-web-" in label


def set_session_cookie(response: Response, *, name: str, token: str, max_age: int) -> None:
    response.set_cookie(
        key=name,
        value=str(token),
        max_age=max(60, int(max_age)),
        httponly=True,
        secure=_is_production(),
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, *, name: str) -> None:
    response.delete_cookie(key=name, path="/")


def cookie_token(request: Request, name: str) -> str | None:
    cookies = getattr(request, "cookies", {}) or {}
    value = str(cookies.get(name) or "").strip()
    return value[:1500] if value else None
