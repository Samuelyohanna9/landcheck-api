"""Survey plot creation business logic shared by HTTP and internal callers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from geoalchemy2.shape import from_shape
from shapely.geometry import Polygon
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.plot import Plot


@dataclass(frozen=True, slots=True)
class SurveyPlotCreation:
    plot: Plot
    already_exists: bool = False


def create_survey_plot(
    db: Session,
    *,
    coordinates: Sequence[Sequence[float]],
    owner_user_id: int | None,
    client_request_id: str | None = None,
) -> SurveyPlotCreation:
    """Add and flush a Survey plot; the caller owns the transaction boundary."""
    request_id = str(client_request_id or "").strip() or None
    if request_id:
        existing_id = db.execute(
            text("SELECT id FROM plots WHERE client_request_id = :client_request_id LIMIT 1"),
            {"client_request_id": request_id},
        ).scalar()
        if existing_id is not None:
            return SurveyPlotCreation(db.get(Plot, int(existing_id)), already_exists=True)

    plot = Plot(
        geom=from_shape(Polygon(coordinates), srid=4326),
        client_request_id=request_id,
        owner_user_id=owner_user_id,
    )
    db.add(plot)
    db.flush()
    return SurveyPlotCreation(plot)
