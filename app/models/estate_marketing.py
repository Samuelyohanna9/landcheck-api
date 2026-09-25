from __future__ import annotations

import uuid

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.sql import func

from app.db_base import Base


class EstatePublicEvent(Base):
    """Lightweight, anonymous marketing signals from the public page (WhatsApp taps, shares,
    directions). No visitor identity is stored - only which estate/plot/campaign it came from."""

    __tablename__ = "estate_public_events"

    id = Column(Integer, primary_key=True)
    estate_id = Column(Integer, ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False)
    plot_id = Column(Integer, ForeignKey("estate_plots.id", ondelete="SET NULL"), nullable=True)
    event_type = Column(String(32), nullable=False)
    source_code = Column(String(120), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("event_type IN ('whatsapp_click', 'share_click', 'directions_click', 'guide_open')", name="ck_estate_public_event_type"),
        Index("ix_estate_public_events_estate_created", "estate_id", "created_at"),
    )


class EstateInspectionSlot(Base):
    """A scheduled group site visit that visitors can book a place on."""

    __tablename__ = "estate_inspection_slots"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    estate_id = Column(Integer, ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False)
    starts_at = Column(DateTime(timezone=True), nullable=False)
    duration_minutes = Column(Integer, nullable=False, default=120)
    capacity = Column(Integer, nullable=False, default=20)
    note = Column(Text, nullable=True)
    status = Column(String(16), nullable=False, default="open")
    staff_reminder_sent_at = Column(DateTime(timezone=True), nullable=True)
    created_by_subject_type = Column(String(64), nullable=False)
    created_by_subject_id = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('open', 'closed', 'cancelled')", name="ck_estate_inspection_slot_status"),
        CheckConstraint("capacity >= 1", name="ck_estate_inspection_slot_capacity"),
        Index("ix_estate_inspection_slots_estate_start", "estate_id", "starts_at"),
    )


class EstateInspectionBooking(Base):
    __tablename__ = "estate_inspection_bookings"

    id = Column(Integer, primary_key=True)
    booking_uid = Column(String(36), nullable=False, unique=True, default=lambda: str(uuid.uuid4()))
    manage_token_hash = Column(String(64), nullable=False, unique=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    estate_id = Column(Integer, ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False)
    slot_id = Column(Integer, ForeignKey("estate_inspection_slots.id", ondelete="CASCADE"), nullable=False)
    plot_id = Column(Integer, ForeignKey("estate_plots.id", ondelete="SET NULL"), nullable=True)
    full_name = Column(String(255), nullable=False)
    phone = Column(String(64), nullable=False)
    email = Column(String(255), nullable=True)
    party_size = Column(Integer, nullable=False, default=1)
    note = Column(Text, nullable=True)
    source_code = Column(String(120), nullable=True)
    source_channel = Column(String(64), nullable=True)
    assigned_agent_subject_type = Column(String(64), nullable=True)
    assigned_agent_subject_id = Column(String(128), nullable=True)
    status = Column(String(16), nullable=False, default="booked")
    staff_notes = Column(Text, nullable=True)
    reminder_24h_sent_at = Column(DateTime(timezone=True), nullable=True)
    reminder_2h_sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('booked', 'cancelled', 'attended', 'no_show')", name="ck_estate_inspection_booking_status"),
        CheckConstraint("party_size >= 1", name="ck_estate_inspection_booking_party"),
        Index("ix_estate_inspection_bookings_slot", "slot_id", "status"),
    )


class EstateProgressUpdate(Base):
    """A dated site update (photo / video / drone / milestone) shown on the public page. Location
    is checked against the plot or estate geometry at upload time and the result stored, so buyers
    can see whether the media was really captured on the land."""

    __tablename__ = "estate_progress_updates"

    id = Column(Integer, primary_key=True)
    update_uid = Column(String(36), nullable=False, unique=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    estate_id = Column(Integer, ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False)
    plot_id = Column(Integer, ForeignKey("estate_plots.id", ondelete="SET NULL"), nullable=True)
    kind = Column(String(16), nullable=False, default="photo")
    title = Column(String(255), nullable=False)
    body = Column(Text, nullable=True)
    media_object_key = Column(String(512), nullable=True)
    media_mime = Column(String(64), nullable=True)
    media_url = Column(String(1024), nullable=True)
    captured_at = Column(DateTime(timezone=True), nullable=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    location_accuracy_m = Column(Float, nullable=True)
    location_source = Column(String(16), nullable=False, default="none")
    verification = Column(String(16), nullable=False, default="unverified")
    distance_m = Column(Float, nullable=True)
    is_published = Column(Boolean, nullable=False, default=True)
    posted_by_subject_type = Column(String(64), nullable=False)
    posted_by_subject_id = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("kind IN ('photo', 'video', 'drone', 'milestone', 'note')", name="ck_estate_progress_kind"),
        CheckConstraint("location_source IN ('exif', 'device', 'none')", name="ck_estate_progress_location_source"),
        CheckConstraint("verification IN ('on_plot', 'on_estate', 'near_estate', 'outside', 'unverified')", name="ck_estate_progress_verification"),
        Index("ix_estate_progress_estate_created", "estate_id", "created_at"),
    )
