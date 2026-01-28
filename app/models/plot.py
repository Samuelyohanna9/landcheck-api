from sqlalchemy import Column, Integer
from geoalchemy2 import Geometry
from app.db import Base

class Plot(Base):
    __tablename__ = "plots"

    id = Column(Integer, primary_key=True, index=True)
    geom = Column(Geometry("POLYGON", srid=4326))
