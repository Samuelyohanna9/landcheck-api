from sqlalchemy import Column, Integer, ForeignKey
from geoalchemy2 import Geometry
from app.db_base import Base


class PlotBuffer(Base):
    __tablename__ = "plot_buffers"

    id = Column(Integer, primary_key=True, index=True)
    plot_id = Column(Integer, ForeignKey("plots.id"))
    geom = Column(Geometry("POLYGON", srid=4326))
