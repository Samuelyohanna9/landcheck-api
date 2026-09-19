"""Harden Estate trials and subscription charge state handling."""

from alembic import op
import sqlalchemy as sa


revision = "20260919_0024"
down_revision = "20260916_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("estate_accounts", sa.Column("trial_claimed_at", sa.DateTime(timezone=True), nullable=True))

    # Existing trial subscriptions prove that the organization's first trial was already used.
    # This also protects accounts created before the one-trial field was introduced.
    op.execute(
        sa.text(
            """
            UPDATE estate_accounts
            SET trial_claimed_at = COALESCE(
                (SELECT s.trial_ends_at
                 FROM estate_subscriptions s
                 WHERE s.organization_id = estate_accounts.organization_id
                   AND s.trial_ends_at IS NOT NULL
                 ORDER BY s.id
                 LIMIT 1),
                created_at
            )
            WHERE trial_claimed_at IS NULL
              AND EXISTS (
                  SELECT 1
                  FROM estate_subscriptions s
                  WHERE s.organization_id = estate_accounts.organization_id
                    AND s.trial_ends_at IS NOT NULL
              )
            """
        )
    )

    with op.batch_alter_table("estate_subscription_charges", recreate="always") as batch_op:
        batch_op.drop_constraint("ck_estate_subscription_charges_status", type_="check")
        batch_op.create_check_constraint(
            "ck_estate_subscription_charges_status",
            "status IN ('pending', 'success', 'failed')",
        )


def downgrade() -> None:
    with op.batch_alter_table("estate_subscription_charges", recreate="always") as batch_op:
        batch_op.drop_constraint("ck_estate_subscription_charges_status", type_="check")
        batch_op.create_check_constraint(
            "ck_estate_subscription_charges_status",
            "status IN ('success', 'failed')",
        )
    op.drop_column("estate_accounts", "trial_claimed_at")
