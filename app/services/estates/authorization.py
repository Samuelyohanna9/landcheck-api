from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from app.models.estate_foundation import EstateOrganization, EstateOrganizationMember
from app.services.estates.permissions import has_permission
from app.services.estates.identity import resolve_session
from app.utils.auth_security import resolve_request_session
from app.utils.survey_auth_security import resolve_survey_session


@dataclass(frozen=True, slots=True)
class EstatePrincipal:
    subject_type: str
    subject_id: str
    display_name: str | None


@dataclass(frozen=True, slots=True)
class EstateAccess:
    organization_id: int
    organization_name: str
    organization_slug: str
    role_key: str
    principal: EstatePrincipal


def resolve_estate_principal(db: Session, request: Request) -> EstatePrincipal:
    """Resolve an existing authenticated identity without changing either auth domain.

    Membership stores this neutral type/id pair, so Estates does not depend on Green tables and
    does not change the Survey user/plot ownership model.
    """
    cached = getattr(request.state, "estate_principal", None)
    if isinstance(cached, EstatePrincipal):
        return cached

    estate_session = resolve_session(db, request)
    if estate_session is not None:
        principal = EstatePrincipal("estate_account", str(estate_session.account_id), estate_session.full_name)
        request.state.estate_principal = principal
        return principal

    survey_session = resolve_survey_session(db, request)
    if survey_session is not None:
        principal = EstatePrincipal("survey_user", str(survey_session.user_id), survey_session.full_name or survey_session.email)
        request.state.estate_principal = principal
        return principal

    session = resolve_request_session(db, request)
    if session is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    subject_type = str(session.subject_type or "").strip().lower()
    subject_id = session.subject_id
    if subject_id is None and session.user_id is not None:
        subject_type = subject_type or "landcheck_user"
        subject_id = session.user_id
    if not subject_type or subject_id is None:
        raise HTTPException(status_code=403, detail="This authenticated account cannot access LandCheck Estates")
    principal = EstatePrincipal(subject_type, str(subject_id), session.display_name)
    request.state.estate_principal = principal
    return principal


def list_estate_access(db: Session, principal: EstatePrincipal) -> list[EstateAccess]:
    rows = (
        db.query(EstateOrganizationMember, EstateOrganization)
        .join(EstateOrganization, EstateOrganization.id == EstateOrganizationMember.organization_id)
        .filter(
            EstateOrganizationMember.subject_type == principal.subject_type,
            EstateOrganizationMember.subject_id == principal.subject_id,
            EstateOrganizationMember.is_active.is_(True),
            EstateOrganization.status == "active",
        )
        .order_by(EstateOrganization.name.asc())
        .all()
    )
    return [
        EstateAccess(
            organization_id=int(organization.id),
            organization_name=str(organization.name),
            organization_slug=str(organization.slug),
            role_key=str(member.role_key),
            principal=principal,
        )
        for member, organization in rows
    ]


def require_estate_access(
    db: Session,
    request: Request,
    organization_id: int,
    *,
    permission: str | None = None,
) -> EstateAccess:
    """Authorize a target organization selected by a trusted route/resource lookup.

    A request payload's organization ID is never a trust boundary: callers must pass the target
    resource's persisted organization ID here and this function derives membership from identity.
    """
    principal = resolve_estate_principal(db, request)
    access = next((item for item in list_estate_access(db, principal) if item.organization_id == int(organization_id)), None)
    if access is None:
        raise HTTPException(status_code=404, detail="Estate organization was not found")
    if permission and not has_permission(access.role_key, permission):
        raise HTTPException(status_code=403, detail="You do not have permission for this Estate action")
    return access
