from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services.estates.survey_requests import transition


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
