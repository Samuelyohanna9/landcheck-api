from __future__ import annotations

from app.models.estate_foundation import EstateOrganization
from app.services.estates import dpa


def _organization(db_session) -> EstateOrganization:
    organization = EstateOrganization(name="DPA Test", slug="dpa-test", contact_email="owner@example.com")
    db_session.add(organization)
    db_session.commit()
    return organization


def test_no_acceptance_yet(db_session):
    organization = _organization(db_session)
    assert dpa.latest_acceptance(db_session, organization.id) is None
    assert dpa.has_accepted_current(db_session, organization.id) is False


def test_recording_acceptance_marks_current_version_accepted(db_session):
    organization = _organization(db_session)
    row = dpa.record_acceptance(
        db_session, organization_id=organization.id, subject_type="estate_account", subject_id="7",
        name="Ada Owner", email="ada@example.com", ip_address="41.58.0.1",
    )
    db_session.commit()

    assert row.version == dpa.DPA_VERSION
    assert dpa.has_accepted_current(db_session, organization.id) is True
    latest = dpa.latest_acceptance(db_session, organization.id)
    assert latest.accepted_by_name == "Ada Owner"
    assert latest.accepted_by_email == "ada@example.com"


def test_acceptance_is_per_organization(db_session):
    mine = _organization(db_session)
    other = EstateOrganization(name="Other Co", slug="other-co", contact_email="other@example.com")
    db_session.add(other)
    db_session.commit()

    dpa.record_acceptance(db_session, organization_id=mine.id, subject_type="estate_account", subject_id="1", name=None, email=None, ip_address=None)
    db_session.commit()

    assert dpa.has_accepted_current(db_session, mine.id) is True
    assert dpa.has_accepted_current(db_session, other.id) is False


def test_an_old_version_no_longer_counts_as_accepted(db_session, monkeypatch):
    organization = _organization(db_session)
    monkeypatch.setattr(dpa, "DPA_VERSION", "2020-01-01")
    dpa.record_acceptance(db_session, organization_id=organization.id, subject_type="estate_account", subject_id="1", name=None, email=None, ip_address=None)
    db_session.commit()
    assert dpa.has_accepted_current(db_session, organization.id) is True

    monkeypatch.setattr(dpa, "DPA_VERSION", "2026-09-27")
    assert dpa.has_accepted_current(db_session, organization.id) is False


def test_re_accepting_appends_rather_than_overwrites(db_session):
    organization = _organization(db_session)
    dpa.record_acceptance(db_session, organization_id=organization.id, subject_type="estate_account", subject_id="1", name="First", email=None, ip_address=None)
    dpa.record_acceptance(db_session, organization_id=organization.id, subject_type="estate_account", subject_id="1", name="First again", email=None, ip_address=None)
    db_session.commit()

    rows = db_session.query(type(dpa.latest_acceptance(db_session, organization.id))).filter_by(organization_id=organization.id).all()
    assert len(rows) == 2
    assert dpa.latest_acceptance(db_session, organization.id).accepted_by_name == "First again"
