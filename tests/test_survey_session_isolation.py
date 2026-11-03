from __future__ import annotations

from types import SimpleNamespace

from app import db as database
from app.utils import row_security


class _FakeCursor:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, statement):
        self.connection.statements.append(statement)

    def close(self):
        self.connection.cursor_closed = True


class _FakeDbapiConnection:
    def __init__(self):
        self.rollback_count = 0
        self.commit_count = 0
        self.statements: list[str] = []
        self.cursor_closed = False

    def rollback(self):
        self.rollback_count += 1

    def commit(self):
        self.commit_count += 1

    def cursor(self):
        return _FakeCursor(self)


def test_checkout_scrubs_context_before_connection_reuse():
    connection = _FakeDbapiConnection()

    database._on_survey_context_checkout(connection, None, None)

    assert connection.statements == ["RESET app.survey_user_id"]
    assert connection.rollback_count == 1
    assert connection.commit_count == 1
    assert connection.cursor_closed is True


def test_pool_reset_scrubs_context_unless_connection_is_being_terminated():
    connection = _FakeDbapiConnection()

    database._on_survey_context_reset(connection, None, SimpleNamespace(terminate_only=False))

    assert connection.statements == ["RESET app.survey_user_id"]

    terminated = _FakeDbapiConnection()
    database._on_survey_context_reset(terminated, None, SimpleNamespace(terminate_only=True))
    assert terminated.statements == []


def test_pool_cleanup_rolls_back_and_surfaces_reset_failures():
    class _FailingConnection(_FakeDbapiConnection):
        def cursor(self):
            raise RuntimeError("database reset failed")

    connection = _FailingConnection()

    try:
        database._on_survey_context_checkout(connection, None, None)
    except RuntimeError as exc:
        assert str(exc) == "database reset failed"
    else:
        raise AssertionError("pool cleanup must not hide a reset failure")

    # The handler attempts a second rollback after the failed RESET, leaving the connection
    # safe for SQLAlchemy to invalidate rather than returning a poisoned transaction to the pool.
    assert connection.rollback_count == 2
    assert connection.commit_count == 0


def test_request_cleanup_rolls_back_before_reset_and_commit(monkeypatch):
    calls: list[str] = []

    class _FakeSession:
        def rollback(self):
            calls.append("rollback")

        info = {}

        def execute(self, statement):
            calls.append(str(statement))

        def commit(self):
            calls.append("commit")

    monkeypatch.setattr(
        row_security,
        "engine",
        SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
    )
    row_security.clear_survey_user_context(_FakeSession())

    assert calls == ["rollback", "RESET app.survey_user_id", "commit"]


def test_all_context_writers_have_explicit_or_pool_cleanup_paths():
    row_security_source = (
        (row_security.__file__ and open(row_security.__file__, encoding="utf-8").read())
    )
    database_source = open(database.__file__, encoding="utf-8").read()

    assert "set_config('app.survey_user_id'" in row_security_source
    assert "clear_survey_user_context" in row_security_source
    assert "RESET app.survey_user_id" in database_source
    assert "attach_survey_context_cleanup(engine)" in database_source
