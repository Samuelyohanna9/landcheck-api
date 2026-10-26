from __future__ import annotations

from app.models.estate_foundation import EstateOrganization
from app.services.estates.subscriptions import (
    estate_limit_for_organization,
    has_auto_posting_access_for_organization,
    has_hazard_access_for_organization,
    has_live_inspection_access_for_organization,
    has_sms_access_for_organization,
    has_soil_analysis_access_for_organization,
    has_whatsapp_access_for_organization,
)


def test_agent_workspace_has_all_features_without_subscription(db_session):
    organization = EstateOrganization(
        name="Free Agent Workspace",
        slug="free-agent-workspace",
        contact_email="agent@example.com",
        settings={"workspace_type": "agent"},
    )
    db_session.add(organization)
    db_session.commit()

    assert estate_limit_for_organization(db_session, organization.id) is None
    assert has_hazard_access_for_organization(db_session, organization.id) is True
    assert has_soil_analysis_access_for_organization(db_session, organization.id) is True
    assert has_live_inspection_access_for_organization(db_session, organization.id) is True
    assert has_sms_access_for_organization(db_session, organization.id) is True
    assert has_whatsapp_access_for_organization(db_session, organization.id) is True
    assert has_auto_posting_access_for_organization(db_session, organization.id) is True


def test_estate_workspace_without_subscription_keeps_plan_limits(db_session):
    organization = EstateOrganization(
        name="Unpaid Estate Workspace",
        slug="unpaid-estate-workspace",
        contact_email="estate@example.com",
        settings={"workspace_type": "estate"},
    )
    db_session.add(organization)
    db_session.commit()

    assert estate_limit_for_organization(db_session, organization.id) == 1
    assert has_hazard_access_for_organization(db_session, organization.id) is False
    assert has_soil_analysis_access_for_organization(db_session, organization.id) is False
    assert has_live_inspection_access_for_organization(db_session, organization.id) is False
    assert has_sms_access_for_organization(db_session, organization.id) is False
    assert has_whatsapp_access_for_organization(db_session, organization.id) is False
    assert has_auto_posting_access_for_organization(db_session, organization.id) is False
