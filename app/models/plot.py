from sqlalchemy import Column, Integer, DateTime, Text
from sqlalchemy.sql import func
from geoalchemy2 import Geometry
from app.db_base import Base


class Plot(Base):
    __tablename__ = "plots"

    id = Column(Integer, primary_key=True, index=True)
    geom = Column(Geometry("POLYGON", srid=4326))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    client_request_id = Column(Text, nullable=True)
    owner_user_id = Column(Integer, nullable=True)
