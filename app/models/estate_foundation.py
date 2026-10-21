from __future__ import annotations

import uuid

from geoalchemy2 import Geometry
from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, Float, ForeignKey, Index, Integer, JSON, Numeric, String, Text, UniqueConstraint
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


class EstateOrganizationPayoutAccount(Base):
    """The bank account a developer org gets settled into for buyer online plot payments, via a
    Paystack/Flutterwave subaccount created at setup time - see
    app/services/estates/payout_accounts.py. One row per organization; a missing row means the
    org has not set up online payments yet, which initiate_allocation_payment() treats as "online
    payment not offered" rather than silently falling back to LandCheck's own account."""

    __tablename__ = "estate_organization_payout_accounts"
    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False, unique=True)
    bank_code = Column(String(16), nullable=False)
    bank_name = Column(String(160), nullable=True)
    account_number = Column(String(32), nullable=False)
    account_name = Column(String(255), nullable=False)
    paystack_subaccount_code = Column(String(64), nullable=True)
    flutterwave_subaccount_id = Column(String(64), nullable=True)
    created_by_subject_type = Column(String(64), nullable=False)
    created_by_subject_id = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class EstateOrganizationMember(Base):
    __tablename__ = "estate_organization_members"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    subject_type = Column(String(64), nullable=False)
    subject_id = Column(String(128), nullable=False)
    role_key = Column(String(32), nullable=False)
    # Only set (and only consulted) when role_key == 'staff' - a custom-built role with an
    # organization-owner-chosen checklist of dashboard permissions, rather than one of the fixed
    # built-in roles above. custom_role_id is the named template it was created from (for display
    # and re-editing); custom_permissions is the actual granted set, copied from that role at
    # creation and independently editable per staff member afterwards.
    custom_role_id = Column(Integer, ForeignKey("estate_staff_roles.id", ondelete="SET NULL"), nullable=True)
    custom_permissions = Column(JSON, nullable=True)
    contact_email = Column(String(255), nullable=True)
    contact_phone = Column(String(64), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("organization_id", "subject_type", "subject_id", name="uq_estate_member_subject"),
        CheckConstraint(
            "role_key IN ('owner', 'manager', 'accounts', 'surveyor', 'field_officer', 'sales', 'marketer', 'viewer', 'staff')",
            name="ck_estate_members_role",
        ),
    )


class EstateStaffRole(Base):
    """An organization-owner-defined named role for the "Add Access" staff feature - a name plus
    a checklist of dashboard permission keys (see app/services/estates/permissions.py's
    PERMISSION_CATALOG). Distinct from the fixed built-in roles on EstateOrganizationMember.role_key:
    those are hardcoded product roles, these are ad hoc, per-organization, and editable at any time.
    """

    __tablename__ = "estate_staff_roles"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(120), nullable=False)
    permissions = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_estate_staff_role_org_name"),
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
    public_cover_object_key = Column(String(512), nullable=True)  # hero background photo; falls back to the built-in default when unset
    public_show_prices = Column(Boolean, nullable=False, default=True)
    public_payment_plan = Column(JSON, nullable=True)
    public_development_forecast = Column(JSON, nullable=True)
    public_whatsapp_number = Column(String(32), nullable=True)
    public_meeting_point = Column(JSON, nullable=True)  # {lat, lng, label, note} - where inspection visitors meet
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
    # Staff-asserted, not auto-computed - LandCheck can't confirm land title on its own; this
    # records that a staff member reviewed the plot's actual title documents (see
    # ESTATE_DOCUMENT_TYPES: title_document/certificate_of_occupancy/governors_consent/
    # survey_plan) and is willing to put their name behind it publicly.
    title_verified = Column(Boolean, nullable=False, default=False)
    title_verified_at = Column(DateTime(timezone=True), nullable=True)
    title_verified_note = Column(Text, nullable=True)
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
    idempotency_key = Column(String(128), nullable=True)
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
    follow_up_reminder_count = Column(Integer, nullable=False, default=0, server_default="0")
    last_follow_up_reminder_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('new', 'contacted', 'converted', 'declined')", name="ck_estate_public_reservation_status"),
    )


