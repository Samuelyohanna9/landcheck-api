from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, Column, DateTime, String, Text, UniqueConstraint
from sqlalchemy.sql import func

from app.db_base import Base


class EstateDemoRequest(Base):
    """A private sales enquiry submitted from the public Estates landing page."""

    __tablename__ = "estate_demo_requests"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    idempotency_key = Column(String(128), nullable=False)
    client_fingerprint = Column(String(64), nullable=True)
    company_name = Column(String(255), nullable=False)
    contact_name = Column(String(255), nullable=False)
    email = Column(String(255), nullable=False)
    phone = Column(String(64), nullable=False)
    message = Column(Text, nullable=True)
    source_path = Column(String(255), nullable=True)
    status = Column(String(32), nullable=False, default="new", server_default="new")
    notification_status = Column(String(32), nullable=False, default="pending", server_default="pending")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    notified_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_estate_demo_requests_idempotency_key"),
        CheckConstraint("status IN ('new', 'contacted', 'closed')", name="ck_estate_demo_requests_status"),
        CheckConstraint("notification_status IN ('pending', 'sent', 'failed')", name="ck_estate_demo_requests_notification_status"),
    )
