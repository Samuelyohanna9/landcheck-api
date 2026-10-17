from __future__ import annotations

import uuid

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text
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


class EstateLiveInspectionSession(Base):
    """A scheduled or in-progress live video walkthrough of one plot for one remote buyer - an
    agent on site joins a video call and streams their GPS position, verified against the plot's
    real boundary (see verify_location() in app/services/estates/marketing_field.py), so the
    buyer can see in real time whether the agent is actually standing on the plot being sold."""

    __tablename__ = "estate_live_inspection_sessions"

    id = Column(Integer, primary_key=True)
    session_uid = Column(String(36), nullable=False, unique=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    estate_id = Column(Integer, ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False)
    plot_id = Column(Integer, ForeignKey("estate_plots.id", ondelete="CASCADE"), nullable=False)
    customer_id = Column(Integer, ForeignKey("estate_customers.id", ondelete="SET NULL"), nullable=True)
    buyer_name = Column(String(255), nullable=False)
    buyer_phone = Column(String(64), nullable=True)
    buyer_email = Column(String(255), nullable=True)
    agent_member_id = Column(Integer, ForeignKey("estate_organization_members.id", ondelete="SET NULL"), nullable=True)
    status = Column(String(16), nullable=False, default="scheduled")
    # The agreed appointment time - purely informational/coordination, not enforced against
    # "go live" (an agent can still start early/late; this is what both emails show and what the
    # reschedule flow negotiates). proposed_times holds the buyer's candidate alternatives from
    # the reschedule page until staff approve one (which moves it into scheduled_at and clears
    # both reschedule fields).
    scheduled_at = Column(DateTime(timezone=True), nullable=True)
    proposed_times = Column(JSON, nullable=True)
    reschedule_requested_at = Column(DateTime(timezone=True), nullable=True)
    video_room_name = Column(String(255), nullable=True)
    video_room_url = Column(String(1024), nullable=True)
    last_lat = Column(Float, nullable=True)
    last_lng = Column(Float, nullable=True)
    last_accuracy_m = Column(Float, nullable=True)
    last_verification = Column(String(16), nullable=True)
    last_distance_m = Column(Float, nullable=True)
    last_location_at = Column(DateTime(timezone=True), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    created_by_subject_type = Column(String(64), nullable=False)
    created_by_subject_id = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('scheduled', 'live', 'ended', 'cancelled')", name="ck_estate_live_inspection_status"),
        CheckConstraint(
            "last_verification IS NULL OR last_verification IN ('on_plot', 'on_estate', 'near_estate', 'outside')",
            name="ck_estate_live_inspection_verification",
        ),
        Index("ix_estate_live_inspections_estate_status", "estate_id", "status"),
    )


class EstateLiveInspectionBuyerToken(Base):
    """A session can have more than one valid buyer link, unlike the other hashed-token tables in
    this codebase (customer/agent portal tokens) - the raw token is one-way hashed and never
    recoverable, so a later email (e.g. "your rescheduled time was approved") that wants to
    include a working link has to mint a fresh one rather than resend the original. Old links
    are never revoked, so a buyer's first invite email keeps working even after a reschedule."""

    __tablename__ = "estate_live_inspection_buyer_tokens"

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("estate_live_inspection_sessions.id", ondelete="CASCADE"), nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class EstateApprovalMilestone(Base):
    """Tracks a developer's progress through the government approvals a Nigerian estate needs
    (survey plan approval, layout approval, Governor's Consent, Certificate of Occupancy) -
    addresses buyers' fear of "approval delays" by making that progress visible instead of a
    black box. plot_id is nullable the same way EstateInspectionBooking/EstateProgressUpdate's
    is: null means the milestone applies to the whole estate (e.g. layout approval), set means
    it's plot-specific (e.g. one plot's own C of O). is_public mirrors the opt-in shape of
    Estate.public_development_forecast's "published" key - staff decide when a milestone is
    ready to show buyers, it isn't shown automatically just because it exists."""

    __tablename__ = "estate_approval_milestones"

    id = Column(Integer, primary_key=True)
    estate_id = Column(Integer, ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False)
    plot_id = Column(Integer, ForeignKey("estate_plots.id", ondelete="CASCADE"), nullable=True)
    milestone_type = Column(String(32), nullable=False)
    status = Column(String(16), nullable=False, default="not_started")
    reference_number = Column(String(120), nullable=True)
    note = Column(Text, nullable=True)
    is_public = Column(Boolean, nullable=False, default=False)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    created_by_subject_type = Column(String(64), nullable=False)
    created_by_subject_id = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint(
            "milestone_type IN ('survey_plan_approval', 'layout_approval', 'governors_consent', 'certificate_of_occupancy')",
            name="ck_estate_approval_milestone_type",
        ),
        CheckConstraint(
            "status IN ('not_started', 'submitted', 'under_review', 'approved', 'rejected')",
            name="ck_estate_approval_milestone_status",
        ),
        Index("ix_estate_approval_milestones_estate_plot", "estate_id", "plot_id"),
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
