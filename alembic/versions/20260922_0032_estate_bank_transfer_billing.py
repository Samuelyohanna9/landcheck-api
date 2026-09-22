"""Support bank-transfer subscriptions and scheduled billing reminders."""

from alembic import op
import sqlalchemy as sa


revision = "20260922_0032"
down_revision = "20260921_0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "estate_subscriptions",
        sa.Column("payment_method", sa.String(length=24), nullable=False, server_default="card"),
    )
    op.add_column("estate_subscriptions", sa.Column("trial_reminder_sent_for", sa.DateTime(timezone=True), nullable=True))
    op.add_column("estate_subscriptions", sa.Column("renewal_reminder_sent_for", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_estate_subscriptions_payment_method",
        "estate_subscriptions",
        "payment_method IN ('card', 'bank_transfer')",
    )
    op.alter_column("estate_subscriptions", "payment_method", server_default=None)


def downgrade() -> None:
    op.drop_constraint("ck_estate_subscriptions_payment_method", "estate_subscriptions", type_="check")
    op.drop_column("estate_subscriptions", "renewal_reminder_sent_for")
    op.drop_column("estate_subscriptions", "trial_reminder_sent_for")
    op.drop_column("estate_subscriptions", "payment_method")
