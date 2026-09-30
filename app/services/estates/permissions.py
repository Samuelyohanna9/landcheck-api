from __future__ import annotations

from typing import Final


ESTATE_ROLES: Final[frozenset[str]] = frozenset(
    {"owner", "manager", "accounts", "surveyor", "field_officer", "sales", "marketer", "viewer"}
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
            "marketing.manage",
        }
    ),
    "accounts": frozenset({"estate.read", "plot.read", "customer.read", "allocation.read", "payment.read", "payment.manage", "document.read", "document.manage", "report.read", "audit.read"}),
    "surveyor": frozenset({"estate.read", "plot.read", "survey.read", "survey.manage", "staking.read", "staking.manage", "document.read", "document.manage", "audit.read"}),
    "field_officer": frozenset({"estate.read", "plot.read", "staking.read", "field.read", "field.manage", "document.read", "document.manage"}),
    "sales": frozenset({"estate.read", "plot.read", "customer.read", "customer.manage", "allocation.read", "allocation.manage", "payment.read", "payment.manage", "document.read", "document.manage"}),
    "marketer": frozenset({"estate.read", "plot.read", "customer.read", "customer.manage", "allocation.read", "allocation.manage", "payment.read", "document.read", "marketing.manage"}),
    "viewer": frozenset({"estate.read", "plot.read", "customer.read", "allocation.read", "document.read", "survey.read", "staking.read", "field.read", "infrastructure.read", "report.read"}),
}


def has_permission(role_key: str, permission: str) -> bool:
    permissions = ROLE_PERMISSIONS.get(str(role_key or "").strip().lower(), frozenset())
    return "*" in permissions or str(permission or "").strip().lower() in permissions


# The full, real catalog of every permission key actually enforced somewhere in the Estates
# backend (grep the routers for `permission="..."` to re-derive this list if it ever drifts) -
# grouped for the "Add Access" staff checklist UI. A custom staff role/member is granted an
# explicit subset of these keys instead of one of the fixed roles above.
PERMISSION_CATALOG: Final[list[dict]] = [
    {"group": "Estate & settings", "items": [
        {"key": "estate.read", "label": "View estate details"},
        {"key": "estate.manage", "label": "Manage estate details, public site & settings"},
    ]},
    {"group": "Plots, map, hazard & soil analysis", "items": [
        {"key": "plot.read", "label": "View plots, map, hazard & soil analysis results"},
        {"key": "plot.manage", "label": "Manage plots & run hazard/soil analysis"},
    ]},
    {"group": "Customers", "items": [
        {"key": "customer.read", "label": "View customers"},
        {"key": "customer.manage", "label": "Manage customers"},
    ]},
    {"group": "Allocations & sales", "items": [
        {"key": "allocation.read", "label": "View allocations, sales & commissions"},
        {"key": "allocation.manage", "label": "Manage allocations & sales"},
    ]},
    {"group": "Payments", "items": [
        {"key": "payment.read", "label": "View payments"},
        {"key": "payment.manage", "label": "Manage payments"},
    ]},
    {"group": "Documents", "items": [
        {"key": "document.read", "label": "View documents"},
        {"key": "document.manage", "label": "Manage documents"},
    ]},
    {"group": "Survey requests", "items": [
        {"key": "survey.read", "label": "View survey requests"},
        {"key": "survey.manage", "label": "Manage survey requests"},
    ]},
    {"group": "Staking", "items": [
        {"key": "staking.read", "label": "View staking tasks"},
        {"key": "staking.manage", "label": "Manage staking tasks"},
    ]},
    {"group": "Field inspections", "items": [
        {"key": "field.read", "label": "View field inspections"},
        {"key": "field.manage", "label": "Manage field inspections"},
    ]},
    {"group": "Infrastructure & development", "items": [
        {"key": "infrastructure.read", "label": "View infrastructure & development forecast"},
        {"key": "infrastructure.manage", "label": "Manage infrastructure & development forecast"},
    ]},
    {"group": "Reports", "items": [
        {"key": "report.read", "label": "View reports"},
    ]},
    {"group": "Audit timeline", "items": [
        {"key": "audit.read", "label": "View audit timeline"},
    ]},
    {"group": "Marketing & social posts", "items": [
        {"key": "marketing.manage", "label": "Manage marketing & social posts"},
    ]},
    {"group": "Billing", "items": [
        {"key": "billing.manage", "label": "Manage billing & subscription"},
    ]},
]

ALL_PERMISSION_KEYS: Final[frozenset[str]] = frozenset(
    item["key"] for group in PERMISSION_CATALOG for item in group["items"]
)


def sanitize_permissions(keys) -> list[str]:
    """Drop anything that isn't a real, currently-enforced permission key - never persist a
    typo'd or stale key a staff checklist submitted, since it would silently grant nothing."""
    if not keys:
        return []
    seen: list[str] = []
    for key in keys:
        normalized = str(key or "").strip().lower()
        if normalized in ALL_PERMISSION_KEYS and normalized not in seen:
            seen.append(normalized)
    return seen
