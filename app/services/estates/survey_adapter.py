"""Controlled Estate-to-Survey working-record materialization."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from geoalchemy2.shape import to_shape
from sqlalchemy.orm import Session

from app.models.estate_foundation import Estate, EstateAllocation, EstateCustomer, EstatePlot, EstateSurveyRequest
from app.routers.plots import upsert_plot_meta
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
    if not result.already_exists:
        _prefill_survey_plot_meta(db, request=request, plot=plot, survey_plot_id=result.plot.id)
    return SurveyMaterialization(result.plot.id, result.already_exists)


def _prefill_survey_plot_meta(db: Session, *, request: EstateSurveyRequest, plot: EstatePlot, survey_plot_id: int) -> None:
    """Carry the estate/customer context the surveyor already captured into the new Survey
    working plot, so the surveyor opens a plan that already knows whose land it is instead of a
    blank form - the surveyor still picks the template and confirms every field before export."""
    estate = db.get(Estate, plot.estate_id)
    customer_name = None
    if request.allocation_id:
        allocation = db.get(EstateAllocation, request.allocation_id)
        if allocation:
            customer = db.get(EstateCustomer, allocation.customer_id)
            customer_name = customer.full_name if customer else None
    title_parts = [part for part in [estate.name if estate else None, f"Plot {plot.plot_number}"] if part]
    upsert_plot_meta(
        db,
        plot_id=survey_plot_id,
        title_text=" - ".join(title_parts) or None,
        location_text=estate.location_text if estate else None,
        state_text=estate.state if estate else None,
        adamawa_owner_name=customer_name,
        commit=False,
    )
