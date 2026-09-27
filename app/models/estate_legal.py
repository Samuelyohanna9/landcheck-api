from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.sql import func

from app.db_base import Base


class EstateDpaAcceptance(Base):
    """A record that someone at an Estate company clicked "I agree" to the Data Processing
    Agreement (see docs/legal/DATA_PROCESSING_AGREEMENT.md). Append-only: accepting again (for
    example after the document changes and `dpa.DPA_VERSION` is bumped) adds a new row rather than
    overwriting the old one, so this table is also the acceptance audit trail."""

    __tablename__ = "estate_dpa_acceptances"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    version = Column(String(20), nullable=False)
    accepted_by_subject_type = Column(String(64), nullable=False)
    accepted_by_subject_id = Column(String(128), nullable=False)
    accepted_by_name = Column(String(255), nullable=True)
    accepted_by_email = Column(String(255), nullable=True)
    ip_address = Column(String(64), nullable=True)
    accepted_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
