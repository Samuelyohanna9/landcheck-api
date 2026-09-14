from __future__ import annotations

import os
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.estate_foundation import EstateOrganizationEntitlement


ESTATE_FEATURES = frozenset(
    {
        "ESTATES_ENABLED",
        "ESTATE_AI_DIGITIZATION",
        "ESTATE_SITE_INTELLIGENCE",
        "ESTATE_PUBLIC_MAP",
        "ESTATE_AI_ASSISTANT",
    }
)


@dataclass(frozen=True, slots=True)
class EstateEntitlement:
    feature_key: str
    is_enabled: bool
    limit_value: int | None


def _env_enabled(feature_key: str) -> bool:
    value = os.getenv(feature_key)
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def get_estate_entitlement(db: Session, organization_id: int, feature_key: str) -> EstateEntitlement:
    normalized = str(feature_key or "").strip().upper()
    if normalized not in ESTATE_FEATURES:
        raise ValueError("Unknown Estate feature")
    if not _env_enabled(normalized):
        return EstateEntitlement(normalized, False, None)
    record = (
        db.query(EstateOrganizationEntitlement)
        .filter(
            EstateOrganizationEntitlement.organization_id == int(organization_id),
            EstateOrganizationEntitlement.feature_key == normalized,
        )
        .one_or_none()
    )
    if record is None:
        return EstateEntitlement(normalized, True, None)
    return EstateEntitlement(normalized, bool(record.is_enabled), record.limit_value)
