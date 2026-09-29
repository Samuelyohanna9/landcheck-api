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
    auto_posting: bool
    soil_analysis: bool
    max_estates: int | None  # None = unlimited


ESTATE_PLANS: Final[dict[str, EstatePlanDefinition]] = {
    "basic": {
        "label": "Basic",
        "monthly": Decimal("19500"),
        "yearly": Decimal("220000"),
        "hazard_analysis": False,
        "auto_posting": False,
        "soil_analysis": False,
        "max_estates": 1,
    },
    "plus": {
        "label": "Plus",
        "monthly": Decimal("24500"),
        "yearly": Decimal("285000"),
        "hazard_analysis": True,
        "auto_posting": False,
        "soil_analysis": False,
        "max_estates": 3,
    },
    "pro": {
        "label": "Pro",
        "monthly": Decimal("48500"),
        "yearly": Decimal("533500"),  # 11 months for the price of 12
        "hazard_analysis": True,
        "auto_posting": True,
        "soil_analysis": True,
        "max_estates": 6,
    },
    "enterprise": {
        "label": "Enterprise",
        "monthly": Decimal("145000"),
        "yearly": Decimal("1595000"),  # 11 months for the price of 12
        "hazard_analysis": True,
        "auto_posting": True,
        "soil_analysis": True,
        "max_estates": None,
    },
}

PLAN_ORDER: Final[tuple[str, ...]] = ("basic", "plus", "pro", "enterprise")

# The plan a feature first appears on, used in "upgrade to ..." messages.
def plan_label(plan_key: str | None) -> str:
    return str(ESTATE_PLANS.get(str(plan_key or ""), {}).get("label") or "Basic")


def plan_max_estates(plan_key: str | None) -> int | None:
    plan = ESTATE_PLANS.get(str(plan_key or ""))
    return plan["max_estates"] if plan else 1


def plan_includes_auto_posting(plan_key: str) -> bool:
    return bool(ESTATE_PLANS.get(plan_key, {}).get("auto_posting", False))


def next_plan_with_estates(plan_key: str | None) -> str | None:
    """The next plan up that manages more estates than this one (for the upgrade prompt)."""
    current = plan_max_estates(plan_key)
    if current is None:
        return None
    for key in PLAN_ORDER:
        limit = ESTATE_PLANS[key]["max_estates"]
        if limit is None or limit > current:
            return key
    return None

ACTIVE_SUBSCRIPTION_STATUSES: Final[frozenset[str]] = frozenset({"trialing", "active"})


def plan_amount(plan_key: str, billing_cycle: str) -> Decimal:
    plan = ESTATE_PLANS[plan_key]
    return plan["monthly"] if billing_cycle == "monthly" else plan["yearly"]


def plan_includes_hazard_analysis(plan_key: str) -> bool:
    return bool(ESTATE_PLANS.get(plan_key, {}).get("hazard_analysis", False))


def plan_includes_soil_analysis(plan_key: str) -> bool:
    return bool(ESTATE_PLANS.get(plan_key, {}).get("soil_analysis", False))
