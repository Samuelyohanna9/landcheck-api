from app.db import Base, engine

# Import ALL models so SQLAlchemy registers them
from app.models.plot import Plot
from app.models.plot_buffer import PlotBuffer
from app.models.detected_feature import DetectedFeature


def init_db():
    Base.metadata.create_all(bind=engine)

if __name__ == "__main__":
    init_db()
