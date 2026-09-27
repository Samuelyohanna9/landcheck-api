from __future__ import annotations

"""The Data Processing Agreement each Estate company accepts, and the record of who did.

`DPA_VERSION` must be bumped together with docs/legal/DATA_PROCESSING_AGREEMENT.md and with the
copy the frontend renders (landcheck-web src/pages/estates/EstateLegalPage.tsx) whenever the
document changes materially - the same "keep two places in sync" pattern as billing_plans.py and
EstatePricingCards.tsx. Bumping it means every company sees "Accept the updated agreement" again,
since their existing acceptance is for the old version."""

from typing import Final

from sqlalchemy.orm import Session

from app.models.estate_legal import EstateDpaAcceptance

DPA_VERSION: Final[str] = "2026-09-27"


def latest_acceptance(db: Session, organization_id: int) -> EstateDpaAcceptance | None:
    return (
        db.query(EstateDpaAcceptance)
        .filter(EstateDpaAcceptance.organization_id == int(organization_id))
        .order_by(EstateDpaAcceptance.accepted_at.desc(), EstateDpaAcceptance.id.desc())
        .first()
    )


def has_accepted_current(db: Session, organization_id: int) -> bool:
    row = latest_acceptance(db, organization_id)
    return bool(row and row.version == DPA_VERSION)


def record_acceptance(
    db: Session,
    *,
    organization_id: int,
    subject_type: str,
    subject_id: str,
    name: str | None,
    email: str | None,
    ip_address: str | None,
) -> EstateDpaAcceptance:
    row = EstateDpaAcceptance(
        organization_id=organization_id,
        version=DPA_VERSION,
        accepted_by_subject_type=subject_type,
        accepted_by_subject_id=str(subject_id),
        accepted_by_name=(name or None),
        accepted_by_email=(email or None),
        ip_address=(ip_address or None),
    )
    db.add(row)
    db.flush()
    return row
