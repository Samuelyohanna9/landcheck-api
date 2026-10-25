from __future__ import annotations

import pytest

from app import main
from app.routers import plots


class _Result:
    def __init__(self, value=True):
        self.value = value

    def scalar(self):
        return self.value


class _FakeSession:
    def __init__(self, lock_results=(True,)):
        self.lock_results = list(lock_results)
        self.executed = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def execute(self, statement, params=None):
        sql = str(statement)
        self.executed.append((sql, params or {}))
        if "pg_try_advisory_lock" in sql:
            return _Result(self.lock_results.pop(0) if self.lock_results else True)
        if "pg_advisory_unlock" in sql:
            return _Result(True)
        return _Result(True)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def test_scheduler_job_runs_once_and_releases_lock(monkeypatch):
    session = _FakeSession(lock_results=(True,))
    monkeypatch.setattr(main, "SessionLocal", lambda: session)
    ran = []

    main._run_scheduler_job_once(991, lambda db: ran.append(db))

    assert ran == [session]
    assert session.commits == 2  # work transaction plus advisory-unlock transaction
    assert session.rollbacks == 0
    assert session.closed
    assert any("pg_advisory_unlock" in sql for sql, _ in session.executed)


def test_scheduler_job_rolls_back_and_releases_lock_after_failure(monkeypatch):
    session = _FakeSession(lock_results=(True,))
    monkeypatch.setattr(main, "SessionLocal", lambda: session)

    with pytest.raises(RuntimeError, match="boom"):
        main._run_scheduler_job_once(992, lambda _db: (_ for _ in ()).throw(RuntimeError("boom")))

    assert session.rollbacks >= 1
    assert session.closed
    assert any("pg_advisory_unlock" in sql for sql, _ in session.executed)


def test_scheduler_job_skips_when_another_worker_holds_lock(monkeypatch):
    session = _FakeSession(lock_results=(False,))
    monkeypatch.setattr(main, "SessionLocal", lambda: session)
    ran = []

    main._run_scheduler_job_once(993, lambda _db: ran.append(True))

    assert ran == []
    assert session.closed
    assert not any("pg_advisory_unlock" in sql for sql, _ in session.executed)


def test_export_slot_waits_without_holding_a_waiting_session(monkeypatch):
    waiting = _FakeSession(lock_results=(False,))
    acquired = _FakeSession(lock_results=(True,))
    sessions = iter((waiting, acquired))
    sleeps = []
    monkeypatch.setattr(plots, "SessionLocal", lambda: next(sessions))
    monkeypatch.setattr(plots.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setenv("PLOT_EXPORT_RENDER_CONCURRENCY", "1")

    db, lock_key = plots._acquire_plot_export_slot()

    assert db is acquired
    assert lock_key == plots._PLOT_EXPORT_SLOT_LOCK_BASE
    assert waiting.closed
    assert sleeps == [0.25]

    plots._release_plot_export_slot(db, lock_key)
    assert acquired.closed is False
    assert any("pg_advisory_unlock" in sql for sql, _ in acquired.executed)


def test_back_computation_job_is_exposed_as_a_plot_export_type():
    assert "back-computation.pdf" in plots._run_single_plot_export_job.__wrapped__.__code__.co_consts
    assert plots.create_plot_back_computation_export_job.__name__ == "create_plot_back_computation_export_job"
