from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import main
from app.routers import plots
from app.utils.row_security import ESTATE_LOOKUP_SELECT_TABLES


class _FakeSession:
    def close(self) -> None:
        pass


def test_estate_request_resolves_lookup_then_switches_to_tenant(monkeypatch):
    calls: list[tuple[str, object]] = []
    db = _FakeSession()
    request = SimpleNamespace(url=SimpleNamespace(path="/estates"))

    monkeypatch.setattr(plots, "SessionLocal", lambda: db)
    monkeypatch.setattr(plots, "ensure_plots_schema_once", lambda _db: calls.append(("schema", None)))
    monkeypatch.setattr(plots, "_resolve_estate_organization_ids", lambda _db, _request: [7, 11])
    monkeypatch.setattr(plots, "set_estate_bypass", lambda _db, mode: calls.append(("bypass", mode)))
    monkeypatch.setattr(plots, "set_estate_tenant_context", lambda _db, ids: calls.append(("tenant", ids)))
    monkeypatch.setattr(plots, "clear_estate_tenant_context", lambda _db: calls.append(("clear", None)))

    session_generator = plots.get_db(request)
    assert next(session_generator) is db
    with pytest.raises(StopIteration):
        next(session_generator)

    assert calls == [
        ("schema", None),
        ("bypass", "lookup"),
        ("tenant", [7, 11]),
        ("bypass", None),
        ("clear", None),
    ]


def test_private_estate_middleware_resolves_identity_under_lookup_context(monkeypatch):
    calls: list[tuple[str, object]] = []
    db = _FakeSession()
    request = SimpleNamespace(url=SimpleNamespace(path="/estates/foundation/access"))

    monkeypatch.setattr(main, "SessionLocal", lambda: db)
    monkeypatch.setattr(main, "_requires_green_session", lambda _request: False)
    monkeypatch.setattr(main, "_requires_estate_session", lambda _request: True)
    monkeypatch.setattr(main, "resolve_request_session", lambda _db, _request: None)
    monkeypatch.setattr(main, "resolve_estate_session", lambda _db, _request: object())
    monkeypatch.setattr(main, "set_estate_bypass", lambda _db, mode: calls.append(("bypass", mode)))

    main._prepare_private_request_sync(request)

    assert calls == [("bypass", "lookup"), ("bypass", None)]


def test_private_estate_middleware_clears_lookup_context_when_identity_is_missing(monkeypatch):
    calls: list[tuple[str, object]] = []
    db = _FakeSession()
    request = SimpleNamespace(url=SimpleNamespace(path="/estates/foundation/access"))

    monkeypatch.setattr(main, "SessionLocal", lambda: db)
    monkeypatch.setattr(main, "_requires_green_session", lambda _request: False)
    monkeypatch.setattr(main, "_requires_estate_session", lambda _request: True)
    monkeypatch.setattr(main, "resolve_request_session", lambda _db, _request: None)
    monkeypatch.setattr(main, "resolve_estate_session", lambda _db, _request: None)
    monkeypatch.setattr(main, "set_estate_bypass", lambda _db, mode: calls.append(("bypass", mode)))

    with pytest.raises(main.HTTPException) as error:
        main._prepare_private_request_sync(request)

    assert error.value.status_code == 401
    assert calls == [("bypass", "lookup"), ("bypass", None)]


def test_estate_organization_is_available_for_identity_lookup_only():
    assert "estate_organizations" in ESTATE_LOOKUP_SELECT_TABLES
