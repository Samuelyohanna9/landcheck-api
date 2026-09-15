from __future__ import annotations

"""Sales-agent commission tracking: a per-organization tier ladder (rate keyed off an agent's
cumulative Allocated sales volume), applied to one sale at a time.

Modeled directly on how Nigerian real estate firms commonly run this - a single-level, volume-
tiered commission (e.g. 5% until N40m in verified sales, 7.5% until N80m, 10% beyond), NOT a
multi-level/recruit-a-downline structure. The rate for a given sale is resolved from the agent's
volume *before* that sale and locked in at that moment - later edits to the tier table never
rewrite a commission already earned on a past sale.
"""

from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.estate_foundation import EstateAllocation, EstateCommissionTier

# Used only when an organization hasn't configured its own tiers yet - a starting point drawn
# from real published Nigerian agency tier structures, not a statutory rate.
DEFAULT_TIERS: list[dict] = [
    {"label": "Tier 1", "min_cumulative_sales": Decimal("0"), "rate_percent": Decimal("5")},
    {"label": "Tier 2", "min_cumulative_sales": Decimal("40000000"), "rate_percent": Decimal("7.5")},
    {"label": "Tier 3", "min_cumulative_sales": Decimal("80000000"), "rate_percent": Decimal("10")},
]


def get_commission_tiers(db: Session, organization_id: int) -> list[EstateCommissionTier]:
    return (
        db.query(EstateCommissionTier)
        .filter(EstateCommissionTier.organization_id == organization_id)
        .order_by(EstateCommissionTier.min_cumulative_sales.asc())
        .all()
    )


def _tier_dicts(rows: list[EstateCommissionTier]) -> list[dict]:
    if not rows:
        return DEFAULT_TIERS
    return [{"label": row.label, "min_cumulative_sales": Decimal(row.min_cumulative_sales), "rate_percent": Decimal(row.rate_percent)} for row in rows]


def resolve_tier(tiers: list[dict], cumulative_volume: Decimal) -> dict:
    ordered = sorted(tiers, key=lambda tier: tier["min_cumulative_sales"])
    applicable = ordered[0]
    for tier in ordered:
        if cumulative_volume >= tier["min_cumulative_sales"]:
            applicable = tier
    return applicable


def cumulative_sales_before(db: Session, *, organization_id: int, subject_type: str, subject_id: str, exclude_allocation_id: int | None = None) -> Decimal:
    """Sum of agreed_price across this agent's other fully Allocated sales - "before" in the sense
    that the allocation currently being priced is excluded, not a point-in-time historical cutoff."""
    query = db.query(func.coalesce(func.sum(EstateAllocation.agreed_price), 0)).filter(
        EstateAllocation.organization_id == organization_id,
        EstateAllocation.sales_agent_subject_type == subject_type,
        EstateAllocation.sales_agent_subject_id == subject_id,
        EstateAllocation.status == "allocated",
    )
    if exclude_allocation_id is not None:
        query = query.filter(EstateAllocation.id != exclude_allocation_id)
    return Decimal(query.scalar() or 0)


def apply_commission(db: Session, *, allocation: EstateAllocation) -> None:
    """Call this once an allocation has actually reached "allocated" (fully paid) status - not on
    every save. No-op if there's no agent tagged or no agreed price to compute a commission from."""
    if not allocation.sales_agent_subject_id or not allocation.sales_agent_subject_type or not allocation.agreed_price:
        return
    tiers = _tier_dicts(get_commission_tiers(db, allocation.organization_id))
    prior_volume = cumulative_sales_before(
        db,
        organization_id=allocation.organization_id,
        subject_type=allocation.sales_agent_subject_type,
        subject_id=allocation.sales_agent_subject_id,
        exclude_allocation_id=allocation.id,
    )
    tier = resolve_tier(tiers, prior_volume)
    allocation.commission_tier_label = tier["label"]
    allocation.commission_rate_percent = tier["rate_percent"]
    allocation.commission_amount = (Decimal(allocation.agreed_price) * tier["rate_percent"] / Decimal(100)).quantize(Decimal("0.01"))
