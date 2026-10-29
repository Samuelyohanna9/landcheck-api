from __future__ import annotations

import secrets

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.sql import func

from app.db_base import Base


class EstateCanvaOAuthState(Base):
    __tablename__ = "estate_canva_oauth_states"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    estate_id = Column(Integer, ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False)
    state_hash = Column(String(64), nullable=False, unique=True)
    code_verifier_enc = Column(Text, nullable=False)
    return_path = Column(String(500), nullable=False)
    created_by_subject_type = Column(String(64), nullable=False)
    created_by_subject_id = Column(String(128), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (Index("ix_estate_canva_oauth_states_expiry", "expires_at", "used_at"),)


class EstateCanvaConnection(Base):
    __tablename__ = "estate_canva_connections"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    canva_user_id = Column(String(128), nullable=False)
    canva_team_id = Column(String(128), nullable=False)
    access_token_enc = Column(Text, nullable=False)
    refresh_token_enc = Column(Text, nullable=False)
    access_token_expires_at = Column(DateTime(timezone=True), nullable=False)
    scopes = Column(Text, nullable=False)
    status = Column(String(24), nullable=False, default="active")
    connected_by_subject_type = Column(String(64), nullable=True)
    connected_by_subject_id = Column(String(128), nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("organization_id", "canva_user_id", "canva_team_id", name="uq_estate_canva_connection"),
        Index("ix_estate_canva_connections_org_status", "organization_id", "status"),
    )


class EstateCanvaDesign(Base):
    __tablename__ = "estate_canva_designs"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    estate_id = Column(Integer, ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False)
    connection_id = Column(Integer, ForeignKey("estate_canva_connections.id", ondelete="CASCADE"), nullable=False)
    canva_design_id = Column(String(128), nullable=False)
    canva_user_id = Column(String(128), nullable=False)
    canva_team_id = Column(String(128), nullable=False)
    title = Column(String(255), nullable=False)
    source_format = Column(String(16), nullable=False, default="post")
    source_style = Column(String(32), nullable=False, default="promo")
    source_plot_id = Column(Integer, ForeignKey("estate_plots.id", ondelete="SET NULL"), nullable=True)
    return_key_hash = Column(String(64), nullable=False, unique=True)
    r2_object_key = Column(String(500), nullable=True)
    r2_content_type = Column(String(80), nullable=True)
    status = Column(String(24), nullable=False, default="created")
    last_error = Column(Text, nullable=True)
    last_return_at = Column(DateTime(timezone=True), nullable=True)
    imported_at = Column(DateTime(timezone=True), nullable=True)
    created_by_subject_type = Column(String(64), nullable=False)
    created_by_subject_id = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_estate_canva_designs_estate_status", "estate_id", "status", "created_at"),
        Index("ix_estate_canva_designs_canva_design", "connection_id", "canva_design_id"),
    )


def new_canva_return_key() -> str:
    return secrets.token_urlsafe(24)
