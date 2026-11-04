from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.routers import plots


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
