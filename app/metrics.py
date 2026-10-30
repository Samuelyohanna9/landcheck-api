"""Low-cardinality Prometheus metrics for the HTTP request lifecycle.

The application process writes metrics to the Prometheus client's multiprocess
directory.  The directory is configured by the container entrypoint before
Uvicorn imports this module.  Metrics are intentionally exported by the
separate ``app.metrics_exporter`` process rather than by FastAPI itself.
"""

from __future__ import annotations

import atexit
from collections.abc import MutableMapping
import os
from time import perf_counter
from typing import Any

from prometheus_client import Counter, Gauge, Histogram, REGISTRY
from prometheus_client import multiprocess
from prometheus_client.values import ValueClass


UNMATCHED_ROUTE = "__unmatched__"
_ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "CONNECT", "TRACE"})


def normalize_method(method: str | None) -> str:
    value = str(method or "").strip().upper()
    return value if value in _ALLOWED_METHODS else "OTHER"


def normalize_route(scope: MutableMapping[str, Any]) -> str:
    """Return only a Starlette route template, never a request URL.

    Router matching happens inside the application, so an outer ASGI middleware
    may not see ``scope['route']`` for unmatched requests.  Those requests use a
    fixed bounded label instead of falling back to the raw path.
    """

    route = scope.get("route")
    template = getattr(route, "path", None)
    if isinstance(template, str) and template.startswith("/") and len(template) <= 512:
        return template
    return UNMATCHED_ROUTE


def normalize_status(status_code: int) -> str:
    status = int(status_code or 500)
    return str(status) if 100 <= status <= 599 else "500"


class RequestMetrics:
    """Metric families kept in one object so tests can use an isolated registry."""

    def __init__(self, *, registry=REGISTRY) -> None:
        self.requests = Counter(
            "landcheck_http_requests_total",
            "Total HTTP requests handled by the LandCheck API.",
            ("method", "route", "status"),
            registry=registry,
        )
        self.duration = Histogram(
            "landcheck_http_request_duration_seconds",
            "HTTP request processing duration in seconds.",
            ("method", "route"),
            # Keep buckets useful for API SLOs without excessive time-series.
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
            registry=registry,
        )
        self.in_progress = Gauge(
            "landcheck_http_requests_in_progress",
            "HTTP requests currently executing in this API process set.",
            ("method",),
            # livesum prevents dead Uvicorn worker values being treated as live.
            multiprocess_mode="livesum",
            registry=registry,
        )

    def observe_request(self, *, method: str, route: str, status: int, duration_seconds: float) -> None:
        safe_method = normalize_method(method)
        safe_route = route if route.startswith("/") and len(route) <= 512 else UNMATCHED_ROUTE
        self.requests.labels(safe_method, safe_route, normalize_status(status)).inc()
        self.duration.labels(safe_method, safe_route).observe(max(float(duration_seconds), 0.0))


class PrometheusMiddleware:
    """Measure the whole downstream ASGI request lifecycle.

    This middleware re-raises unhandled exceptions after recording them as 500s;
    FastAPI/Starlette remains responsible for producing the actual error response.
    """

    def __init__(self, app, *, metrics: RequestMetrics | None = None) -> None:
        self.app = app
        self.metrics = metrics or http_metrics

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = normalize_method(scope.get("method"))
        started_at = perf_counter()
        recorded = False
        response_status = 500
        self.metrics.in_progress.labels(method).inc()

        async def send_wrapper(message):
            nonlocal response_status
            if message.get("type") == "http.response.start":
                response_status = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # Record before re-raising so ServerErrorMiddleware can still return
            # its normal 500 response and the application's error behavior stays intact.
            self.metrics.observe_request(
                method=method,
                route=normalize_route(scope),
                status=500,
                duration_seconds=perf_counter() - started_at,
            )
            recorded = True
            raise
        finally:
            if not recorded:
                self.metrics.observe_request(
                    method=method,
                    route=normalize_route(scope),
                    status=response_status,
                    duration_seconds=perf_counter() - started_at,
                )
            self.metrics.in_progress.labels(method).dec()


def cleanup_worker_metrics() -> bool:
    """Remove only this process's live-gauge files during normal shutdown.

    ``mark_process_dead`` intentionally affects only ``gauge_live*`` files; it
    leaves counters and histograms intact.  It cannot run after SIGKILL or a
    hard crash, so the Uvicorn parent would still need an exit hook for those
    cases (the current Uvicorn CLI does not expose one).
    """

    metrics_dir = str(os.getenv("PROMETHEUS_MULTIPROC_DIR") or "").strip()
    if not metrics_dir or not os.path.isdir(metrics_dir):
        return False
    try:
        close_all_files = getattr(ValueClass, "close_all_files", None)
        if callable(close_all_files):
            close_all_files()
        multiprocess.mark_process_dead(os.getpid(), path=metrics_dir)
    except (FileNotFoundError, OSError):
        # Shutdown must never change application exit behavior if a metric file
        # disappears during cleanup.
        return False
    return True


# If a worker PID is reused, discard only the old live-gauge file for that PID
# before this process creates its own metric values. Counters and histograms are
# deliberately preserved. The atexit hook covers normal worker termination;
# hard crashes still require supervisor-level cleanup.
cleanup_worker_metrics()
atexit.register(cleanup_worker_metrics)


http_metrics = RequestMetrics()
