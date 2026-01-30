# app/db_init.py

from app.db import engine
from app.db_base import Base  # 👈 THIS IS THE FIX

# Import ALL models so SQLAlchemy registers them
from app.models.plot import Plot
from app.models.plot_buffer import PlotBuffer
from app.models.detected_feature import DetectedFeature


def init_db():
    Base.metadata.create_all(bind=engine)
