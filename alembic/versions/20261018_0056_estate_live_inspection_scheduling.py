"""Estate live inspection scheduling, reschedule requests, and multi-use buyer tokens."""

from alembic import op
import sqlalchemy as sa


revision = "20261018_0056"
down_revision = "20261017_0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("estate_live_inspection_sessions", sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("estate_live_inspection_sessions", sa.Column("proposed_times", sa.JSON(), nullable=True))
    op.add_column("estate_live_inspection_sessions", sa.Column("reschedule_requested_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "estate_live_inspection_buyer_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("estate_live_inspection_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_estate_live_inspection_buyer_tokens_session", "estate_live_inspection_buyer_tokens", ["session_id"])

    # Carry each session's existing single token over into the new table so already-sent invite
    # links keep working, then drop the old column.
    op.execute(
        """
        INSERT INTO estate_live_inspection_buyer_tokens (session_id, token_hash, created_at)
        SELECT id, buyer_token_hash, created_at FROM estate_live_inspection_sessions
        WHERE buyer_token_hash IS NOT NULL
        """
    )
    op.drop_column("estate_live_inspection_sessions", "buyer_token_hash")


def downgrade() -> None:
    op.add_column("estate_live_inspection_sessions", sa.Column("buyer_token_hash", sa.String(64), nullable=True))
    op.execute(
        """
        UPDATE estate_live_inspection_sessions s
        SET buyer_token_hash = t.token_hash
        FROM (
            SELECT DISTINCT ON (session_id) session_id, token_hash
            FROM estate_live_inspection_buyer_tokens
            ORDER BY session_id, created_at ASC
        ) t
        WHERE t.session_id = s.id
        """
    )
    op.alter_column("estate_live_inspection_sessions", "buyer_token_hash", nullable=False)
    op.create_unique_constraint("uq_estate_live_inspection_sessions_buyer_token_hash", "estate_live_inspection_sessions", ["buyer_token_hash"])
    op.drop_index("ix_estate_live_inspection_buyer_tokens_session", table_name="estate_live_inspection_buyer_tokens")
    op.drop_table("estate_live_inspection_buyer_tokens")
    op.drop_column("estate_live_inspection_sessions", "reschedule_requested_at")
    op.drop_column("estate_live_inspection_sessions", "proposed_times")
    op.drop_column("estate_live_inspection_sessions", "scheduled_at")
