from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.estate_auth import EstateAccount, EstateAuthSession
from app.models.estate_billing import EstatePasswordResetToken
from app.models.estate_foundation import EstateOrganization, EstateOrganizationEntitlement, EstateOrganizationMember
from app.routers.plots import get_db
from app.schemas.estate_auth import EstateLogin, EstateRegister
from app.services.estates import estate_email
from app.services.estates.identity import (
    hash_password,
    issue_session,
    normalize_email,
    resolve_session,
    revoke_session,
    slugify,
    verify_password,
)


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=200)


RESET_TOKEN_TTL_HOURS = 1


def _hash_reset_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


router = APIRouter(prefix="/estates/auth", tags=["estate-auth"])
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _account_payload(db: Session, account: EstateAccount) -> dict:
    membership = (
        db.query(EstateOrganizationMember)
        .filter(
            EstateOrganizationMember.organization_id == account.organization_id,
            EstateOrganizationMember.subject_type == "estate_account",
            EstateOrganizationMember.subject_id == str(account.id),
            EstateOrganizationMember.is_active.is_(True),
        )
        .one_or_none()
    )
    organization = db.get(EstateOrganization, account.organization_id)
    return {
        "id": int(account.id),
        "account_uid": account.account_uid,
        "full_name": account.full_name,
        "email": account.email,
        "role_key": membership.role_key if membership else None,
        "organization_id": int(account.organization_id),
        "organization_name": organization.name if organization else None,
        "organization_slug": organization.slug if organization else None,
    }


@router.post("/register", status_code=201)
def register(payload: EstateRegister, request: Request, db: Session = Depends(get_db)):
    email = normalize_email(payload.email)
    if not EMAIL_PATTERN.fullmatch(email):
        raise HTTPException(status_code=422, detail="Enter a valid company email address")
    if db.query(EstateAccount).filter(EstateAccount.email_normalized == email).first():
        raise HTTPException(status_code=409, detail="An Estate account already exists for this email")
    base_slug = slugify(payload.organization_slug or payload.organization_name)
    slug = base_slug
    for suffix in range(2, 100):
        if not db.query(EstateOrganization).filter(EstateOrganization.slug == slug).first():
            break
        slug = f"{base_slug[: max(1, 108 - len(str(suffix)) - 1)]}-{suffix}"
    else:
        raise HTTPException(status_code=409, detail="Could not create a unique company address")
    organization = EstateOrganization(
        name=payload.organization_name.strip(),
        slug=slug,
        contact_email=email,
        status="active",
    )
    db.add(organization)
    db.flush()
    account = EstateAccount(
        organization_id=organization.id,
        email=payload.email.strip(),
        email_normalized=email,
        full_name=payload.full_name.strip(),
        password_hash=hash_password(payload.password),
        status="active",
    )
    db.add(account)
    db.flush()
    db.add(
        EstateOrganizationMember(
            organization_id=organization.id,
            subject_type="estate_account",
            subject_id=str(account.id),
            role_key="owner",
            is_active=True,
        )
    )
    db.add(
        EstateOrganizationEntitlement(
            organization_id=organization.id,
            feature_key="ESTATES_ENABLED",
            is_enabled=True,
        )
    )
    try:
        session = issue_session(db, account, request)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="This Estate company or account already exists") from exc
    estate_email.send_welcome_email(organization=organization, account=account)
    return {**session, "user": _account_payload(db, account), "organization": {"id": organization.id, "name": organization.name, "slug": organization.slug}}


@router.post("/login")
def login(payload: EstateLogin, request: Request, db: Session = Depends(get_db)):
    email = normalize_email(payload.email)
    if not EMAIL_PATTERN.fullmatch(email):
        raise HTTPException(status_code=422, detail="Enter a valid company email address")
    account = db.query(EstateAccount).filter(EstateAccount.email_normalized == email).one_or_none()
    if not account or account.status != "active" or not verify_password(payload.password, account.password_hash):
        raise HTTPException(status_code=401, detail="Invalid Estate email or password")
    organization = db.get(EstateOrganization, account.organization_id)
    if not organization or organization.status != "active":
        raise HTTPException(status_code=403, detail="This Estate company is not active")
    account.last_login_at = datetime.utcnow()
    session = issue_session(db, account, request)
    db.commit()
    return {**session, "user": _account_payload(db, account), "organization": {"id": organization.id, "name": organization.name, "slug": organization.slug}}


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    revoke_session(db, request)
    db.commit()
    return {"status": "ok"}


@router.get("/me")
def me(request: Request, db: Session = Depends(get_db)):
    session = resolve_session(db, request)
    if not session:
        return {"authed": False}
    account = db.get(EstateAccount, session.account_id)
    if not account:
        return {"authed": False}
    return {"authed": True, "user": _account_payload(db, account), "expires_at": session.expires_at}


@router.post("/forgot-password")
def forgot_password(payload: ForgotPasswordRequest, db: Session = Depends(get_db)):
    """Always returns the same generic message regardless of whether the email exists, so this
    endpoint can't be used to discover which company emails have an Estate account."""
    generic_response = {"status": "ok", "message": "If an account exists for that email, a reset link has been sent."}
    email = normalize_email(payload.email)
    account = db.query(EstateAccount).filter(EstateAccount.email_normalized == email, EstateAccount.status == "active").one_or_none()
    if not account:
        return generic_response
    raw_token = secrets.token_urlsafe(32)
    db.add(
        EstatePasswordResetToken(
            account_id=account.id,
            token_hash=_hash_reset_token(raw_token),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=RESET_TOKEN_TTL_HOURS),
        )
    )
    db.commit()
    web_url = str(os.getenv("LANDCHECK_WEB_URL") or "https://landcheck.online").rstrip("/")
    reset_link = f"{web_url}/estates/reset-password?token={raw_token}"
    estate_email.send_password_reset_email(account=account, reset_link=reset_link)
    return generic_response


@router.post("/reset-password")
def reset_password(payload: ResetPasswordRequest, db: Session = Depends(get_db)):
    token_hash = _hash_reset_token(str(payload.token or ""))
    row = db.query(EstatePasswordResetToken).filter(EstatePasswordResetToken.token_hash == token_hash).one_or_none()
    now = datetime.now(timezone.utc)
    expires_at = row.expires_at if row and row.expires_at.tzinfo else (row.expires_at.replace(tzinfo=timezone.utc) if row else None)
    if not row or row.used_at is not None or (expires_at and expires_at <= now):
        raise HTTPException(status_code=400, detail="This reset link is invalid or has expired. Request a new one.")
    account = db.get(EstateAccount, row.account_id)
    if not account:
        raise HTTPException(status_code=400, detail="This reset link is invalid or has expired. Request a new one.")
    try:
        account.password_hash = hash_password(payload.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    row.used_at = now
    # Force re-login everywhere after a password change - standard practice, and it also covers
    # the case where the password was reset because a session/device was compromised.
    db.query(EstateAuthSession).filter(EstateAuthSession.account_id == account.id, EstateAuthSession.session_state == "active").update(
        {"session_state": "revoked", "revoked_at": now, "revoke_reason": "password_reset"}
    )
    db.commit()
    return {"status": "ok"}
