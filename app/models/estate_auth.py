from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.sql import func

from app.db_base import Base


class EstateAccount(Base):
    """A company-facing Estate identity, independent from Survey and Green identities."""

    __tablename__ = "estate_accounts"

    id = Column(Integer, primary_key=True)
    account_uid = Column(String(36), nullable=False, unique=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    email = Column(String(255), nullable=False)
    email_normalized = Column(String(255), nullable=False, unique=True)
    full_name = Column(String(255), nullable=False)
    password_hash = Column(String(255), nullable=False)
    status = Column(String(32), nullable=False, default="active")
    trial_claimed_at = Column(DateTime(timezone=True), nullable=True)
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("organization_id", "email_normalized", name="uq_estate_account_org_email"),
        CheckConstraint("status IN ('active', 'suspended', 'archived')", name="ck_estate_accounts_status"),
    )


class EstateAuthSession(Base):
    """Hashed bearer sessions for the Estate web application."""

    __tablename__ = "estate_auth_sessions"

    id = Column(Integer, primary_key=True)
    session_uid = Column(String(64), nullable=False, unique=True)
    access_token_hash = Column(String(64), nullable=False, unique=True)
    account_id = Column(Integer, ForeignKey("estate_accounts.id", ondelete="CASCADE"), nullable=False)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    session_state = Column(String(32), nullable=False, default="active")
    client_label = Column(String(120), nullable=True)
    ip_address = Column(String(255), nullable=True)
    user_agent = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_seen_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoke_reason = Column(String(255), nullable=True)

    __table_args__ = (
        CheckConstraint("session_state IN ('active', 'revoked', 'expired')", name="ck_estate_auth_sessions_state"),
    )
