"""Private Prometheus exporter for the multiprocess API metrics directory."""

from __future__ import annotations

import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from prometheus_client.multiprocess import MultiProcessCollector


def exporter_path_allowed(path: str) -> bool:
    return path == "/metrics" or path == "/healthz"


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path == "/healthz":
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path != "/metrics":
            self.send_error(404)
            return

        registry = CollectorRegistry(auto_describe=True)
        MultiProcessCollector(registry)
        body = generate_latest(registry)
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPE_LATEST)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args) -> None:
        # Prometheus scrapes contain no customer request URLs; avoid noisy access logs.
        return


def main() -> None:
    multiproc_dir = str(os.getenv("PROMETHEUS_MULTIPROC_DIR") or "").strip()
    if not multiproc_dir:
        raise RuntimeError("PROMETHEUS_MULTIPROC_DIR must be set for the metrics exporter")
    if not os.path.isdir(multiproc_dir):
        raise RuntimeError(f"PROMETHEUS_MULTIPROC_DIR does not exist: {multiproc_dir}")

    host = str(os.getenv("METRICS_EXPORTER_HOST") or "0.0.0.0").strip()
    port = int(str(os.getenv("METRICS_EXPORTER_PORT") or "9101").strip())
    server = ThreadingHTTPServer((host, port), MetricsHandler)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
