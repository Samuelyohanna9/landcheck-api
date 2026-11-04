import inspect
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services.estates.survey_requests import transition
from app.routers.estates import (
    _load_estate_background_job_estate,
    _run_estate_development_forecast_job,
    _run_estate_hazard_assessment_job,
    _run_estate_hazard_boundary_map_job,
    _run_estate_soil_assessment_job,
    _survey_request_owner_user_id,
)


def test_survey_request_allows_initial_workflow_transitions():
    request = SimpleNamespace(status="requested")
    transition(request, "assigned")
    assert request.status == "assigned"
    transition(request, "in_progress")
    assert request.status == "in_progress"


def test_survey_request_rejects_arbitrary_transition():
    with pytest.raises(HTTPException) as error:
        transition(SimpleNamespace(status="completed"), "in_progress")
    assert error.value.status_code == 409


def test_estate_survey_handoff_uses_recorded_owner_without_rls_plot_lookup():
    row = SimpleNamespace(survey_owner_user_id=42)

    assert _survey_request_owner_user_id(row) == 42


def test_estate_survey_handoff_rejects_legacy_request_without_recorded_owner():
    with pytest.raises(HTTPException) as error:
        _survey_request_owner_user_id(SimpleNamespace(survey_owner_user_id=None))

    assert error.value.status_code == 404


def test_estate_analysis_workers_accept_hazard_queue_owner_argument():
    for worker in (
        _run_estate_development_forecast_job,
        _run_estate_hazard_assessment_job,
        _run_estate_hazard_boundary_map_job,
        _run_estate_soil_assessment_job,
    ):
        parameters = inspect.signature(worker).parameters
        assert "_owner_user_id" in parameters


def test_estate_analysis_worker_loads_under_lookup_then_tenant_context(monkeypatch):
    calls = []

    class FakeDb:
        def get(self, _model, estate_id):
            assert estate_id == 8
            return SimpleNamespace(organization_id=3)

    monkeypatch.setattr("app.routers.estates.set_estate_bypass", lambda _db, mode: calls.append(("bypass", mode)))
    monkeypatch.setattr("app.routers.estates.set_estate_tenant_context", lambda _db, ids: calls.append(("tenant", ids)))

    estate = _load_estate_background_job_estate(FakeDb(), 8)

    assert estate.organization_id == 3
    assert calls == [("bypass", "lookup"), ("bypass", None), ("tenant", [3])]
