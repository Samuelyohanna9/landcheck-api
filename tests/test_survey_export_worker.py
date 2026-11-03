from __future__ import annotations

import inspect
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers import plots
from app.services import survey_export_worker as worker


@pytest.fixture
def local_tmp_dir():
    # The managed Windows runner can deny pytest's global %TEMP% directory. Keep these focused
    # file-publication tests inside the repository and remove the directory after each test.
    root = Path(__file__).resolve().parents[1] / ".survey-worker-test-tmp"
    root.mkdir(exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="case-", dir=root))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class _FakeResult:
    def __init__(self, *, rows=None, scalar_rows=None, mapping=None, scalar_value=None):
        self.rows = list(rows or [])
        self.scalar_rows = list(scalar_rows or [])
        self.mapping = mapping
        self.scalar_value = scalar_value
        self.rowcount = 1

    def scalars(self):
        return self

    def all(self):
        return self.rows

    def mappings(self):
        return self

    def first(self):
        return self.mapping

    def scalar(self):
        return self.scalar_value


class _ClaimSession:
    def __init__(self):
        self.calls = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, params or {}))
        if "ROW_NUMBER() OVER" in sql:
            return _FakeResult(rows=["job-1"])
        if "FOR UPDATE SKIP LOCKED" in sql:
            return _FakeResult(mapping={
                "id": "job-1",
                "export_type": "survey-plan.pdf",
                "owner_user_id": 42,
                "attempt_count": 0,
                "max_attempts": 3,
            })
        if "RETURNING id, export_type" in sql:
            return _FakeResult(mapping={
                "id": "job-1",
                "export_type": "survey-plan.pdf",
                "owner_user_id": 42,
                "attempt_count": 1,
                "max_attempts": 3,
            })
        return _FakeResult()

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _UpdateSession:
    def __init__(self):
        self.calls = []
        self.commits = 0

    def execute(self, statement, params=None):
        self.calls.append((str(statement), params or {}))
        return _FakeResult()

    def commit(self):
        self.commits += 1


def test_completion_update_is_fenced_to_lease_and_pending_cancellation():
    session = _UpdateSession()

    assert plots._set_plot_export_job_status(
        session,
        "job-1",
        status="completed",
        lease_token="token-1",
        progress=100,
    )

    sql, params = session.calls[0]
    assert "lease_token = :lease_token" in sql
    assert "cancel_requested_at IS NULL" in sql
    assert "lease_owner = NULL" in sql
    assert "lease_expires_at = NULL" in sql
    assert "heartbeat_at = NULL" in sql
    assert params["lease_token"] == "token-1"


def test_failure_update_also_clears_terminal_lease():
    session = _UpdateSession()

    assert plots._set_plot_export_job_status(
        session,
        "job-1",
        status="failed",
        lease_token="token-1",
        error_text="render failed",
        completed=True,
    )

    sql, params = session.calls[0]
    assert "lease_token = NULL" in sql
    assert "lease_owner = NULL" in sql
    assert "lease_expires_at = NULL" in sql
    assert "heartbeat_at = NULL" in sql
    assert params["lease_token"] == "token-1"


def test_single_plot_worker_archives_dxf_in_shared_storage():
    source = inspect.getsource(plots._run_single_plot_export_job)
    assert '"survey-plan.dxf"' in source


def test_queued_renderer_defaults_are_plain_values_not_fastapi_body_sentinels():
    values = plots._with_plot_export_defaults({}, {"title_text": "SURVEY PLAN", "station_names": []})
    assert values == {"title_text": "SURVEY PLAN", "station_names": []}
    overridden = plots._with_plot_export_defaults({"title_text": "Custom"}, values)
    assert overridden["title_text"] == "Custom"


def test_single_plot_export_cache_is_atomic_and_scoped(local_tmp_dir, monkeypatch):
    tmp_path = local_tmp_dir
    shared_dir = tmp_path / "shared"
    source_path = tmp_path / "worker-local.dxf"
    source_path.write_bytes(b"DXF CONTENT")
    monkeypatch.setattr(plots, "SURVEY_EXPORT_SHARED_DIR", str(shared_dir))

    cached_path = plots._cache_single_plot_export_file(
        plot_id=17,
        export_type="survey-plan.dxf",
        cache_key="cache-key-123456789",
        source_path=str(source_path),
        file_name="plot_17_survey_plan.dxf",
    )

    assert Path(cached_path).read_bytes() == b"DXF CONTENT"
    assert str(Path(cached_path).resolve()).startswith(str(shared_dir.resolve()))
    assert not list(shared_dir.rglob("*.tmp"))


