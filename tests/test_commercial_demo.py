from __future__ import annotations

from fastapi import HTTPException, Request
from pydantic import ValidationError

from app.models.commercial import EstateDemoRequest
from app.routers import commercial
from app.schemas.commercial import EstateDemoRequestCreate
from app.services.estates import estate_email


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/commercial/estate-demo",
            "headers": [],
            "client": ("127.0.0.1", 43210),
            "server": ("testserver", 8000),
        }
    )


def _payload(**overrides) -> EstateDemoRequestCreate:
    values = {
        "company_name": "Example Estates",
        "contact_name": "Ada Example",
        "email": "ada@example.com",
        "phone": "+234 800 000 0000",
        "message": "Please show plot and payment workflows.",
        "source_path": "/estates/",
    }
    values.update(overrides)
    return EstateDemoRequestCreate(**values)


def test_demo_request_validates_phone_without_line_breaks():
    try:
        EstateDemoRequestCreate(
            company_name="Example Estates",
            contact_name="Ada Example",
            email="ada@example.com",
            phone="+234 800\n000 0000",
        )
    except ValidationError:
        pass
    else:
        raise AssertionError("phone line breaks must be rejected")


def test_demo_honeypot_is_accepted_without_storage(db_session):
    EstateDemoRequest.__table__.create(bind=db_session.get_bind(), checkfirst=True)
    try:
        result = commercial.submit_estate_demo_request(
            _request(), _payload(website="https://bot.invalid"), idempotency_key="bot-request", db=db_session
        )
        assert result["status"] == "received"
        assert result["request_id"] is None
        assert db_session.query(EstateDemoRequest).count() == 0
    finally:
        EstateDemoRequest.__table__.drop(bind=db_session.get_bind(), checkfirst=True)


def test_demo_request_is_idempotent_and_notified_once(db_session, monkeypatch):
    EstateDemoRequest.__table__.create(bind=db_session.get_bind(), checkfirst=True)
    notifications = []
    monkeypatch.setattr(
        commercial.estate_email,
        "send_estate_demo_request_notification",
        lambda **kwargs: notifications.append(kwargs) or True,
    )
    try:
        first = commercial.submit_estate_demo_request(
            _request(), _payload(), idempotency_key="demo-request-1", db=db_session
        )
        second = commercial.submit_estate_demo_request(
            _request(), _payload(), idempotency_key="demo-request-1", db=db_session
        )

        assert first["status"] == "received"
        assert first["already_processed"] is False
        assert second["already_processed"] is True
        assert second["request_id"] == first["request_id"]
        assert len(notifications) == 1
        assert db_session.query(EstateDemoRequest).count() == 1
    finally:
        EstateDemoRequest.__table__.drop(bind=db_session.get_bind(), checkfirst=True)


def test_demo_request_is_kept_when_notification_fails(db_session, monkeypatch):
    EstateDemoRequest.__table__.create(bind=db_session.get_bind(), checkfirst=True)
    monkeypatch.setattr(commercial.estate_email, "send_estate_demo_request_notification", lambda **kwargs: False)
    try:
        result = commercial.submit_estate_demo_request(
            _request(), _payload(), idempotency_key="demo-request-failed-notification", db=db_session
        )
        stored = db_session.query(EstateDemoRequest).filter_by(idempotency_key="demo-request-failed-notification").one()
        assert result["status"] == "received"
        assert result["notification_sent"] is False
        assert stored.notification_status == "failed"
        monkeypatch.setattr(commercial.estate_email, "send_estate_demo_request_notification", lambda **kwargs: True)
        retried = commercial.submit_estate_demo_request(
            _request(), _payload(), idempotency_key="demo-request-failed-notification", db=db_session
        )
        db_session.refresh(stored)
        assert retried["already_processed"] is True
        assert retried["notification_sent"] is True
        assert stored.notification_status == "sent"
    finally:
        EstateDemoRequest.__table__.drop(bind=db_session.get_bind(), checkfirst=True)


def test_demo_rate_limit_is_shared_by_database_state(db_session, monkeypatch):
    EstateDemoRequest.__table__.create(bind=db_session.get_bind(), checkfirst=True)
    monkeypatch.setattr(
        commercial.estate_email,
        "send_estate_demo_request_notification",
        lambda **kwargs: True,
    )
    try:
        for index in range(5):
            result = commercial.submit_estate_demo_request(
                _request(), _payload(), idempotency_key=f"rate-limit-{index}", db=db_session
            )
            assert result["status"] == "received"

        try:
            commercial.submit_estate_demo_request(
                _request(), _payload(), idempotency_key="rate-limit-blocked", db=db_session
            )
        except HTTPException as exc:
            assert exc.status_code == 429
            assert exc.headers["Retry-After"] == "3600"
        else:
            raise AssertionError("the sixth request must be rate limited")
    finally:
        EstateDemoRequest.__table__.drop(bind=db_session.get_bind(), checkfirst=True)


def test_demo_notification_escapes_body_and_subject(monkeypatch):
    sent = {}
    monkeypatch.setattr(estate_email, "_send_email", lambda **kwargs: sent.update(kwargs))

    assert estate_email.send_estate_demo_request_notification(
        request_id="request-1",
        company_name="Acme\r\nInjected: no",
        contact_name="<Contact>",
        email="contact@example.com",
        phone="+234 800 000 0000",
        message="<script>alert(1)</script>",
    ) is True
    assert "\r" not in sent["subject"]
    assert "\n" not in sent["subject"]
    assert "&lt;script&gt;" in sent["body_html"]
    assert "<script>" not in sent["body_html"]
