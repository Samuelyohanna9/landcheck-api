from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.sql import func

from app.db_base import Base

POST_STATUSES = ("draft", "scheduled", "publishing", "published", "partial", "failed", "cancelled")


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


class EstateSocialPost(Base):
    """One marketing post: a caption plus a generated image, sent to one or more channels now or later."""

    __tablename__ = "estate_social_posts"

    id = Column(Integer, primary_key=True)
    post_uid = Column(String(36), nullable=False, unique=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    estate_id = Column(Integer, ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False)
    plot_id = Column(Integer, ForeignKey("estate_plots.id", ondelete="SET NULL"), nullable=True)
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
        CheckConstraint("status IN ('draft', 'scheduled', 'publishing', 'published', 'partial', 'failed', 'cancelled')", name="ck_estate_social_post_status"),
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
    status = Column(String(16), nullable=False, default="queued")  # queued | sent | failed | skipped
    provider_message_id = Column(String(120), nullable=True)
    error = Column(Text, nullable=True)
    sent_by_subject_id = Column(String(128), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (Index("ix_estate_whatsapp_sends_batch", "estate_id", "batch_uid"),)