def test_subdivision_renderer_uses_resolved_measurement_area(monkeypatch, local_tmp_dir):
    tmp_path = local_tmp_dir
    output_path = tmp_path / "subdivision.pdf"
    captured = {}

    monkeypatch.setattr(
        plots,
        "get_plot_meta",
        lambda _db, _plot_id: {
            "coordinate_system": "wgs84",
            "title_text": "Survey plan",
            "location_text": "Location",
            "lga_text": "LGA",
            "state_text": "State",
            "scale_text": "1 : 1000",
            "surveyor_name": "Surveyor",
            "surveyor_rank": "LS",
            "paper_size": "A4",
        },
    )
    monkeypatch.setattr(plots, "_station_names_from_survey_input_coordinates", lambda _value: [])
    monkeypatch.setattr(plots, "_load_plot_polygon_wgs84", lambda _db, _plot_id: object())
    monkeypatch.setattr(
        plots,
        "_resolve_measurement_polygon_context",
        lambda *_args: {
            "render_coordinate_system": "wgs84",
            "epsg_code": 4326,
            "crs_name": "WGS 84",
            "measurement_polygon": object(),
            "measurement_area_m2": 1234.5,
        },
    )
    monkeypatch.setattr(plots, "_resolve_unit_system_for_plot", lambda *_args: "metric")

    def render_map(**kwargs):
        Path(kwargs["output_path"]).write_bytes(b"map")

    monkeypatch.setattr(plots, "render_plot_map_layout", render_map)
    monkeypatch.setattr(
        plots,
        "_build_plot_report_from_area",
        lambda _plot_id, area, _db: captured.setdefault("area", area) or {"area": area},
    )
    monkeypatch.setattr(plots, "generate_plot_report_pdf", lambda report, path, map_path, **kwargs: Path(path).write_bytes(b"pdf"))

    class _DB:
        def commit(self):
            pass

    plots._render_survey_plan_pdf_for_plot(_DB(), 17, str(output_path))

    assert captured["area"] == 1234.5
    assert output_path.read_bytes() == b"pdf"


def test_cancelled_job_does_not_start_renderer(monkeypatch):
    job = worker.ClaimedJob("job-1", "survey-plan.pdf", "token-1", 1, 3, 42)
    cancelled = []
    monkeypatch.setattr(worker, "_is_cancel_requested", lambda _job: True)
    monkeypatch.setattr(worker, "_finish_cancelled", lambda _job: cancelled.append(_job.id))
    monkeypatch.setattr(plots, "_run_single_plot_export_job", lambda *_args, **_kwargs: pytest.fail("renderer started"))

    worker._run_claimed_job(job)

    assert cancelled == ["job-1"]


def test_claim_next_job_uses_skip_locked_and_a_lease_token(monkeypatch):
    session = _ClaimSession()
    monkeypatch.setattr(worker, "WORKER_ID", "test-worker")

    claimed = worker.claim_next_job(session)

    assert claimed is not None
    assert claimed.id == "job-1"
    assert claimed.owner_user_id == 42
    assert claimed.lease_token
    assert any("FOR UPDATE SKIP LOCKED" in sql for sql, _ in session.calls)
    update_sql, update_params = next((sql, params) for sql, params in session.calls if "RETURNING id, export_type" in sql)
    assert "lease_token = :lease_token" in update_sql
    assert update_params["lease_owner"] == "test-worker"
    assert session.commits == 1


def test_claim_next_job_rejects_unknown_export_type(monkeypatch):
    session = _ClaimSession()
    original_execute = session.execute

    def execute(statement, params=None):
        result = original_execute(statement, params)
        if "FOR UPDATE SKIP LOCKED" in str(statement):
            result.mapping["export_type"] = "not-supported"
        return result

    session.execute = execute
    with pytest.raises(RuntimeError, match="Unsupported Survey export type"):
        worker.claim_next_job(session)
    assert session.rollbacks == 1


