from __future__ import annotations

import uuid

from geoalchemy2 import Geometry
from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, ForeignKey, Integer, JSON, Numeric, String, Text, UniqueConstraint
from sqlalchemy.sql import func

from app.db_base import Base


class EstateOrganization(Base):
    __tablename__ = "estate_organizations"

    id = Column(Integer, primary_key=True)
    organization_uid = Column(String(36), nullable=False, unique=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False)
    slug = Column(String(120), nullable=False, unique=True)
    status = Column(String(32), nullable=False, default="active")
    contact_email = Column(String(255), nullable=True)
    settings = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('active', 'suspended', 'archived')", name="ck_estate_organizations_status"),
    )


class EstateOrganizationMember(Base):
    __tablename__ = "estate_organization_members"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    subject_type = Column(String(64), nullable=False)
    subject_id = Column(String(128), nullable=False)
    role_key = Column(String(32), nullable=False)
    contact_email = Column(String(255), nullable=True)
    contact_phone = Column(String(64), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("organization_id", "subject_type", "subject_id", name="uq_estate_member_subject"),
        CheckConstraint(
            "role_key IN ('owner', 'manager', 'accounts', 'surveyor', 'field_officer', 'sales', 'marketer', 'viewer')",
            name="ck_estate_members_role",
        ),
    )


class EstateOrganizationEntitlement(Base):
    __tablename__ = "estate_organization_entitlements"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    feature_key = Column(String(64), nullable=False)
    is_enabled = Column(Boolean, nullable=False, default=False)
    limit_value = Column(Integer, nullable=True)
    updated_by_subject_type = Column(String(64), nullable=True)
    updated_by_subject_id = Column(String(128), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("organization_id", "feature_key", name="uq_estate_entitlement_feature"),
        CheckConstraint("limit_value IS NULL OR limit_value >= 0", name="ck_estate_entitlements_limit"),
    )


class EstateAuditEvent(Base):
    __tablename__ = "estate_audit_events"

    id = Column(Integer, primary_key=True)
    event_uid = Column(String(36), nullable=False, unique=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    actor_subject_type = Column(String(64), nullable=True)
    actor_subject_id = Column(String(128), nullable=True)
    action = Column(String(120), nullable=False)
    entity_type = Column(String(80), nullable=False)
    entity_id = Column(String(128), nullable=False)
    before_data = Column(JSON, nullable=True)
    after_data = Column(JSON, nullable=True)
    metadata_json = Column("metadata", JSON, nullable=False, default=dict)
    correlation_id = Column(String(128), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Estate(Base):
    __tablename__ = "estate_estates"
    id = Column(Integer, primary_key=True)
    estate_uid = Column(String(36), nullable=False, unique=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    state = Column(String(120), nullable=True)
    locality = Column(String(160), nullable=True)
    location_text = Column(String(255), nullable=True)
    status = Column(String(32), nullable=False, default="planning")
    crs = Column(String(80), nullable=False, default="EPSG:4326")
    datum = Column(String(80), nullable=True)
    unit_system = Column(String(4), nullable=False, default="m")  # "m" | "ft" - display/entry unit for dimensions and areas
    approximate_area_sqm = Column(Numeric(16, 2), nullable=True)
    project_reference = Column(String(120), nullable=True)
    project_owner = Column(String(255), nullable=True)
    ownership_details = Column(Text, nullable=True)
    boundary = Column(Geometry("POLYGON", srid=4326), nullable=True)
    public_slug = Column(String(140), nullable=True, unique=True)
    public_enabled = Column(Boolean, nullable=False, default=False)
    public_description = Column(Text, nullable=True)
    public_tagline = Column(String(255), nullable=True)
    public_contact_phone = Column(String(64), nullable=True)
    public_logo_object_key = Column(String(512), nullable=True)
    public_show_prices = Column(Boolean, nullable=False, default=True)
    public_payment_plan = Column(JSON, nullable=True)
    created_by_subject_type = Column(String(64), nullable=False)
    created_by_subject_id = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    archived_at = Column(DateTime(timezone=True), nullable=True)


class EstateBlock(Base):
    __tablename__ = "estate_blocks"
    id = Column(Integer, primary_key=True)
    estate_id = Column(Integer, ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False)
    label = Column(String(80), nullable=False)
    name = Column(String(160), nullable=True)
    geometry = Column(Geometry("POLYGON", srid=4326), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    __table_args__ = (UniqueConstraint("estate_id", "label", name="uq_estate_blocks_label"),)


class EstatePlot(Base):
    __tablename__ = "estate_plots"
    id = Column(Integer, primary_key=True)
    plot_uid = Column(String(36), nullable=False, unique=True, default=lambda: str(uuid.uuid4()))
    estate_id = Column(Integer, ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False)
    block_id = Column(Integer, ForeignKey("estate_blocks.id", ondelete="SET NULL"), nullable=True)
    plot_number = Column(String(120), nullable=False)
    plot_number_normalized = Column(String(120), nullable=False)
    geometry = Column(Geometry("POLYGON", srid=4326), nullable=False)
    area_sqm = Column(Numeric(16, 2), nullable=False)
    public_address = Column(String(255), nullable=True)
    asking_price = Column(Numeric(16, 2), nullable=True)
    land_use = Column(String(120), nullable=True)
    commercial_status = Column(String(32), nullable=False, default="available")
    development_status = Column(String(32), nullable=False, default="not_started")
    geometry_status = Column(String(32), nullable=False, default="draft")
    source_type = Column(String(32), nullable=False, default="manual")
    source_reference = Column(String(128), nullable=True)
    created_by_subject_type = Column(String(64), nullable=False)
    created_by_subject_id = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    __table_args__ = (UniqueConstraint("estate_id", "plot_number_normalized", name="uq_estate_plot_number"),)


class EstatePublicReservationRequest(Base):
    """A public sales lead for a plot, separate from an internal allocation/customer record."""

    __tablename__ = "estate_public_reservation_requests"

    id = Column(Integer, primary_key=True)
    request_uid = Column(String(36), nullable=False, unique=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    estate_id = Column(Integer, ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False)
    plot_id = Column(Integer, ForeignKey("estate_plots.id", ondelete="CASCADE"), nullable=False)
    full_name = Column(String(255), nullable=False)
    phone = Column(String(64), nullable=False)
    email = Column(String(255), nullable=True)
    message = Column(Text, nullable=True)
    source_code = Column(String(120), nullable=True)
    source_channel = Column(String(64), nullable=True)
    assigned_agent_subject_type = Column(String(64), nullable=True)
    assigned_agent_subject_id = Column(String(128), nullable=True)
    status = Column(String(32), nullable=False, default="new")
    staff_notes = Column(Text, nullable=True)
    contacted_at = Column(DateTime(timezone=True), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    customer_id = Column(Integer, ForeignKey("estate_customers.id", ondelete="SET NULL"), nullable=True)
    allocation_id = Column(Integer, ForeignKey("estate_allocations.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('new', 'contacted', 'converted', 'declined')", name="ck_estate_public_reservation_status"),
    )


class EstateCustomer(Base):
    __tablename__ = "estate_customers"
    id = Column(Integer, primary_key=True)
    customer_uid = Column(String(36), nullable=False, unique=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    reference_no = Column(String(80), nullable=True)
    full_name = Column(String(255), nullable=False)
    full_name_normalized = Column(String(255), nullable=False)
    phone = Column(String(64), nullable=True)
    email = Column(String(255), nullable=True)
    address = Column(Text, nullable=True)
    company_name = Column(String(255), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class EstateAllocation(Base):
    __tablename__ = "estate_allocations"
    id = Column(Integer, primary_key=True)
    allocation_uid = Column(String(36), nullable=False, unique=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    estate_id = Column(Integer, ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False)
    plot_id = Column(Integer, ForeignKey("estate_plots.id", ondelete="RESTRICT"), nullable=False)
    customer_id = Column(Integer, ForeignKey("estate_customers.id", ondelete="RESTRICT"), nullable=False)
    status = Column(String(32), nullable=False)
    reservation_date = Column(DateTime(timezone=True), nullable=True)
    reservation_expires_at = Column(DateTime(timezone=True), nullable=True)
    reservation_reminder_sent_at = Column(DateTime(timezone=True), nullable=True)
    allocation_date = Column(DateTime(timezone=True), nullable=True)
    agreed_price = Column(Numeric(16, 2), nullable=True)
    payment_plan = Column(Text, nullable=True)
    next_payment_due_at = Column(DateTime(timezone=True), nullable=True)
    payment_reminder_sent_for_due_at = Column(DateTime(timezone=True), nullable=True)
    notes = Column(Text, nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    cancellation_reason = Column(Text, nullable=True)
    created_by_subject_type = Column(String(64), nullable=False)
    created_by_subject_id = Column(String(128), nullable=False)
    # The org member credited with bringing in this sale - optional, tagged at reserve/allocate
    # time. Commission fields are only ever filled in once the sale is fully Allocated (paid),
    # snapshotting the tier/rate/amount that applied at that moment so a later tier-table edit
    # never silently rewrites a commission already earned.
    sales_agent_subject_type = Column(String(64), nullable=True)
    sales_agent_subject_id = Column(String(128), nullable=True)
    lead_source_code = Column(String(120), nullable=True)
    lead_source_channel = Column(String(64), nullable=True)
    commission_tier_label = Column(String(120), nullable=True)
    commission_rate_percent = Column(Numeric(5, 2), nullable=True)
    commission_amount = Column(Numeric(16, 2), nullable=True)
    # An unguessable per-allocation token used for the customer-facing "view your plot on satellite
    # map" link sent in lifecycle emails - deliberately not the numeric id, since that's guessable/
    # enumerable and this link needs no login.
    share_token = Column(String(64), nullable=True, unique=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class EstateCommissionTier(Base):
    """A per-organization commission ladder: a sales agent's commission rate on a sale is set by
    their cumulative Allocated (fully paid) sales volume *before* that sale, mirroring how Nigerian
    real estate firms commonly run this (e.g. 5% until N40m in verified sales, 7.5% until N80m,
    10% beyond) - the rate steps up automatically from the next transaction, it never recalculates
    past ones."""

    __tablename__ = "estate_commission_tiers"
    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    label = Column(String(120), nullable=False)
    min_cumulative_sales = Column(Numeric(16, 2), nullable=False, default=0)
    rate_percent = Column(Numeric(5, 2), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class EstateCommissionPayout(Base):
    """A record of the org actually paying out (some or all of) a locked-in commission to the
    agent who earned it - separate from EstateAllocation.commission_amount, which is only ever
    what was *earned*. Modeled on EstatePayment (customer -> org) but in the other direction (org
    -> agent); a receipt/proof of payment attaches the same way, via EstateDocument + a
    entity_type="commission_payout" EstateDocumentLink, reusing the existing generic upload."""

    __tablename__ = "estate_commission_payouts"
    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    allocation_id = Column(Integer, ForeignKey("estate_allocations.id", ondelete="CASCADE"), nullable=False)
    sales_agent_subject_type = Column(String(64), nullable=False)
    sales_agent_subject_id = Column(String(128), nullable=False)
    amount = Column(Numeric(16, 2), nullable=False)
    payment_date = Column(DateTime(timezone=True), nullable=False)
    payment_method = Column(String(80), nullable=False)
    reference_no = Column(String(160), nullable=True)
    notes = Column(Text, nullable=True)
    paid_by_subject_type = Column(String(64), nullable=False)
    paid_by_subject_id = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class EstatePayment(Base):
    __tablename__ = "estate_payments"
    id = Column(Integer, primary_key=True)
    payment_uid = Column(String(36), nullable=False, unique=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    allocation_id = Column(Integer, ForeignKey("estate_allocations.id", ondelete="RESTRICT"), nullable=False)
    customer_id = Column(Integer, ForeignKey("estate_customers.id", ondelete="RESTRICT"), nullable=False)
    plot_id = Column(Integer, ForeignKey("estate_plots.id", ondelete="RESTRICT"), nullable=False)
    amount = Column(Numeric(16, 2), nullable=False)
    currency = Column(String(3), nullable=False, default="NGN")
    payment_date = Column(DateTime(timezone=True), nullable=False)
    payment_method = Column(String(80), nullable=False)
    reference_no = Column(String(160), nullable=True)
    notes = Column(Text, nullable=True)
    status = Column(String(32), nullable=False)
    recorded_by_subject_type = Column(String(64), nullable=False)
    recorded_by_subject_id = Column(String(128), nullable=False)
    confirmed_by_subject_type = Column(String(64), nullable=True)
    confirmed_by_subject_id = Column(String(128), nullable=True)
    confirmed_at = Column(DateTime(timezone=True), nullable=True)
    voided_by_subject_type = Column(String(64), nullable=True)
    voided_by_subject_id = Column(String(128), nullable=True)
    voided_at = Column(DateTime(timezone=True), nullable=True)
    void_reason = Column(Text, nullable=True)
    receipt_number = Column(String(120), nullable=True, unique=True)
    reconciled_at = Column(DateTime(timezone=True), nullable=True)
    reconciled_by_subject_type = Column(String(64), nullable=True)
    reconciled_by_subject_id = Column(String(128), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class EstateNotificationLog(Base):
    """Delivery audit for customer-facing Estate email notifications."""

    __tablename__ = "estate_notification_logs"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    estate_id = Column(Integer, ForeignKey("estate_estates.id", ondelete="SET NULL"), nullable=True)
    customer_id = Column(Integer, ForeignKey("estate_customers.id", ondelete="SET NULL"), nullable=True)
    allocation_id = Column(Integer, ForeignKey("estate_allocations.id", ondelete="SET NULL"), nullable=True)
    channel = Column(String(32), nullable=False, default="email")
    event_key = Column(String(80), nullable=False)
    recipient_email = Column(String(255), nullable=True)
    recipient_name = Column(String(255), nullable=True)
    subject = Column(String(255), nullable=True)
    status = Column(String(32), nullable=False)
    error_message = Column(Text, nullable=True)
    details = Column("metadata", JSON, nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('sent', 'failed', 'skipped')", name="ck_estate_notification_logs_status"),
    )


class EstatePaymentInbox(Base):
    """A safe holding area for bank/online payment notifications before staff match them."""

    __tablename__ = "estate_payment_inbox"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    estate_id = Column(Integer, ForeignKey("estate_estates.id", ondelete="SET NULL"), nullable=True)
    amount = Column(Numeric(16, 2), nullable=False)
    currency = Column(String(3), nullable=False, default="NGN")
    payment_date = Column(DateTime(timezone=True), nullable=False)
    payer_name = Column(String(255), nullable=True)
    payer_reference = Column(String(160), nullable=True)
    source = Column(String(80), nullable=False, default="manual")
    raw_payload = Column(JSON, nullable=True)
    status = Column(String(32), nullable=False, default="unmatched")
    matched_payment_id = Column(Integer, ForeignKey("estate_payments.id", ondelete="SET NULL"), nullable=True)
    match_notes = Column(Text, nullable=True)
    matched_at = Column(DateTime(timezone=True), nullable=True)
    matched_by_subject_type = Column(String(64), nullable=True)
    matched_by_subject_id = Column(String(128), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('unmatched', 'matched', 'ignored')", name="ck_estate_payment_inbox_status"),
    )


class EstateCustomerPortalToken(Base):
    """Revocable, hashed links for a buyer's safe self-service portal."""

    __tablename__ = "estate_customer_portal_tokens"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    customer_id = Column(Integer, ForeignKey("estate_customers.id", ondelete="CASCADE"), nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    created_by_subject_type = Column(String(64), nullable=False)
    created_by_subject_id = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class EstateAgentPortalToken(Base):
    """Revocable, hashed invite links for an organization member's agent workspace."""

    __tablename__ = "estate_agent_portal_tokens"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    member_id = Column(Integer, ForeignKey("estate_organization_members.id", ondelete="CASCADE"), nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    created_by_subject_type = Column(String(64), nullable=False)
    created_by_subject_id = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class EstateQrCampaign(Base):
    """A tracked public-page source such as an entrance sign, brochure, agent, or WhatsApp link."""

    __tablename__ = "estate_qr_campaigns"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    estate_id = Column(Integer, ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False)
    code = Column(String(120), nullable=False)
    name = Column(String(160), nullable=False)
    channel = Column(String(64), nullable=False, default="other")
    assigned_agent_subject_type = Column(String(64), nullable=True)
    assigned_agent_subject_id = Column(String(128), nullable=True)
    scan_count = Column(Integer, nullable=False, default=0)
    last_scanned_at = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_by_subject_type = Column(String(64), nullable=False)
    created_by_subject_id = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (UniqueConstraint("estate_id", "code", name="uq_estate_qr_campaign_code"),)


class EstateDocument(Base):
    __tablename__ = "estate_documents"
    id = Column(Integer, primary_key=True)
    document_uid = Column(String(36), nullable=False, unique=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    object_key = Column(String(512), nullable=False, unique=True)
    original_filename = Column(String(255), nullable=False)
    mime_type = Column(String(120), nullable=False)
    size_bytes = Column(Integer, nullable=False)
    checksum = Column(String(128), nullable=True)
    document_type = Column(String(64), nullable=False, default="other")
    description = Column(Text, nullable=True)
    uploaded_by_subject_type = Column(String(64), nullable=False)
    uploaded_by_subject_id = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class EstateDocumentLink(Base):
    __tablename__ = "estate_document_links"
    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("estate_documents.id", ondelete="CASCADE"), nullable=False)
    entity_type = Column(String(64), nullable=False)
    entity_id = Column(String(128), nullable=False)
    __table_args__ = (UniqueConstraint("document_id", "entity_type", "entity_id", name="uq_estate_document_link"),)


class EstatePaymentRule(Base):
    __tablename__ = "estate_payment_rules"
    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    rule_key = Column(String(64), nullable=False)
    is_enabled = Column(Boolean, nullable=False, default=False)
    percentage = Column(Numeric(5, 2), nullable=True)
    description = Column(Text, nullable=True)
    __table_args__ = (UniqueConstraint("organization_id", "rule_key", name="uq_estate_payment_rule"),)


class EstateSurveyRequest(Base):
    __tablename__ = "estate_survey_requests"
    id = Column(Integer, primary_key=True)
    request_uid = Column(String(36), nullable=False, unique=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    estate_id = Column(Integer, ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False)
    plot_id = Column(Integer, ForeignKey("estate_plots.id", ondelete="CASCADE"), nullable=False)
    allocation_id = Column(Integer, ForeignKey("estate_allocations.id", ondelete="SET NULL"), nullable=True)
    requested_by_subject_type = Column(String(64), nullable=False)
    requested_by_subject_id = Column(String(128), nullable=False)
    assigned_surveyor_subject_type = Column(String(64), nullable=True)
    assigned_surveyor_subject_id = Column(String(128), nullable=True)
    status = Column(String(32), nullable=False, default="requested")
    survey_owner_user_id = Column(Integer, nullable=True)
    survey_working_plot_id = Column(Integer, nullable=True, unique=True)
    survey_reference = Column(String(128), nullable=True)
    materialized_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class EstateStakingTask(Base):
    __tablename__ = "estate_staking_tasks"
    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    estate_id = Column(Integer, ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False)
    plot_id = Column(Integer, ForeignKey("estate_plots.id", ondelete="CASCADE"), nullable=False)
    survey_request_id = Column(Integer, ForeignKey("estate_survey_requests.id", ondelete="CASCADE"), nullable=False)
    status = Column(String(32), nullable=False, default="pending")
    assigned_subject_type = Column(String(64), nullable=True)
    assigned_subject_id = Column(String(128), nullable=True)
    notes = Column(Text, nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class EstateFieldInspection(Base):
    __tablename__ = "estate_field_inspections"
    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    estate_id = Column(Integer, ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False)
    plot_id = Column(Integer, ForeignKey("estate_plots.id", ondelete="CASCADE"), nullable=False)
    inspection_type = Column(String(64), nullable=False, default="site_visit")
    outcome = Column(String(32), nullable=False, default="observed")
    notes = Column(Text, nullable=True)
    inspected_by_subject_type = Column(String(64), nullable=False)
    inspected_by_subject_id = Column(String(128), nullable=False)
    inspected_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class EstateSpatialFeature(Base):
    __tablename__ = "estate_spatial_features"
    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    estate_id = Column(Integer, ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False)
    feature_type = Column(String(32), nullable=False)
    name = Column(String(160), nullable=True)
    geometry = Column(Geometry("GEOMETRY", srid=4326), nullable=False)
    status = Column(String(32), nullable=False, default="active")
    created_by_subject_type = Column(String(64), nullable=False)
    created_by_subject_id = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class EstateHazardAssessment(Base):
    __tablename__ = "estate_hazard_assessments"
    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    estate_id = Column(Integer, ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False)
    plot_id = Column(Integer, ForeignKey("estate_plots.id", ondelete="CASCADE"), nullable=True)
    hazard_type = Column(String(32), nullable=False)
    status = Column(String(32), nullable=False, default="completed")
    risk_class = Column(String(64), nullable=True)
    risk_score = Column(Numeric(8, 3), nullable=True)
    result_payload = Column(JSON, nullable=False, default=dict)
    source = Column(String(80), nullable=False, default="landcheck_hazard_engine")
    assessed_by_subject_type = Column(String(64), nullable=False)
    assessed_by_subject_id = Column(String(128), nullable=False)
    assessed_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class EstateImportReview(Base):
    __tablename__ = "estate_import_reviews"
    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    estate_id = Column(Integer, ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False)
    source_type = Column(String(32), nullable=False)
    survey_georeference_session_id = Column(String(128), nullable=True)
    status = Column(String(32), nullable=False, default="review_required")
    notes = Column(Text, nullable=True)
    candidate_data = Column(JSON, nullable=False, default=list)
    created_by_subject_type = Column(String(64), nullable=False)
    created_by_subject_id = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class EstateLayoutProposal(Base):
    """A generated concept layout awaiting surveyor/manager approval."""

    __tablename__ = "estate_layout_proposals"

    id = Column(Integer, primary_key=True)
    proposal_uid = Column(String(36), nullable=False, unique=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    estate_id = Column(Integer, ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False)
    status = Column(String(32), nullable=False, default="review_required")
    criteria = Column(JSON, nullable=False, default=dict)
    diagnostics = Column(JSON, nullable=False, default=dict)
    plot_candidates = Column(JSON, nullable=False, default=list)
    feature_candidates = Column(JSON, nullable=False, default=list)
    created_by_subject_type = Column(String(64), nullable=False)
    created_by_subject_id = Column(String(128), nullable=False)
    reviewed_by_subject_type = Column(String(64), nullable=True)
    reviewed_by_subject_id = Column(String(128), nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('review_required', 'approved', 'rejected')", name="ck_estate_layout_proposals_status"),
    )
