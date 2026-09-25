"""Estate marketing: WhatsApp/meeting point, lead follow-up, public events, inspections, progress feed."""

from alembic import op
import sqlalchemy as sa


revision = "20260925_0034"
down_revision = "20260924_0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("estate_estates", sa.Column("public_whatsapp_number", sa.String(32), nullable=True))
    op.add_column("estate_estates", sa.Column("public_meeting_point", sa.JSON(), nullable=True))
    op.add_column("estate_public_reservation_requests", sa.Column("follow_up_reminder_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("estate_public_reservation_requests", sa.Column("last_follow_up_reminder_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "estate_public_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("estate_id", sa.Integer(), sa.ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plot_id", sa.Integer(), sa.ForeignKey("estate_plots.id", ondelete="SET NULL"), nullable=True),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("source_code", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("event_type IN ('whatsapp_click', 'share_click', 'directions_click', 'guide_open')", name="ck_estate_public_event_type"),
    )
    op.create_index("ix_estate_public_events_estate_created", "estate_public_events", ["estate_id", "created_at"])

    op.create_table(
        "estate_inspection_slots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("estate_id", sa.Integer(), sa.ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False, server_default="120"),
        sa.Column("capacity", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("staff_reminder_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_subject_type", sa.String(64), nullable=False),
        sa.Column("created_by_subject_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('open', 'closed', 'cancelled')", name="ck_estate_inspection_slot_status"),
        sa.CheckConstraint("capacity >= 1", name="ck_estate_inspection_slot_capacity"),
    )
    op.create_index("ix_estate_inspection_slots_estate_start", "estate_inspection_slots", ["estate_id", "starts_at"])

    op.create_table(
        "estate_inspection_bookings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("booking_uid", sa.String(36), nullable=False, unique=True),
        sa.Column("manage_token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("estate_id", sa.Integer(), sa.ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("slot_id", sa.Integer(), sa.ForeignKey("estate_inspection_slots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plot_id", sa.Integer(), sa.ForeignKey("estate_plots.id", ondelete="SET NULL"), nullable=True),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column("phone", sa.String(64), nullable=False),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("party_size", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("source_code", sa.String(120), nullable=True),
        sa.Column("source_channel", sa.String(64), nullable=True),
        sa.Column("assigned_agent_subject_type", sa.String(64), nullable=True),
        sa.Column("assigned_agent_subject_id", sa.String(128), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="booked"),
        sa.Column("staff_notes", sa.Text(), nullable=True),
        sa.Column("reminder_24h_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reminder_2h_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('booked', 'cancelled', 'attended', 'no_show')", name="ck_estate_inspection_booking_status"),
        sa.CheckConstraint("party_size >= 1", name="ck_estate_inspection_booking_party"),
    )
    op.create_index("ix_estate_inspection_bookings_slot", "estate_inspection_bookings", ["slot_id", "status"])

    op.create_table(
        "estate_progress_updates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("update_uid", sa.String(36), nullable=False, unique=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("estate_id", sa.Integer(), sa.ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plot_id", sa.Integer(), sa.ForeignKey("estate_plots.id", ondelete="SET NULL"), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False, server_default="photo"),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("media_object_key", sa.String(512), nullable=True),
        sa.Column("media_mime", sa.String(64), nullable=True),
        sa.Column("media_url", sa.String(1024), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("location_accuracy_m", sa.Float(), nullable=True),
        sa.Column("location_source", sa.String(16), nullable=False, server_default="none"),
        sa.Column("verification", sa.String(16), nullable=False, server_default="unverified"),
        sa.Column("distance_m", sa.Float(), nullable=True),
        sa.Column("is_published", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("posted_by_subject_type", sa.String(64), nullable=False),
        sa.Column("posted_by_subject_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("kind IN ('photo', 'video', 'drone', 'milestone', 'note')", name="ck_estate_progress_kind"),
        sa.CheckConstraint("location_source IN ('exif', 'device', 'none')", name="ck_estate_progress_location_source"),
        sa.CheckConstraint("verification IN ('on_plot', 'on_estate', 'near_estate', 'outside', 'unverified')", name="ck_estate_progress_verification"),
    )
    op.create_index("ix_estate_progress_estate_created", "estate_progress_updates", ["estate_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_estate_progress_estate_created", table_name="estate_progress_updates")
    op.drop_table("estate_progress_updates")
    op.drop_index("ix_estate_inspection_bookings_slot", table_name="estate_inspection_bookings")
    op.drop_table("estate_inspection_bookings")
    op.drop_index("ix_estate_inspection_slots_estate_start", table_name="estate_inspection_slots")
    op.drop_table("estate_inspection_slots")
    op.drop_index("ix_estate_public_events_estate_created", table_name="estate_public_events")
    op.drop_table("estate_public_events")
    op.drop_column("estate_public_reservation_requests", "last_follow_up_reminder_at")
    op.drop_column("estate_public_reservation_requests", "follow_up_reminder_count")
    op.drop_column("estate_estates", "public_meeting_point")
    op.drop_column("estate_estates", "public_whatsapp_number")
