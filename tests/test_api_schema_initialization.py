from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_schema_initializer():
    spec = importlib.util.spec_from_file_location(
        "landcheck_schema_initializer",
        ROOT / "scripts" / "initialize_api_schema.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_legacy_schema_bootstrap_runs_once_in_deterministic_order(monkeypatch):
    initializer = _load_schema_initializer()
    calls: list[str] = []

    monkeypatch.setattr(initializer, "init_db", lambda: calls.append("legacy_tables"))
    monkeypatch.setattr(initializer, "ensure_survey_auth_schema", lambda: calls.append("survey_auth"))
    monkeypatch.setattr(initializer, "ensure_plots_schema_once", lambda _db: calls.append("plot_schema"))
    monkeypatch.setattr(initializer, "ensure_hazard_analysis_jobs_table", lambda _db: calls.append("hazard_schema"))
    monkeypatch.setattr(initializer, "ensure_georeference_schema", lambda _db: calls.append("georef_schema"))
    monkeypatch.setattr(initializer, "ensure_photo_evidence_schema", lambda _db: calls.append("photo_schema"))
    monkeypatch.setattr(initializer, "ensure_osm_overpass_schema", lambda _db: calls.append("osm_schema"))
    monkeypatch.setattr(initializer, "ensure_plan_reader_usage_schema", lambda _db: calls.append("plan_reader_schema"))
    monkeypatch.setattr(initializer, "ensure_survey_activity_table", lambda _db: calls.append("survey_activity_schema"))
    monkeypatch.setattr(initializer, "ensure_tree_health_schema", lambda _db: calls.append("tree_health_schema"))
    monkeypatch.setattr(initializer, "SessionLocal", lambda: _FakeSession(calls))
    monkeypatch.setattr(
        initializer,
        "ensure_activity_log_table",
        lambda: calls.append("activity_log"),
    )
    monkeypatch.setattr(
        initializer.green,
        "bootstrap_green_schema",
        lambda: calls.append("green_schema"),
    )

    initializer.initialize_schema()

    assert calls == [
        "legacy_tables",
        "survey_auth",
        "plot_schema",
        "hazard_schema",
        "georef_schema",
        "photo_schema",
        "osm_schema",
        "plan_reader_schema",
        "survey_activity_schema",
        "tree_health_schema",
        "activity_log",
        "green_schema",
    ]


class _FakeSession:
    def __init__(self, calls):
        self.calls = calls

    def close(self):
        pass


def test_one_shot_schema_service_runs_before_api_workers():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    migrate_block = compose.split("  migrate:", 1)[1].split("  survey-export-worker:", 1)[0]
    assert "python -m scripts.initialize_api_schema" in migrate_block
    assert "alembic upgrade head" in migrate_block
    assert "python -m scripts.verify_runtime_schema" in migrate_block
    assert "service_completed_successfully" in compose

    start_script = (ROOT / "scripts" / "start_api.sh").read_text(encoding="utf-8")
    assert "alembic upgrade head" not in start_script
    assert "python -m scripts.initialize_api_schema" not in start_script
    assert start_script.index("exec uvicorn") >= 0


def test_fastapi_startup_does_not_bootstrap_schema_per_worker():
    main_source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    startup_body = main_source.split("def startup_event():", 1)[1].split("\n\n", 1)[0]
    assert "init_db()" not in startup_body
    assert "ensure_activity_log_table()" not in startup_body
    assert "bootstrap_green_schema()" not in startup_body
