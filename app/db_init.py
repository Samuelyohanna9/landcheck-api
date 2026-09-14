# app/db_init.py
from app.db import engine
from app.db_base import Base

# IMPORTANT: import ALL models so they register with Base
from app.models.plot import Plot
from app.models.plot_buffer import PlotBuffer
from app.models.detected_feature import DetectedFeature
from app.models import estate_foundation  # noqa: F401 - register Estate models for local bootstrap.
from app.models import estate_auth  # noqa: F401 - register Estate identity models for local bootstrap.


def init_db():
    Base.metadata.create_all(bind=engine)
