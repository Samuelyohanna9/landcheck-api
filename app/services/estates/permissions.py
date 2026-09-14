from __future__ import annotations

from typing import Final


ESTATE_ROLES: Final[frozenset[str]] = frozenset(
    {"owner", "manager", "accounts", "surveyor", "field_officer", "sales", "viewer"}
)

# Permission keys are deliberately broader than Phase 0 routes. Future modules must use these
# server-side checks rather than inventing route-local role checks.
ROLE_PERMISSIONS: Final[dict[str, frozenset[str]]] = {
    "owner": frozenset({"*"}),
    "manager": frozenset(
        {
            "estate.read",
            "estate.manage",
            "plot.read",
            "plot.manage",
            "customer.read",
            "customer.manage",
            "allocation.read",
            "allocation.manage",
            "payment.read",
            "payment.manage",
            "document.read",
            "document.manage",
            "survey.read",
            "survey.manage",
            "staking.read",
            "staking.manage",
            "field.read",
            "field.manage",
            "infrastructure.read",
            "infrastructure.manage",
            "report.read",
            "audit.read",
        }
    ),
    "accounts": frozenset({"estate.read", "plot.read", "customer.read", "allocation.read", "payment.read", "payment.manage", "document.read", "document.manage", "report.read", "audit.read"}),
    "surveyor": frozenset({"estate.read", "plot.read", "survey.read", "survey.manage", "staking.read", "staking.manage", "document.read", "document.manage", "audit.read"}),
    "field_officer": frozenset({"estate.read", "plot.read", "staking.read", "field.read", "field.manage", "document.read", "document.manage"}),
    "sales": frozenset({"estate.read", "plot.read", "customer.read", "customer.manage", "allocation.read", "allocation.manage", "payment.read", "payment.manage", "document.read", "document.manage"}),
    "viewer": frozenset({"estate.read", "plot.read", "customer.read", "allocation.read", "document.read", "survey.read", "staking.read", "field.read", "infrastructure.read", "report.read"}),
}


def has_permission(role_key: str, permission: str) -> bool:
    permissions = ROLE_PERMISSIONS.get(str(role_key or "").strip().lower(), frozenset())
    return "*" in permissions or str(permission or "").strip().lower() in permissions
