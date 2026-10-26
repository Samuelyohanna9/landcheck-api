from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException, Response

from app.models.estate_auth import EstateAccount
from app.models.estate_foundation import EstateOrganization
from app.routers.estate_auth import login, register, verify_email
from app.schemas.estate_auth import EstateLogin, EstateRegister
from app.services.estates import dpa, estate_email


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


def test_agent_registration_verifies_into_agent_login(db_session, monkeypatch):
    verification = {}
    welcome = {}
    monkeypatch.setattr(
        "app.services.estates.estate_email.send_email_verification_email",
        lambda **kwargs: verification.update(kwargs) or True,
    )
    monkeypatch.setattr(
        "app.services.estates.estate_email.send_welcome_email",
        lambda **kwargs: welcome.update(kwargs) or True,
    )

    result = register(_payload(workspace_type="agent"), _FakeRequest(), db_session)
    verification_url = verification["verification_url"]
    query = parse_qs(urlparse(verification_url).query)
    response = verify_email(query["token"][0], workspace=query["workspace"][0], db=db_session)

    assert result["message"].endswith("LandCheck Agents.")
    assert verification["workspace_type"] == "agent"
    assert response.status_code == 303
    assert response.headers["location"].endswith("/agents/email-verified")
    assert welcome["workspace_type"] == "agent"


def test_agent_welcome_email_uses_property_sales_onboarding(monkeypatch):
    sent = {}
    monkeypatch.setattr(estate_email, "_send_email", lambda **kwargs: sent.update(kwargs))

    assert estate_email.send_welcome_email(
        organization=SimpleNamespace(name="Ada Property Services"),
        account=SimpleNamespace(email="ada@example.com", full_name="Ada Owner"),
        workspace_type="agent",
    ) is True

    assert sent["subject"] == "Welcome to LandCheck Agents - start selling property professionally"
    assert sent["from_display_name"] == "LandCheck Agents"
    assert "Add a property" in sent["body_text"]
    assert "Facebook" in sent["body_text"]
    assert "Basic" not in sent["body_text"]


def test_legacy_agent_login_establishes_free_workspace_mode(db_session, monkeypatch):
    monkeypatch.setattr("app.services.estates.estate_email.send_email_verification_email", lambda **kwargs: True)
    result = register(_payload(), _FakeRequest(), db_session)
    account = db_session.query(EstateAccount).filter(EstateAccount.email == "ada@example.com").one()
    organization = db_session.get(EstateOrganization, result["organization"]["id"])
    organization.settings = {}
    account.email_verified_at = datetime.utcnow()
    db_session.commit()

    session = login(
        EstateLogin(email="ada@example.com", password="a-strong-password", workspace_type="agent"),
        _FakeRequest(),
        Response(),
        db_session,
    )

    assert session["needs_plan"] is False
    assert session["user"]["workspace_type"] == "agent"
    organization = db_session.get(EstateOrganization, result["organization"]["id"])
    assert organization.settings["workspace_type"] == "agent"
