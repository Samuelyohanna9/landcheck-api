"""Protect retryable estate create actions from duplicate records.

Revision ID: 20260921_0031
Revises: 20260921_0030
"""

from alembic import op
import sqlalchemy as sa


revision = "20260921_0031"
down_revision = "20260921_0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("estate_public_reservation_requests", sa.Column("idempotency_key", sa.String(length=128), nullable=True))
    op.add_column("estate_customers", sa.Column("idempotency_key", sa.String(length=128), nullable=True))
    op.create_index(
        "uq_estate_public_reservation_idempotency",
        "estate_public_reservation_requests",
        ["organization_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index(
        "uq_estate_customer_idempotency",
        "estate_customers",
        ["organization_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index(
        "uq_estate_survey_active_plot",
        "estate_survey_requests",
        ["plot_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('requested', 'assigned', 'in_progress', 'ready_for_review', 'approved', 'failed')"),
    )
    op.create_index(
        "uq_estate_staking_active_survey",
        "estate_staking_tasks",
        ["survey_request_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'assigned', 'in_progress')"),
    )


def downgrade() -> None:
    op.drop_index("uq_estate_staking_active_survey", table_name="estate_staking_tasks")
    op.drop_index("uq_estate_survey_active_plot", table_name="estate_survey_requests")
    op.drop_index("uq_estate_customer_idempotency", table_name="estate_customers")
    op.drop_index("uq_estate_public_reservation_idempotency", table_name="estate_public_reservation_requests")
    op.drop_column("estate_customers", "idempotency_key")
    op.drop_column("estate_public_reservation_requests", "idempotency_key")
