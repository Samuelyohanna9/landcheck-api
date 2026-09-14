from __future__ import annotations

from datetime import datetime
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.estate_auth import EstateAccount
from app.models.estate_foundation import EstateOrganization, EstateOrganizationEntitlement, EstateOrganizationMember
from app.routers.plots import get_db
from app.schemas.estate_auth import EstateLogin, EstateRegister
from app.services.estates.identity import (
    hash_password,
    issue_session,
    normalize_email,
    resolve_session,
    revoke_session,
    slugify,
    verify_password,
)


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
