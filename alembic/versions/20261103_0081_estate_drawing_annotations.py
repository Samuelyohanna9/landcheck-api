"""Add Estate drawing annotations (calibration, live measurement, markup pins on a drawing)."""

from alembic import op
import sqlalchemy as sa


revision = "20261103_0081"
down_revision = "20261103_0080"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "estate_drawing_annotations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("estate_documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=True),
        sa.Column("created_by_subject_type", sa.String(length=64), nullable=False),
        sa.Column("created_by_subject_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("kind IN ('calibration', 'measurement', 'markup')", name="ck_estate_drawing_annotations_kind"),
    )
    op.create_index("ix_estate_drawing_annotations_document", "estate_drawing_annotations", ["document_id"])


def downgrade() -> None:
    op.drop_table("estate_drawing_annotations")
