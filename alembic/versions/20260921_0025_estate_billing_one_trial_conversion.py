"""Guarantee a subscription can only ever have one trial_conversion charge row."""

from alembic import op
import sqlalchemy as sa


revision = "20260921_0025"
down_revision = "20260919_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A scheduler race (or any other bug) may already have recorded more than one
    # trial_conversion charge for the same subscription. Keep the earliest one - the real
    # trial-conversion attempt - and relabel any later ones as dunning retries, which is what
    # they actually are, before the new unique index below makes that state unrepresentable.
    op.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT id, row_number() OVER (
                    PARTITION BY subscription_id ORDER BY attempted_at ASC, id ASC
                ) AS rn
                FROM estate_subscription_charges
                WHERE charge_type = 'trial_conversion'
            )
            UPDATE estate_subscription_charges
            SET charge_type = 'retry'
            WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
            """
        )
    )

    op.create_index(
        "ux_estate_subscription_charges_one_trial_conversion",
        "estate_subscription_charges",
        ["subscription_id"],
        unique=True,
        postgresql_where=sa.text("charge_type = 'trial_conversion'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ux_estate_subscription_charges_one_trial_conversion",
        table_name="estate_subscription_charges",
    )
