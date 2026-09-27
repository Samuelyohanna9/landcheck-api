from __future__ import annotations

import uuid

from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.sql import func

from app.db_base import Base

POST_STATUSES = ("draft", "scheduled", "publishing", "published", "partial", "failed", "cancelled", "skipped")


class EstateSocialAccount(Base):
    """A Facebook Page or Instagram Business account a company connected so LandCheck can post to it.
    The access token is stored encrypted and is never returned by any endpoint."""

    __tablename__ = "estate_social_accounts"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    provider = Column(String(16), nullable=False)  # facebook | instagram
    external_id = Column(String(64), nullable=False)
    name = Column(String(255), nullable=False)
    username = Column(String(120), nullable=True)
    linked_page_id = Column(String(64), nullable=True)  # for Instagram: the Facebook Page it hangs off
    facebook_user_id = Column(String(64), nullable=True)  # who granted access - needed to honour Meta data-deletion requests
    access_token_enc = Column(Text, nullable=False)
    token_expires_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(16), nullable=False, default="active")  # active | needs_reconnect | revoked
    connected_by_subject_type = Column(String(64), nullable=True)
    connected_by_subject_id = Column(String(128), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("organization_id", "provider", "external_id", name="uq_estate_social_account"),
        CheckConstraint("provider IN ('facebook', 'instagram')", name="ck_estate_social_provider"),
    )


class EstateSocialPlan(Base):
    """A content plan: several posts written and scheduled automatically (for example one a day for a week),
    optionally repeating so the schedule never runs dry."""

    __tablename__ = "estate_social_plans"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    estate_id = Column(Integer, ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(120), nullable=False)
    status = Column(String(16), nullable=False, default="active")  # active | paused | ended
    per_day = Column(Integer, nullable=True)
    per_week = Column(Integer, nullable=True)
    times = Column(JSON, nullable=False, default=list)  # ["09:00", "19:00"] in Lagos time
    channels = Column(JSON, nullable=False, default=list)
    style = Column(String(16), nullable=False, default="mixed")  # promo | luxury | mixed
    tone = Column(String(16), nullable=False, default="friendly")  # friendly | professional | urgent
    duration_days = Column(Integer, nullable=False, default=7)
    auto_renew = Column(Boolean, nullable=False, default=False)
    source_code = Column(String(120), nullable=True)
    cursor = Column(Integer, nullable=False, default=0)  # where the content rotation stands, so renewals continue it
    start_date = Column(Date, nullable=True)
    created_by_subject_type = Column(String(64), nullable=False)
    created_by_subject_id = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('active', 'paused', 'ended')", name="ck_estate_social_plan_status"),
        Index("ix_estate_social_plans_estate", "estate_id", "status"),
    )


class EstateSocialPost(Base):
    """One marketing post: a caption plus a generated image, sent to one or more channels now or later."""

    __tablename__ = "estate_social_posts"

    id = Column(Integer, primary_key=True)
    post_uid = Column(String(36), nullable=False, unique=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    estate_id = Column(Integer, ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False)
    plot_id = Column(Integer, ForeignKey("estate_plots.id", ondelete="SET NULL"), nullable=True)
    plan_id = Column(Integer, ForeignKey("estate_social_plans.id", ondelete="SET NULL"), nullable=True)
    auto_caption = Column(Boolean, nullable=False, default=False)  # rewritten from live data just before posting
    variant = Column(Integer, nullable=False, default=0)
    template_key = Column(String(40), nullable=False, default="custom")
    caption = Column(Text, nullable=False)
    image_format = Column(String(16), nullable=False, default="post")
    image_style = Column(String(16), nullable=False, default="promo")
    source_code = Column(String(120), nullable=True)
    # facebook | instagram | instagram_story | whatsapp_status (manual) | other (manual)
    channels = Column(JSON, nullable=False, default=list)
    status = Column(String(16), nullable=False, default="draft")
    scheduled_at = Column(DateTime(timezone=True), nullable=True)
    published_at = Column(DateTime(timezone=True), nullable=True)
    reminder_sent_at = Column(DateTime(timezone=True), nullable=True)
    results = Column(JSON, nullable=False, default=dict)
    created_by_subject_type = Column(String(64), nullable=False)
    created_by_subject_id = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('draft', 'scheduled', 'publishing', 'published', 'partial', 'failed', 'cancelled', 'skipped')", name="ck_estate_social_post_status"),
        Index("ix_estate_social_posts_plan", "plan_id", "scheduled_at"),
        Index("ix_estate_social_posts_estate", "estate_id", "created_at"),
        Index("ix_estate_social_posts_due", "status", "scheduled_at"),
    )


class EstateMarketingOptin(Base):
    """A buyer who agreed to receive updates about an estate on WhatsApp. The consent text and time are
    kept as proof, and sending stops immediately when status becomes 'revoked'."""

    __tablename__ = "estate_marketing_optins"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    estate_id = Column(Integer, ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False)
    full_name = Column(String(255), nullable=True)
    phone_digits = Column(String(20), nullable=False)
    channel = Column(String(16), nullable=False, default="whatsapp")
    consent_text = Column(Text, nullable=False)
    source_code = Column(String(120), nullable=True)
    status = Column(String(16), nullable=False, default="active")  # active | revoked
    unsubscribe_hash = Column(String(64), nullable=False, unique=True)
    consented_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("estate_id", "phone_digits", "channel", name="uq_estate_marketing_optin"),
        CheckConstraint("status IN ('active', 'revoked')", name="ck_estate_marketing_optin_status"),
        Index("ix_estate_marketing_optins_estate", "estate_id", "status"),
    )


class EstateWhatsappSend(Base):
    """Audit trail of every WhatsApp template message sent to an opted-in contact."""

    __tablename__ = "estate_whatsapp_sends"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    estate_id = Column(Integer, ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False)
    optin_id = Column(Integer, ForeignKey("estate_marketing_optins.id", ondelete="SET NULL"), nullable=True)
    batch_uid = Column(String(36), nullable=False)
    template_name = Column(String(120), nullable=False)
    params = Column(JSON, nullable=False, default=list)
    image_url = Column(String(500), nullable=True)  # the flyer design attached as the template's header image
    status = Column(String(16), nullable=False, default="queued")  # queued | sent | failed | skipped
    provider_message_id = Column(String(120), nullable=True)
    error = Column(Text, nullable=True)
    sent_by_subject_id = Column(String(128), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (Index("ix_estate_whatsapp_sends_batch", "estate_id", "batch_uid"),)
