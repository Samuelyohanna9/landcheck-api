from sqlalchemy import Column, Integer, String, ForeignKey
from geoalchemy2 import Geometry
from app.db import Base

class DetectedFeature(Base):
    __tablename__ = "detected_features"

    id = Column(Integer, primary_key=True, index=True)
    plot_id = Column(Integer, ForeignKey("plots.id"))
    feature_type = Column(String)   # building, road, river, tree
    location = Column(String)       # inside | buffer
    geom = Column(Geometry("GEOMETRY", srid=4326))
