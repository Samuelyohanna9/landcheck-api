from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services.estates.survey_requests import transition
from app.routers.estates import _survey_request_owner_user_id


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
