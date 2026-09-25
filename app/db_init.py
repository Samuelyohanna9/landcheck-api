# app/db_init.py
from app.db import engine
from app.db_base import Base

# IMPORTANT: import ALL models so they register with Base
from app.models.plot import Plot
from app.models.plot_buffer import PlotBuffer
from app.models.detected_feature import DetectedFeature
from app.models import estate_foundation  # noqa: F401 - register Estate models for local bootstrap.
from app.models import estate_marketing  # noqa: F401 - register Estate marketing models.
from app.models import estate_auth  # noqa: F401 - register Estate identity models for local bootstrap.


def init_db():
    # Estate tables are versioned by Alembic and must not be created or altered
    # implicitly during application startup.
    legacy_tables = [table for table in Base.metadata.sorted_tables if not table.name.startswith("estate_")]
    Base.metadata.create_all(bind=engine, tables=legacy_tables)
