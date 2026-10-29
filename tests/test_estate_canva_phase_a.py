from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models.estate_canva import EstateCanvaConnection
from app.routers.estate_canva import _connection as lookup_connection
from app.services.estates import canva_connect


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, content: bytes = b""):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = content

    def json(self):
        return self._payload


class DummyDb:
    def __init__(self):
        self.flushes = 0

    def flush(self):
        self.flushes += 1


@pytest.fixture()
def canva_env(monkeypatch):
    monkeypatch.setenv("CANVA_ENABLED", "true")
    monkeypatch.setenv("CANVA_CLIENT_ID", "client-id")
    monkeypatch.setenv("CANVA_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("CANVA_SECRET_KEY", "local-test-secret")


def _connection() -> EstateCanvaConnection:
    return EstateCanvaConnection(
        organization_id=11,
        canva_user_id="canva-user",
        canva_team_id="canva-team",
        access_token_enc=canva_connect.encrypt_token("old-access"),
        refresh_token_enc=canva_connect.encrypt_token("old-refresh"),
        access_token_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        scopes=canva_connect.DEFAULT_SCOPES,
        status="active",
    )


def test_pkce_pair_and_authorization_url_use_s256(canva_env):
    verifier, challenge = canva_connect.pkce_pair()
    url = canva_connect.authorization_url(state="state-value", challenge=challenge)

    assert verifier
    assert "code_challenge_method=S256" in url
    assert "client_id=client-id" in url
    assert "state=state-value" in url
    assert "design%3Acontent%3Aread" in url


def test_oauth_exchange_surfaces_provider_failure(canva_env, monkeypatch):
    monkeypatch.setattr(canva_connect.requests, "post", lambda *args, **kwargs: FakeResponse(400, {"message": "invalid_grant"}))

    with pytest.raises(canva_connect.CanvaApiError) as error:
        canva_connect.exchange_code("expired-code", "verifier")

    assert error.value.status == 400
    assert "invalid_grant" in str(error.value)


def test_expired_access_token_refreshes_and_rotates_refresh_token(canva_env, monkeypatch):
    db = DummyDb()
    connection = _connection()
    monkeypatch.setattr(
        canva_connect.requests,
        "post",
        lambda *args, **kwargs: FakeResponse(200, {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}),
    )

    assert canva_connect.access_token(db, connection) == "new-access"
    assert canva_connect.decrypt_token(connection.access_token_enc) == "new-access"
    assert canva_connect.decrypt_token(connection.refresh_token_enc) == "new-refresh"
    assert db.flushes == 1


def test_request_retries_once_after_expired_token(canva_env, monkeypatch):
    db = DummyDb()
    connection = _connection()
    connection.access_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    responses = iter([FakeResponse(401), FakeResponse(200, {"ok": True})])
    refresh = lambda *args, **kwargs: FakeResponse(200, {"access_token": "refreshed", "refresh_token": "rotated", "expires_in": 3600})
    monkeypatch.setattr(canva_connect.requests, "request", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(canva_connect.requests, "post", refresh)

    assert canva_connect.request_json(db, connection, "GET", "/users/me") == {"ok": True}


def test_export_failure_is_reported_without_download(canva_env, monkeypatch):
    db = DummyDb()
    connection = _connection()
    monkeypatch.setattr(
        canva_connect,
        "request_json",
        lambda *args, **kwargs: {"job": {"id": "export-1"}} if kwargs.get("json_body") else {"job": {"status": "failed", "error": {"code": "approval_required", "message": "approval required"}}},
    )
    monkeypatch.setattr(canva_connect.requests, "get", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("failed exports must not download")))

    with pytest.raises(canva_connect.CanvaApiError) as error:
        canva_connect.export_design(db, connection, "design-1", "png")

    assert error.value.code == "approval_required"


def test_invalid_return_token_is_rejected_before_network_access(canva_env):
    with pytest.raises(canva_connect.CanvaApiError) as error:
        canva_connect.verify_correlation_jwt("not-a-jwt")

    assert error.value.code == "invalid_return"


def test_connection_lookup_is_organization_scoped(db_session, canva_env):
    EstateCanvaConnection.__table__.create(bind=db_session.get_bind(), checkfirst=True)
    try:
        db_session.add_all([
            EstateCanvaConnection(
                organization_id=101, canva_user_id="user-a", canva_team_id="team-a",
                access_token_enc="a", refresh_token_enc="a", access_token_expires_at=datetime.now(timezone.utc),
                scopes=canva_connect.DEFAULT_SCOPES, status="active",
            ),
            EstateCanvaConnection(
                organization_id=202, canva_user_id="user-b", canva_team_id="team-b",
                access_token_enc="b", refresh_token_enc="b", access_token_expires_at=datetime.now(timezone.utc),
                scopes=canva_connect.DEFAULT_SCOPES, status="active",
            ),
        ])
        db_session.commit()

        row = lookup_connection(db_session, 101)

        assert row.canva_user_id == "user-a"
        assert lookup_connection(db_session, 202).canva_user_id == "user-b"
    finally:
        EstateCanvaConnection.__table__.drop(bind=db_session.get_bind(), checkfirst=True)


def test_revoke_uses_backend_only_token_endpoint(canva_env, monkeypatch):
    calls = []
    monkeypatch.setattr(
        canva_connect.requests,
        "post",
        lambda *args, **kwargs: calls.append((args, kwargs)) or FakeResponse(200, {}),
    )

    canva_connect.revoke_token("refresh-token")

    assert calls[0][0][0].endswith("/oauth/revoke")
    assert calls[0][1]["data"] == {"token": "refresh-token"}
    assert calls[0][1]["auth"] == ("client-id", "client-secret")
