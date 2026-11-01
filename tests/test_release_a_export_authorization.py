import inspect
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

# Keep source-only checks importable in a clean checkout without a developer .env file. The
# integration test below is explicitly enabled with RELEASE_A_HTTP_TEST_DATABASE_URL.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg2://release_a_test:release_a_test_pw@127.0.0.1:1/landcheck_release_a",
)

from app.routers import plots


def test_clean_release_reads_only_existing_export_job_columns():
    source = inspect.getsource(plots._get_plot_export_job)
    assert "owner_user_id" not in source
    assert "attempt_count" not in source
    assert "lease_token" not in source


def test_status_and_download_require_source_owner(monkeypatch):
    monkeypatch.setattr(
        plots,
        "require_survey_session",
        lambda _db, _request: SimpleNamespace(user_id=42),
    )
    monkeypatch.setattr(plots, "_get_plot_export_owner_user_id", lambda _db, _job: 42)
    plots._require_plot_export_job_access(object(), object(), {"id": "job-1"})

    monkeypatch.setattr(plots, "_get_plot_export_owner_user_id", lambda _db, _job: 99)
    with pytest.raises(HTTPException) as exc:
        plots._require_plot_export_job_access(object(), object(), {"id": "job-1"})
    assert exc.value.status_code == 404


def test_ownerless_legacy_job_fails_closed(monkeypatch):
    monkeypatch.setattr(
        plots,
        "require_survey_session",
        lambda _db, _request: SimpleNamespace(user_id=42),
    )
    monkeypatch.setattr(plots, "_get_plot_export_owner_user_id", lambda _db, _job: None)
    with pytest.raises(HTTPException) as exc:
        plots._require_plot_export_job_access(object(), object(), {"id": "legacy-job"})
    assert exc.value.status_code == 404


def test_submission_requires_authentication_and_exact_plot_owner(monkeypatch):
    def reject(_db, _request):
        raise HTTPException(status_code=401, detail="Survey sign-in required")

    monkeypatch.setattr(plots, "require_survey_session", reject)
    with pytest.raises(HTTPException) as exc:
        plots._require_plot_export_submission_access(plot_id=7, request=object(), db=object())
    assert exc.value.status_code == 401

    monkeypatch.setattr(
        plots,
        "require_survey_session",
        lambda _db, _request: SimpleNamespace(user_id=42),
    )
    monkeypatch.setattr(plots, "_get_plot_owner_user_id", lambda _db, _plot_id: 99)
    with pytest.raises(HTTPException) as exc:
        plots._require_plot_export_submission_access(plot_id=7, request=object(), db=object())
    assert exc.value.status_code == 404


def test_final_file_routes_use_owner_dependency():
    protected = (
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
    for endpoint in protected:
        route = next(route for route in plots.router.routes if route.endpoint is endpoint)
        assert any(
            getattr(dependency.call, "__name__", "") == "_require_plot_export_submission_access"
            for dependency in route.dependant.dependencies
        )


def test_job_submission_routes_have_explicit_access_checks():
    plot_routes = (
        plots.create_plot_survey_report_export_job,
        plots.create_plot_orthophoto_export_job,
        plots.create_plot_topomap_export_job,
        plots.create_plot_dxf_export_job,
        plots.create_plot_shapefile_export_job,
        plots.create_plot_back_computation_export_job,
        plots.create_plot_technical_report_export_job,
    )
    for endpoint in plot_routes:
        assert "_require_plot_export_submission_access(plot_id=plot_id" in inspect.getsource(endpoint)

    for endpoint in (
        plots.create_subdivision_batch_export_job,
        plots.create_subdivision_batch_clean_copy_export_job,
        plots.export_subdivision_batch_survey_plans,
        plots.export_subdivision_batch_clean_copy_pdf,
    ):
        assert "_require_subdivision_batch_export_access" in inspect.getsource(endpoint)


@pytest.mark.skipif(
    not os.getenv("RELEASE_A_HTTP_TEST_DATABASE_URL"),
    reason="set RELEASE_A_HTTP_TEST_DATABASE_URL for isolated HTTP staging validation",
)
def test_real_http_status_and_download_authorization_against_staging():
    pytest.importorskip("httpx2", reason="Starlette TestClient requires httpx2 in this environment")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.db import SessionLocal
    from app.utils.survey_auth_security import find_or_create_survey_user, issue_survey_session

    api = FastAPI()
    api.include_router(plots.router)

    db = SessionLocal()
    user_a = find_or_create_survey_user(db, email="release-a-user-a@example.invalid", full_name="User A")
    user_b = find_or_create_survey_user(db, email="release-a-user-b@example.invalid", full_name="User B")
    token_a = issue_survey_session(db, user_id=user_a)["access_token"]
    token_b = issue_survey_session(db, user_id=user_b)["access_token"]
    db.close()

    with TestClient(api) as client:
        owner_status = client.get("/plots/export-jobs/job-a", headers={"Authorization": f"Bearer {token_a}"})
        other_status = client.get("/plots/export-jobs/job-a", headers={"Authorization": f"Bearer {token_b}"})
        anonymous_status = client.get("/plots/export-jobs/job-a")
        ownerless_status = client.get("/plots/export-jobs/job-null", headers={"Authorization": f"Bearer {token_a}"})
        subdivision_status = client.get("/plots/export-jobs/job-batch", headers={"Authorization": f"Bearer {token_a}"})
        owner_download = client.get("/plots/export-jobs/job-a/download", headers={"Authorization": f"Bearer {token_a}"})
        other_download = client.get("/plots/export-jobs/job-a/download", headers={"Authorization": f"Bearer {token_b}"})
        missing_status = client.get("/plots/export-jobs/does-not-exist", headers={"Authorization": f"Bearer {token_a}"})

    assert owner_status.status_code == 200
    assert other_status.status_code == 404
    assert anonymous_status.status_code == 401
    assert ownerless_status.status_code == 404
    assert subdivision_status.status_code == 200
    assert owner_download.status_code == 200
    assert other_download.status_code == 404
    assert missing_status.status_code == 404
