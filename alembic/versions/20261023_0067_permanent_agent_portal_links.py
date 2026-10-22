"""Make agent workspace links permanent until revoked."""

from alembic import op
import sqlalchemy as sa


revision = "20261023_0067"
down_revision = "20261021_0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "estate_agent_portal_tokens",
        "expires_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
    )
    # Preserve existing active links instead of forcing every agent to receive a new URL.
    op.execute(
        sa.text(
            "UPDATE estate_agent_portal_tokens "
            "SET expires_at = NULL "
            "WHERE revoked_at IS NULL"
        )
    )


def downgrade() -> None:
    # A downgrade restores a finite validity window before making the column required again.
    op.execute(
        sa.text(
            "UPDATE estate_agent_portal_tokens "
            "SET expires_at = CURRENT_TIMESTAMP + INTERVAL '365 days' "
            "WHERE expires_at IS NULL"
        )
    )
    op.alter_column(
        "estate_agent_portal_tokens",
        "expires_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )
