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
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("organization_id", "subject_type", "subject_id", name="uq_estate_member_subject"),
        CheckConstraint(
            "role_key IN ('owner', 'manager', 'accounts', 'surveyor', 'field_officer', 'sales', 'viewer')",
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
    approximate_area_sqm = Column(Numeric(16, 2), nullable=True)
    project_reference = Column(String(120), nullable=True)
    project_owner = Column(String(255), nullable=True)
    ownership_details = Column(Text, nullable=True)
    boundary = Column(Geometry("POLYGON", srid=4326), nullable=True)
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
    allocation_date = Column(DateTime(timezone=True), nullable=True)
    agreed_price = Column(Numeric(16, 2), nullable=True)
    payment_plan = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    cancellation_reason = Column(Text, nullable=True)
    created_by_subject_type = Column(String(64), nullable=False)
    created_by_subject_id = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


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
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


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
