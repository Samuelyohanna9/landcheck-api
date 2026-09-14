"""Controlled Estate-to-Survey working-record materialization."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from geoalchemy2.shape import to_shape
from sqlalchemy.orm import Session

from app.models.estate_foundation import EstatePlot, EstateSurveyRequest
from app.services.estates.qc import validate_polygon
from app.services.survey.plots import create_survey_plot


@dataclass(frozen=True, slots=True)
class SurveyMaterialization:
    survey_plot_id: int
    already_materialized: bool


def materialize_estate_plot_for_survey(db: Session, *, request: EstateSurveyRequest, plot: EstatePlot, survey_owner_user_id: int) -> SurveyMaterialization:
    if request.survey_working_plot_id:
        return SurveyMaterialization(request.survey_working_plot_id, True)
    geometry = to_shape(plot.geometry)
    geojson = geometry.__geo_interface__
    _, issues = validate_polygon(geojson)
    if any(issue.severity == "error" for issue in issues):
        raise ValueError("Estate plot geometry is not valid for Survey")
    result = create_survey_plot(db, coordinates=list(geometry.exterior.coords)[:-1], owner_user_id=survey_owner_user_id, client_request_id=f"estate-survey-{request.request_uid}")
    request.survey_owner_user_id = survey_owner_user_id
    request.survey_working_plot_id = result.plot.id
    request.survey_reference = f"SURVEY-{result.plot.id}"
    request.materialized_at = datetime.now(timezone.utc)
    return SurveyMaterialization(result.plot.id, result.already_exists)
