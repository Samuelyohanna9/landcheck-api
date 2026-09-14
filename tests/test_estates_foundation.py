from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.estate_foundation import EstateAuditEvent, EstateOrganization, EstateOrganizationEntitlement, EstateOrganizationMember
from app.services.estates import authorization
from app.services.estates.audit import append_estate_audit_event
from app.services.estates.authorization import EstatePrincipal, list_estate_access, require_estate_access, resolve_estate_principal
from app.services.estates.entitlements import get_estate_entitlement
from app.services.estates.permissions import has_permission


def _request() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace())


def _organization(db_session, slug: str) -> EstateOrganization:
    organization = EstateOrganization(name=f"{slug.title()} Properties", slug=slug)
    db_session.add(organization)
    db_session.flush()
    return organization


def _membership(db_session, organization: EstateOrganization, *, subject_id: str, role: str) -> None:
    db_session.add(
        EstateOrganizationMember(
            organization_id=organization.id,
            subject_type="survey_user",
            subject_id=subject_id,
            role_key=role,
        )
    )
    db_session.commit()


def test_estate_principal_requires_an_existing_authenticated_session(db_session, monkeypatch):
    monkeypatch.setattr(authorization, "resolve_survey_session", lambda *_: None)
    monkeypatch.setattr(authorization, "resolve_request_session", lambda *_: None)

    with pytest.raises(HTTPException) as error:
        resolve_estate_principal(db_session, _request())

    assert error.value.status_code == 401


def test_estate_principal_reuses_survey_identity_without_changing_survey_auth(db_session, monkeypatch):
    survey_session = SimpleNamespace(user_id=42, full_name="Survey User", email="survey@example.test")
    monkeypatch.setattr(authorization, "resolve_survey_session", lambda *_: survey_session)

    principal = resolve_estate_principal(db_session, _request())

    assert principal == EstatePrincipal("survey_user", "42", "Survey User")


def test_estate_principal_reuses_existing_landcheck_identity(db_session, monkeypatch):
    monkeypatch.setattr(authorization, "resolve_survey_session", lambda *_: None)
    landcheck_session = SimpleNamespace(
        subject_type="green_user",
        subject_id=7,
        user_id=7,
        display_name="Estate Manager",
    )
    monkeypatch.setattr(authorization, "resolve_request_session", lambda *_: landcheck_session)

    principal = resolve_estate_principal(db_session, _request())

    assert principal == EstatePrincipal("green_user", "7", "Estate Manager")


def test_organization_scope_is_derived_from_membership_not_client_input(db_session, monkeypatch):
    first = _organization(db_session, "first-estate")
    second = _organization(db_session, "second-estate")
    _membership(db_session, first, subject_id="42", role="manager")
    principal = EstatePrincipal("survey_user", "42", "Survey User")
    monkeypatch.setattr(authorization, "resolve_estate_principal", lambda *_: principal)

    allowed = require_estate_access(db_session, _request(), first.id, permission="estate.manage")
    assert allowed.organization_id == first.id

    with pytest.raises(HTTPException) as error:
        require_estate_access(db_session, _request(), second.id, permission="estate.read")
    assert error.value.status_code == 404


def test_role_permissions_are_server_side_and_least_privilege():
    assert has_permission("owner", "payment.manage")
    assert has_permission("accounts", "payment.manage")
    assert not has_permission("surveyor", "payment.manage")
    assert has_permission("surveyor", "survey.manage")
    assert has_permission("sales", "allocation.manage")
    assert not has_permission("viewer", "plot.manage")


def test_list_access_excludes_inactive_memberships_and_organizations(db_session):
    active = _organization(db_session, "active-estate")
    suspended = _organization(db_session, "suspended-estate")
    suspended.status = "suspended"
    db_session.add(
        EstateOrganizationMember(
            organization_id=active.id,
            subject_type="survey_user",
            subject_id="42",
            role_key="viewer",
            is_active=False,
        )
    )
    db_session.add(
        EstateOrganizationMember(
            organization_id=suspended.id,
            subject_type="survey_user",
            subject_id="42",
            role_key="owner",
        )
    )
    db_session.commit()

    assert list_estate_access(db_session, EstatePrincipal("survey_user", "42", None)) == []


def test_audit_writer_is_append_only_and_snapshots_input(db_session):
    organization = _organization(db_session, "audit-estate")
    before = {"status": "draft"}
    event = append_estate_audit_event(
        db_session,
        organization_id=organization.id,
        actor=EstatePrincipal("survey_user", "42", "Survey User"),
        action="estate.foundation.created",
        entity_type="estate_organization",
        entity_id=organization.id,
        before_data=before,
        after_data={"status": "active"},
        metadata={"source": "test"},
    )
    before["status"] = "mutated"
    db_session.commit()

    stored = db_session.query(EstateAuditEvent).filter(EstateAuditEvent.id == event.id).one()
    assert stored.before_data == {"status": "draft"}
    assert stored.actor_subject_id == "42"
    assert stored.action == "estate.foundation.created"


def test_entitlements_require_global_gate_and_allow_organization_override(db_session, monkeypatch):
    organization = _organization(db_session, "entitlement-estate")
    monkeypatch.setenv("ESTATES_ENABLED", "true")
    assert get_estate_entitlement(db_session, organization.id, "ESTATES_ENABLED").is_enabled

    db_session.add(
        EstateOrganizationEntitlement(
            organization_id=organization.id,
            feature_key="ESTATES_ENABLED",
            is_enabled=False,
            limit_value=0,
        )
    )
    db_session.commit()
    assert not get_estate_entitlement(db_session, organization.id, "ESTATES_ENABLED").is_enabled

    monkeypatch.setenv("ESTATES_ENABLED", "false")
    assert not get_estate_entitlement(db_session, organization.id, "ESTATES_ENABLED").is_enabled


def test_initial_migration_is_reversible_and_protects_audit_rows():
    migration = Path(__file__).parents[1] / "alembic" / "versions" / "20260913_0001_estates_foundation.py"
    content = migration.read_text(encoding="utf-8")
    assert "def upgrade" in content
    assert "def downgrade" in content
    assert "estate_audit_events_no_update_delete" in content
    assert "DROP TRIGGER IF EXISTS estate_audit_events_no_update_delete" in content
