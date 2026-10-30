from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import asyncio
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.multiprocess import MultiProcessCollector

from app.metrics import (
    UNMATCHED_ROUTE,
    PrometheusMiddleware,
    RequestMetrics,
    cleanup_worker_metrics,
    normalize_method,
    normalize_route,
)
from app.metrics_exporter import MetricsHandler, exporter_path_allowed


def test_route_and_method_normalization_is_bounded() -> None:
    assert normalize_method("get") == "GET"
    assert normalize_method("made-up-method") == "OTHER"
    assert normalize_route({"route": SimpleNamespace(path="/estates/{estate_id}/plots/{plot_id}")}) == "/estates/{estate_id}/plots/{plot_id}"
    assert normalize_route({"path": "/private/customer/123"}) == UNMATCHED_ROUTE


async def _run_asgi(app, path: str = "/ok") -> list[dict]:
    messages: list[dict] = []
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "query_string": b"secret=not-a-label",
        "route": SimpleNamespace(path="/estates/{estate_id}/summary"),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    return messages


def test_middleware_records_success_and_http_errors_without_raw_urls() -> None:
    registry = CollectorRegistry()
    metrics = RequestMetrics(registry=registry)

    async def downstream(scope, receive, send):
        status = 404 if scope["path"] == "/missing" else 200
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    middleware = PrometheusMiddleware(downstream, metrics=metrics)
    asyncio.run(_run_asgi(middleware))
    asyncio.run(_run_asgi(middleware, "/missing"))

    rendered = generate_latest(registry).decode()
    assert 'route="/estates/{estate_id}/summary"' in rendered
    assert 'status="200"' in rendered
    assert 'status="404"' in rendered
    assert "secret=not-a-label" not in rendered
    assert metrics.in_progress.labels("GET")._value.get() == 0


def test_unhandled_exception_is_recorded_and_re_raised() -> None:
    registry = CollectorRegistry()
    metrics = RequestMetrics(registry=registry)

    async def downstream(scope, receive, send):
        raise RuntimeError("deliberate test failure")

    middleware = PrometheusMiddleware(downstream, metrics=metrics)
    with pytest.raises(RuntimeError):
        asyncio.run(_run_asgi(middleware))

    rendered = generate_latest(registry).decode()
    assert 'status="500"' in rendered


def test_in_progress_gauge_is_nonzero_while_request_is_running() -> None:
    registry = CollectorRegistry()
    metrics = RequestMetrics(registry=registry)
    observed: list[float] = []

    async def downstream(scope, receive, send):
        observed.append(metrics.in_progress.labels("GET")._value.get())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    asyncio.run(_run_asgi(PrometheusMiddleware(downstream, metrics=metrics)))
    assert observed == [1.0]
    assert metrics.in_progress.labels("GET")._value.get() == 0


def test_multiprocess_workers_aggregate_into_one_scrape() -> None:
    worker_code = (
        "from app.metrics import http_metrics; "
        "http_metrics.observe_request(method='GET', route='/health/', status=200, duration_seconds=0.025)"
    )
    with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1]) as metric_dir:
        env = os.environ.copy()
        env["PROMETHEUS_MULTIPROC_DIR"] = metric_dir
        for _ in range(2):
            completed = subprocess.run([sys.executable, "-c", worker_code], env=env, cwd=Path(__file__).parents[1], capture_output=True, text=True)
            assert completed.returncode == 0, completed.stderr

        previous_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
        os.environ["PROMETHEUS_MULTIPROC_DIR"] = metric_dir
        try:
            registry = CollectorRegistry()
            MultiProcessCollector(registry)
            rendered = generate_latest(registry).decode()
            assert 'landcheck_http_requests_total{method="GET",route="/health/",status="200"} 2.0' in rendered
        finally:
            if previous_dir is None:
                os.environ.pop("PROMETHEUS_MULTIPROC_DIR", None)
            else:
                os.environ["PROMETHEUS_MULTIPROC_DIR"] = previous_dir


