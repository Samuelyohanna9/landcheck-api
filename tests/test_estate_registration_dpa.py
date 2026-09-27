from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.models.estate_foundation import EstateOrganization
from app.routers.estate_auth import register
from app.schemas.estate_auth import EstateRegister
from app.services.estates import dpa


class _FakeRequest:
    def __init__(self):
        self.headers: dict[str, str] = {}
        self.client = None


def _payload(**overrides) -> EstateRegister:
    fields = dict(organization_name="Bloomfield Estates", full_name="Ada Owner", email="ada@example.com", password="a-strong-password", accept_dpa=True)
    fields.update(overrides)
    return EstateRegister(**fields)


def test_registration_is_refused_without_accepting_the_dpa(db_session):
    with pytest.raises(HTTPException) as excinfo:
        register(_payload(accept_dpa=False), _FakeRequest(), db_session)
    assert excinfo.value.status_code == 422
    assert db_session.query(EstateOrganization).count() == 0


def test_accepting_at_registration_is_recorded_immediately(db_session, monkeypatch):
    monkeypatch.setattr("app.services.estates.estate_email.send_welcome_email", lambda **kwargs: False)
    result = register(_payload(), _FakeRequest(), db_session)

    organization_id = result["organization"]["id"]
    assert dpa.has_accepted_current(db_session, organization_id) is True
    latest = dpa.latest_acceptance(db_session, organization_id)
    assert latest.accepted_by_name == "Ada Owner"
    assert latest.accepted_by_email == "ada@example.com"
    assert latest.accepted_by_subject_type == "estate_account"
