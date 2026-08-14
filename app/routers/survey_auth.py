import os
import secrets
from threading import Lock
from time import time
from urllib.parse import urlencode

import requests
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.routers.plots import get_db
from app.utils.survey_auth_security import (
    consume_magic_link_otp,
    consume_magic_link_token,
    create_magic_link_token,
    find_or_create_survey_user,
    find_or_create_survey_user_by_google,
    issue_survey_session,
    resolve_survey_session,
    revoke_survey_session,
)
from app.utils.survey_email import send_magic_link_email

router = APIRouter(prefix="/survey/auth", tags=["survey-auth"])

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

# Short-lived, single-use exchange codes handed to the frontend via the OAuth redirect URL
# instead of the real bearer token, so the token itself never appears in browser history / a
# Referer header. In-memory is fine: each code lives for well under a minute.
_OAUTH_EXCHANGE_TTL_SECONDS = 60
_oauth_exchange_codes: dict[str, dict] = {}
_oauth_exchange_lock = Lock()


def _frontend_base_url() -> str:
    return str(os.getenv("LANDCHECK_SURVEY_WEB_URL") or "https://landcheck.online").strip().rstrip("/")


@router.post("/magic-link/request")
def request_magic_link(email: str = Body(..., embed=True), db: Session = Depends(get_db)):
    user_id = find_or_create_survey_user(db, email=email)
    raw_token, otp_code = create_magic_link_token(db, user_id=user_id)
    link_url = f"{_frontend_base_url()}/survey/auth/verify?token={raw_token}"
    try:
        send_magic_link_email(to_email=str(email).strip().lower(), link_url=link_url, otp_code=otp_code)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not send sign-in email: {exc}") from exc
    return {"status": "ok"}


@router.post("/magic-link/verify")
def verify_magic_link(request: Request, token: str = Body(..., embed=True), db: Session = Depends(get_db)):
    user_id = consume_magic_link_token(db, raw_token=token)
    return issue_survey_session(db, user_id=user_id, request=request)


@router.post("/otp/verify")
def verify_magic_link_otp(
    request: Request,
    email: str = Body(..., embed=True),
    code: str = Body(..., embed=True),
    db: Session = Depends(get_db),
):
    user_id = consume_magic_link_otp(db, email=email, code=code)
    return issue_survey_session(db, user_id=user_id, request=request)


@router.get("/google/start")
def google_oauth_start():
    client_id = str(os.getenv("GOOGLE_OAUTH_CLIENT_ID") or "").strip()
    redirect_uri = str(os.getenv("GOOGLE_OAUTH_REDIRECT_URI") or "").strip()
    if not client_id or not redirect_uri:
        raise HTTPException(status_code=503, detail="Google sign-in is not configured yet")
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "prompt": "select_account",
    }
    return RedirectResponse(f"{GOOGLE_AUTH_URL}?{urlencode(params)}")


@router.get("/google/callback")
def google_oauth_callback(request: Request, code: str, db: Session = Depends(get_db)):
    client_id = str(os.getenv("GOOGLE_OAUTH_CLIENT_ID") or "").strip()
    client_secret = str(os.getenv("GOOGLE_OAUTH_CLIENT_SECRET") or "").strip()
    redirect_uri = str(os.getenv("GOOGLE_OAUTH_REDIRECT_URI") or "").strip()
    if not client_id or not client_secret or not redirect_uri:
        raise HTTPException(status_code=503, detail="Google sign-in is not configured yet")

    token_resp = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=15,
    )
    if not token_resp.ok:
        raise HTTPException(status_code=502, detail="Google sign-in failed")
    google_access_token = token_resp.json().get("access_token")

    userinfo_resp = requests.get(
        GOOGLE_USERINFO_URL,
        headers={"Authorization": f"Bearer {google_access_token}"},
        timeout=15,
    )
    if not userinfo_resp.ok:
        raise HTTPException(status_code=502, detail="Google sign-in failed")
    userinfo = userinfo_resp.json()

    user_id = find_or_create_survey_user_by_google(
        db,
        google_sub=str(userinfo.get("sub") or ""),
        email=str(userinfo.get("email") or ""),
        full_name=userinfo.get("name"),
    )
    session = issue_survey_session(db, user_id=user_id, request=request)

    exchange_code = secrets.token_urlsafe(24)
    with _oauth_exchange_lock:
        _oauth_exchange_codes[exchange_code] = {"session": session, "expires_at": time() + _OAUTH_EXCHANGE_TTL_SECONDS}
    return RedirectResponse(f"{_frontend_base_url()}/survey/auth/callback?code={exchange_code}")


@router.post("/google/exchange")
def google_oauth_exchange(code: str = Body(..., embed=True)):
    with _oauth_exchange_lock:
        entry = _oauth_exchange_codes.pop(code, None)
    if not entry or entry["expires_at"] < time():
        raise HTTPException(status_code=400, detail="This sign-in link has expired")
    return entry["session"]


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    revoke_survey_session(db, request)
    return {"status": "ok"}


@router.get("/me")
def me(request: Request, db: Session = Depends(get_db)):
    session = resolve_survey_session(db, request)
    if not session:
        return {"authed": False}
    return {
        "authed": True,
        "user": {"id": session.user_id, "email": session.email, "full_name": session.full_name},
    }
