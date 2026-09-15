from __future__ import annotations

from decimal import Decimal
from typing import Final, TypedDict


TRIAL_DAYS: Final[int] = 3

# The ₦50 verification charge exists purely to get a tokenizable card through Flutterwave's
# hosted checkout during the free trial - it is refunded immediately once the token is captured,
# so it never actually costs the customer anything. See estate_flutterwave.py / subscriptions.py.
VERIFICATION_CHARGE_AMOUNT: Final[Decimal] = Decimal("50")


class EstatePlanDefinition(TypedDict):
    label: str
    monthly: Decimal
    yearly: Decimal
    hazard_analysis: bool


ESTATE_PLANS: Final[dict[str, EstatePlanDefinition]] = {
    "basic": {
        "label": "Basic",
        "monthly": Decimal("19500"),
        "yearly": Decimal("220000"),
        "hazard_analysis": False,
    },
    "plus": {
        "label": "Plus",
        "monthly": Decimal("24500"),
        "yearly": Decimal("285000"),
        "hazard_analysis": True,
    },
}

ACTIVE_SUBSCRIPTION_STATUSES: Final[frozenset[str]] = frozenset({"trialing", "active"})


def plan_amount(plan_key: str, billing_cycle: str) -> Decimal:
    plan = ESTATE_PLANS[plan_key]
    return plan["monthly"] if billing_cycle == "monthly" else plan["yearly"]


def plan_includes_hazard_analysis(plan_key: str) -> bool:
    return bool(ESTATE_PLANS.get(plan_key, {}).get("hazard_analysis", False))