def test_normal_worker_cleanup_removes_live_gauge_but_preserves_counters() -> None:
    worker_code = (
        "from app.metrics import http_metrics, cleanup_worker_metrics; "
        "http_metrics.requests.labels('GET', '/health/', '200').inc(); "
        "http_metrics.in_progress.labels('GET').inc(); "
        "cleanup_worker_metrics(); cleanup_worker_metrics()"
    )
    with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1]) as metric_dir:
        env = os.environ.copy()
        env["PROMETHEUS_MULTIPROC_DIR"] = metric_dir
        completed = subprocess.run([sys.executable, "-c", worker_code], env=env, cwd=Path(__file__).parents[1], capture_output=True, text=True)
        assert completed.returncode == 0, completed.stderr

        previous_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
        os.environ["PROMETHEUS_MULTIPROC_DIR"] = metric_dir
        try:
            registry = CollectorRegistry()
            MultiProcessCollector(registry)
            rendered = generate_latest(registry).decode()
            assert 'landcheck_http_requests_total{method="GET",route="/health/",status="200"} 1.0' in rendered
            assert "landcheck_http_requests_in_progress" not in rendered
        finally:
            if previous_dir is None:
                os.environ.pop("PROMETHEUS_MULTIPROC_DIR", None)
            else:
                os.environ["PROMETHEUS_MULTIPROC_DIR"] = previous_dir


def test_cleanup_function_is_safe_when_metrics_directory_is_missing(monkeypatch) -> None:
    missing_dir = Path(__file__).parents[1] / f".missing-prometheus-dir-{os.getpid()}"
    assert not missing_dir.exists()
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(missing_dir))
    assert cleanup_worker_metrics() is False


def test_cleanup_is_idempotent_and_targets_only_current_worker(monkeypatch) -> None:
    with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1]) as metric_dir:
        monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", metric_dir)
        calls: list[tuple[int, str]] = []

        def fake_mark_process_dead(pid: int, *, path: str) -> None:
            calls.append((pid, path))

        from app import metrics as metrics_module

        monkeypatch.setattr(metrics_module.multiprocess, "mark_process_dead", fake_mark_process_dead)
        assert cleanup_worker_metrics() is True
        assert cleanup_worker_metrics() is True
        assert calls == [(os.getpid(), metric_dir), (os.getpid(), metric_dir)]


def test_metrics_exporter_only_allows_private_scrape_paths() -> None:
    assert exporter_path_allowed("/metrics")
    assert exporter_path_allowed("/healthz")
    assert not exporter_path_allowed("/")
    assert not exporter_path_allowed("/metrics?customer=1")


def test_private_exporter_serves_metrics_and_health_only() -> None:
    with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1]) as metric_dir:
        previous_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
        os.environ["PROMETHEUS_MULTIPROC_DIR"] = metric_dir
        server = ThreadingHTTPServer(("127.0.0.1", 0), MetricsHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            assert urllib.request.urlopen(f"{base_url}/healthz", timeout=3).status == 200
            assert urllib.request.urlopen(f"{base_url}/metrics", timeout=3).status == 200
            try:
                urllib.request.urlopen(f"{base_url}/", timeout=3)
            except urllib.error.HTTPError as error:
                assert error.code == 404
            else:
                raise AssertionError("exporter root must not be exposed")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            if previous_dir is None:
                os.environ.pop("PROMETHEUS_MULTIPROC_DIR", None)
            else:
                os.environ["PROMETHEUS_MULTIPROC_DIR"] = previous_dir


def test_fastapi_listener_has_no_metrics_route() -> None:
    from app.main import app

    assert not any(getattr(route, "path", None) == "/metrics" for route in app.routes)
    middleware_names = [middleware.cls.__name__ for middleware in app.user_middleware]
    assert middleware_names.index("PrometheusMiddleware") < middleware_names.index("CORSMiddleware")


def test_compose_does_not_publish_exporter_port_and_uses_external_network() -> None:
    compose = (Path(__file__).parents[1] / "docker-compose.yml").read_text(encoding="utf-8")
    exporter_section = compose.split("  metrics-exporter:", 1)[1].split("\n\nvolumes:", 1)[0]
    assert "9101:9101" not in exporter_section
    assert "landcheck_monitoring" in exporter_section
    assert "condition: service_healthy" in exporter_section
    assert "landcheck-monitoring_monitoring" in compose
    assert "external: true" in compose