class GeotechSurveyRequest(Base):
    """A staff-initiated request (from the Estate dashboard's Hazard tab) to have a licensed
    geotechnical survey done for a plot or a whole Estate - the honest follow-through on the
    Ground & Drainage screening's scope note, which explicitly does not measure load-bearing
    capacity, water table depth, or subsurface layers. Unlike EstatePublicReservationRequest, this
    is raised by an already-authenticated Estate staff member, not an anonymous public visitor."""

    __tablename__ = "estate_geotech_survey_requests"

    id = Column(Integer, primary_key=True)
    request_uid = Column(String(36), nullable=False, unique=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    estate_id = Column(Integer, ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False)
    plot_id = Column(Integer, ForeignKey("estate_plots.id", ondelete="CASCADE"), nullable=True)
    requested_by_subject_type = Column(String(64), nullable=False)
    requested_by_subject_id = Column(String(128), nullable=False)
    contact_name = Column(String(255), nullable=True)
    contact_phone = Column(String(64), nullable=True)
    contact_email = Column(String(255), nullable=True)
    note = Column(Text, nullable=True)
    status = Column(String(32), nullable=False, default="new")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('new', 'contacted', 'completed', 'declined')", name="ck_geotech_survey_request_status"),
    )


class EstateCustomer(Base):
    __tablename__ = "estate_customers"
    id = Column(Integer, primary_key=True)
    customer_uid = Column(String(36), nullable=False, unique=True, default=lambda: str(uuid.uuid4()))
    idempotency_key = Column(String(128), nullable=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    reference_no = Column(String(80), nullable=True)
    # A durable cross-system id for spreadsheet re-imports (distinct from idempotency_key, which
    # is single-request retry semantics only) - "this row maps to customer #482 in your
    # spreadsheet" across repeat imports. Nullable; unique per org when supplied.
    external_id = Column(String(128), nullable=True)
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
    idempotency_key = Column(String(128), nullable=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    estate_id = Column(Integer, ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False)
    plot_id = Column(Integer, ForeignKey("estate_plots.id", ondelete="RESTRICT"), nullable=False)
    customer_id = Column(Integer, ForeignKey("estate_customers.id", ondelete="RESTRICT"), nullable=False)
    status = Column(String(32), nullable=False)
    # A sale/contract reference from an external system (e.g. a spreadsheet's own "Contract No."
    # column) - nullable, not unique (a developer's old contract numbers aren't guaranteed
    # globally unique), used only as a match hint on re-import.
    external_reference = Column(String(160), nullable=True)
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
    idempotency_key = Column(String(128), nullable=True)
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
    idempotency_key = Column(String(128), nullable=True)
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


class EstateAllocationChargeAttempt(Base):
    """An allocation-scoped Paystack/Flutterwave ledger row for a buyer paying for their plot
    online through the Buyer Portal - the online-payment counterpart to EstatePayment's manual
    bank-transfer flow. Deliberately separate from EstateSubscriptionCharge, which is FK'd to a
    subscription/organization, not an allocation; this one's grain matches EstatePayment's."""

    __tablename__ = "estate_allocation_charge_attempts"
    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    allocation_id = Column(Integer, ForeignKey("estate_allocations.id", ondelete="CASCADE"), nullable=False)
    customer_id = Column(Integer, ForeignKey("estate_customers.id", ondelete="CASCADE"), nullable=False)
    amount = Column(Numeric(16, 2), nullable=False)
    currency = Column(String(3), nullable=False, default="NGN")
    payment_provider = Column(String(32), nullable=False)
    tx_ref = Column(String(64), nullable=False, unique=True)
    # Client-supplied, same convention as EstatePayment.idempotency_key - a double-click or a
    # retried request reuses the same key instead of opening a second gateway checkout. Enforced
    # by a partial unique index (organization_id, idempotency_key) WHERE NOT NULL, see the
    # 20261019_0062 migration.
    idempotency_key = Column(String(128), nullable=True)
    provider_transaction_id = Column(String(64), nullable=True)
    status = Column(String(16), nullable=False, default="pending")
    provider_payload = Column(JSON, nullable=True)
    resulting_payment_id = Column(Integer, ForeignKey("estate_payments.id", ondelete="SET NULL"), nullable=True)
    failure_reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)
    # Whether this successful charge has actually settled into the developer's bank account yet -
    # Flutterwave settles on its own schedule (typically T+1), not instantly at charge time. Only
    # ever flipped to "settled" by a positive match against Flutterwave's own settlement records
    # (see app/services/estates/settlement_tracking.py) - never guessed from elapsed time.
    settlement_status = Column(String(16), nullable=False, default="pending")
    settled_at = Column(DateTime(timezone=True), nullable=True)
    settlement_checked_at = Column(DateTime(timezone=True), nullable=True)
    # Set only when a LandCheck admin manually triggered an immediate Flutterwave transfer (see
    # estate_payout_transfers.py) instead of waiting for Flutterwave's own automatic subaccount
    # settlement to be detected by settlement_tracking.refresh_settlement_status - lets the two
    # settlement paths be told apart in the Accounting views on both sides.
    settlement_channel = Column(String(24), nullable=True)
    manual_transfer_id = Column(String(120), nullable=True)
    manual_transfer_reference = Column(String(160), nullable=True)
    manual_transfer_status = Column(String(32), nullable=True)
    manual_transfer_error = Column(Text, nullable=True)
    manual_settled_by_subject_type = Column(String(64), nullable=True)
    manual_settled_by_subject_id = Column(String(128), nullable=True)


class EstatePaymentIssueReport(Base):
    """A developer flagging a problem with a specific payment (most commonly: an online payment
    that hasn't settled) from the Accounting tab - a lightweight support ticket, not a full
    workflow. Emailed to LandCheck support on creation; status tracked here so staff can see what
    they've already reported."""

    __tablename__ = "estate_payment_issue_reports"
    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    charge_attempt_id = Column(Integer, ForeignKey("estate_allocation_charge_attempts.id", ondelete="SET NULL"), nullable=True)
    payment_id = Column(Integer, ForeignKey("estate_payments.id", ondelete="SET NULL"), nullable=True)
    customer_name_snapshot = Column(String(255), nullable=True)
    plot_number_snapshot = Column(String(64), nullable=True)
    message = Column(Text, nullable=False)
    status = Column(String(16), nullable=False, default="open")
    reported_by_subject_type = Column(String(64), nullable=False)
    reported_by_subject_id = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    resolved_by_subject_type = Column(String(64), nullable=True)
    resolved_by_subject_id = Column(String(128), nullable=True)


class EstateSettlementRequest(Base):
    """A developer asking LandCheck to manually initiate an immediate settlement of a captured
    online payment, ahead of Flutterwave's own next-business-day schedule for that subaccount.
    Emailed to LandCheck support on creation (see estate_email.send_settlement_request_email);
    reviewed and actioned from the Estates admin accounting view (app/routers/estate_admin_accounting.py),
    where "settled" is only ever reached after a real Flutterwave transfer succeeds, never guessed."""

    __tablename__ = "estate_settlement_requests"
    id = Column(Integer, primary_key=True)
    request_uid = Column(String(36), nullable=False, unique=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    charge_attempt_id = Column(Integer, ForeignKey("estate_allocation_charge_attempts.id", ondelete="SET NULL"), nullable=True)
    amount_snapshot = Column(Numeric(16, 2), nullable=True)
    note = Column(Text, nullable=True)
    status = Column(String(16), nullable=False, default="requested")
    requested_by_subject_type = Column(String(64), nullable=False)
    requested_by_subject_id = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    reviewed_by_subject_type = Column(String(64), nullable=True)
    reviewed_by_subject_id = Column(String(128), nullable=True)
    settled_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("status IN ('requested', 'in_review', 'settled', 'declined')", name="ck_estate_settlement_requests_status"),
    )


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


class EstateSoilAssessment(Base):
    """Soil Analysis results - deliberately a separate table from EstateHazardAssessment: Soil
    Analysis is its own sidebar tool, not a Hazard Analysis category (see soil_analysis.py's
    module docstring), so its stored results don't live in the same table/dashboard the Hazard
    pages read from."""

    __tablename__ = "estate_soil_assessments"
    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    estate_id = Column(Integer, ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False)
    plot_id = Column(Integer, ForeignKey("estate_plots.id", ondelete="CASCADE"), nullable=True)
    status = Column(String(32), nullable=False, default="completed")
    risk_class = Column(String(64), nullable=True)
    risk_score = Column(Numeric(8, 3), nullable=True)
    result_payload = Column(JSON, nullable=False, default=dict)
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


class EstateImportBatch(Base):
    """One spreadsheet upload through the "Connect your tools" Integrations feature - customers,
    plots, sales/allocations and payments, staged for review before anything is committed. The
    plot-geometry equivalent of this is EstateImportReview above; this one is for business
    records, following the same "upload -> validate -> stage -> approve" shape."""

    __tablename__ = "estate_import_batches"
    id = Column(Integer, primary_key=True)
    batch_uid = Column(String(36), nullable=False, unique=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    estate_id = Column(Integer, ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False)
    source_filename = Column(String(255), nullable=False)
    status = Column(String(16), nullable=False, default="uploaded")
    column_mapping = Column(JSON, nullable=True)
    # Required only when a batch mixes opening-balance rows with dated transaction rows, so
    # transactions after this date aren't double-counted against the opening total - see
    # app/services/estates/data_import.py.
    opening_balance_cutoff = Column(DateTime(timezone=True), nullable=True)
    row_counts = Column(JSON, nullable=True)
    uploaded_by_subject_type = Column(String(64), nullable=False)
    uploaded_by_subject_id = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    committed_at = Column(DateTime(timezone=True), nullable=True)
    reversed_at = Column(DateTime(timezone=True), nullable=True)
    reversed_by_subject_type = Column(String(64), nullable=True)
    reversed_by_subject_id = Column(String(128), nullable=True)

    __table_args__ = (
        CheckConstraint("status IN ('uploaded', 'previewed', 'committed', 'reversed')", name="ck_estate_import_batches_status"),
    )


class EstateImportRow(Base):
    """One logical record (a customer + optionally their plot/sale/payment) staged within an
    EstateImportBatch - normalized to this one shape regardless of whether the source was a
    single combined sheet or several linked worksheets, see data_import.py."""

    __tablename__ = "estate_import_rows"
    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, ForeignKey("estate_import_batches.id", ondelete="CASCADE"), nullable=False)
    row_index = Column(Integer, nullable=False)
    source_data = Column(JSON, nullable=False)
    match_status = Column(String(16), nullable=False, default="new")
    matched_entity_type = Column(String(32), nullable=True)
    matched_entity_id = Column(Integer, nullable=True)
    resolution = Column(String(16), nullable=False, default="create")
    issues = Column(JSON, nullable=False, default=list)
    committed_customer_id = Column(Integer, ForeignKey("estate_customers.id", ondelete="SET NULL"), nullable=True)
    committed_allocation_id = Column(Integer, ForeignKey("estate_allocations.id", ondelete="SET NULL"), nullable=True)
    committed_payment_id = Column(Integer, ForeignKey("estate_payments.id", ondelete="SET NULL"), nullable=True)
    # Staff-picked plot that overrides automatic plot_number matching for this row (e.g. the
    # sheet's plot text didn't match anything) - set via the row-override endpoint, takes priority
    # over source_data.plot at commit time. See data_import.commit_import_rows.
    manual_plot_id = Column(Integer, ForeignKey("estate_plots.id", ondelete="SET NULL"), nullable=True)
    # Distinguishes "this row created a brand-new record" from "this row matched an existing one"
    # - committed_*_id alone can't tell the two apart, and reversal must only ever undo what the
    # import itself created, never touch a pre-existing record it just happened to match.
    created_new_customer = Column(Boolean, nullable=False, default=False)
    created_new_allocation = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("match_status IN ('new', 'matched', 'ambiguous', 'conflict', 'error')", name="ck_estate_import_rows_match_status"),
        CheckConstraint("resolution IN ('create', 'update', 'skip')", name="ck_estate_import_rows_resolution"),
        Index("idx_estate_import_rows_batch", "batch_id"),
    )


class EstateImportMappingPreset(Base):
    """A saved column-mapping so a developer doesn't have to re-map the same spreadsheet layout
    on every repeat import/export from their own system."""

    __tablename__ = "estate_import_mapping_presets"
    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(160), nullable=False)
    entity_type = Column(String(32), nullable=False)
    mapping = Column(JSON, nullable=False)
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