def test_run_claimed_job_dispatches_to_existing_renderer(monkeypatch):
    job = worker.ClaimedJob(
        id="job-1",
        export_type="survey-plan.pdf",
        lease_token="token-1",
        attempt_count=1,
        max_attempts=3,
        owner_user_id=42,
    )
    calls = []
    monkeypatch.setattr(worker, "_is_cancel_requested", lambda _job: False)
    monkeypatch.setattr(worker, "_heartbeat", lambda *_args: None)
    monkeypatch.setattr(
        plots,
        "_run_single_plot_export_job",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    worker._run_claimed_job(job)

    assert calls == [(
        ("job-1",),
        {"lease_token": "token-1", "mark_failure": False},
    )]


def test_uuid_export_job_access_requires_matching_owner(monkeypatch):
    monkeypatch.setattr(
        plots,
        "require_survey_session",
        lambda _db, _request: SimpleNamespace(user_id=42),
    )
    job = {"id": "7f3c-job", "owner_user_id": 42}
    plots._require_plot_export_job_access(object(), object(), job)

    monkeypatch.setattr(
        plots,
        "require_survey_session",
        lambda _db, _request: SimpleNamespace(user_id=99),
    )
    with pytest.raises(HTTPException) as exc:
        plots._require_plot_export_job_access(object(), object(), job)
    assert exc.value.status_code == 404


def test_uuid_export_job_access_fails_closed_when_owner_is_missing(monkeypatch):
    monkeypatch.setattr(
        plots,
        "require_survey_session",
        lambda _db, _request: SimpleNamespace(user_id=42),
    )
    monkeypatch.setattr(plots, "_get_plot_export_owner_user_id", lambda _db, _job: None)

    with pytest.raises(HTTPException) as exc:
        plots._require_plot_export_job_access(object(), object(), {"id": "legacy-job", "owner_user_id": None})

    assert exc.value.status_code == 404


def test_export_submission_requires_authenticated_survey_session(monkeypatch):
    def reject(_db, _request):
        raise HTTPException(status_code=401, detail="Survey sign-in required")

    monkeypatch.setattr(plots, "require_survey_session", reject)
    with pytest.raises(HTTPException) as exc:
        plots._require_plot_export_submission_access(plot_id=17, request=object(), db=object())
    assert exc.value.status_code == 401


def test_export_submission_denies_a_plot_owned_by_another_user(monkeypatch):
    monkeypatch.setattr(
        plots,
        "require_survey_session",
        lambda _db, _request: SimpleNamespace(user_id=42),
    )
    monkeypatch.setattr(plots, "_get_plot_owner_user_id", lambda _db, _plot_id: 99)

    with pytest.raises(HTTPException) as exc:
        plots._require_plot_export_submission_access(plot_id=17, request=object(), db=object())
    assert exc.value.status_code == 404


def test_subdivision_export_requires_authenticated_owner_context(monkeypatch):
    monkeypatch.setattr(
        plots,
        "require_survey_session",
        lambda _db, _request: SimpleNamespace(user_id=42),
    )
    monkeypatch.setattr(plots, "_get_plot_owner_user_id", lambda _db, _plot_id: None)

    assert plots._require_subdivision_batch_export_access(object(), object(), 17) == 42


def test_all_survey_job_submission_routes_have_explicit_access_checks():
    route_functions = (
        plots.create_plot_survey_report_export_job,
        plots.create_plot_orthophoto_export_job,
        plots.create_plot_topomap_export_job,
        plots.create_plot_dxf_export_job,
        plots.create_plot_shapefile_export_job,
        plots.create_plot_back_computation_export_job,
        plots.create_plot_technical_report_export_job,
    )
    for route_function in route_functions:
        assert "_require_plot_export_submission_access(plot_id=plot_id, request=request, db=db)" in inspect.getsource(route_function)

    for route_function in (
        plots.create_subdivision_batch_export_job,
        plots.create_subdivision_batch_clean_copy_export_job,
        plots.export_subdivision_batch_survey_plans,
        plots.export_subdivision_batch_clean_copy_pdf,
    ):
        assert "_require_subdivision_batch_export_access" in inspect.getsource(route_function)


def test_legacy_final_file_routes_have_owner_authorization_dependencies():
    protected_routes = (
        plots.download_plot_report_pdf,
        plots.simple_download_pdf,
        plots.download_back_computation_pdf,
        plots.orthophoto_pdf,
        plots.download_survey_plan_dwg,
        plots.download_survey_plan_shapefile,
        plots.get_saved_survey_plan_pdf,
        plots.get_saved_orthophoto_pdf,
        plots.get_saved_back_computation_pdf,
    )
    for route_function in protected_routes:
        route = next(route for route in plots.router.routes if route.endpoint is route_function)
        assert any(
            getattr(dependency.call, "__name__", "") == "_require_plot_export_submission_access"
            for dependency in route.dependant.dependencies
        )


def test_export_routes_no_longer_start_in_process_daemon_jobs():
    assert "threading.Thread(target=_run_single_plot_export_job" not in inspect.getsource(plots._insert_single_plot_export_job)
    assert "threading.Thread(target=_run_subdivision_batch_export_job" not in inspect.getsource(plots.create_subdivision_batch_export_job)
    assert "threading.Thread(target=_run_subdivision_batch_clean_copy_export_job" not in inspect.getsource(plots.create_subdivision_batch_clean_copy_export_job)
