"""Estate Pro and Enterprise subscription plans."""

from alembic import op


revision = "20260928_0037"
down_revision = "20260927_0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_estate_subscriptions_plan", "estate_subscriptions", type_="check")
    op.create_check_constraint("ck_estate_subscriptions_plan", "estate_subscriptions", "plan_key IN ('basic', 'plus', 'pro', 'enterprise')")


def downgrade() -> None:
    op.execute("UPDATE estate_subscriptions SET plan_key = 'plus' WHERE plan_key IN ('pro', 'enterprise')")
    op.drop_constraint("ck_estate_subscriptions_plan", "estate_subscriptions", type_="check")
    op.create_check_constraint("ck_estate_subscriptions_plan", "estate_subscriptions", "plan_key IN ('basic', 'plus')")
