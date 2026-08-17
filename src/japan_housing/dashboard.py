"""Loopback-only dashboard and narrow decision API."""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .config import AppConfig
from .report import build_report_payload, load_latest
from .state import StateStore

MAX_DECISION_BODY = 8 * 1024
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


def host_header_is_loopback(value: str) -> bool:
    try:
        parsed = urlparse(f"//{value}")
        _ = parsed.port
    except ValueError:
        return False
    return (
        not parsed.username
        and not parsed.password
        and (parsed.hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}
    )


def _handler(config: AppConfig, web_root: Path) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "JapanHousingDashboard/0.1"

        def log_message(self, format: str, *args: object) -> None:
            # Keep the normal concise access log; request bodies and secrets are never logged.
            super().log_message(format, *args)

        def _headers(self, status: int, content_type: str, length: int) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header(
                "Cache-Control", "no-store" if self.path.startswith("/api/") else "no-cache"
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            )
            self.end_headers()

        def _json(self, status: int, payload: object) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode()
            self._headers(status, "application/json; charset=utf-8", len(body))
            self.wfile.write(body)

        def _valid_local_request(self) -> bool:
            return host_header_is_loopback(self.headers.get("Host", ""))

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if not self._valid_local_request():
                self._json(HTTPStatus.FORBIDDEN, {"error": "loopback Host header required"})
                return
            if path == "/api/report":
                try:
                    latest, _ = load_latest(config.runtime.artifacts_path)
                    with StateStore(config.runtime.state_path) as store:
                        payload = build_report_payload(
                            store,
                            str(latest["run_id"]),
                            [],
                            dict(latest.get("performance") or {}),
                            config,
                        )
                    payload["generated_at"] = latest.get("generated_at")
                    payload["performance"] = latest.get("performance") or {}
                    self._json(HTTPStatus.OK, payload)
                except (OSError, ValueError, RuntimeError) as exc:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
                return
            asset = ASSETS.get(path)
            if not asset:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            file_name, content_type = asset
            try:
                body = (web_root / file_name).read_bytes()
            except OSError:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "dashboard asset missing"})
                return
            self._headers(HTTPStatus.OK, content_type, len(body))
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            if urlparse(self.path).path != "/api/decisions":
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            if not self._valid_local_request() or self.headers.get("X-Japan-Housing") != "1":
                self._json(HTTPStatus.FORBIDDEN, {"error": "local dashboard request required"})
                return
            if self.headers.get_content_type() != "application/json":
                self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "JSON required"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_DECISION_BODY:
                self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "invalid request size"})
                return
            try:
                body = json.loads(self.rfile.read(length))
                stable_key = str(body["stable_key"])
                status = str(body["status"])
                note = str(body.get("note") or "")
                with StateStore(config.runtime.state_path) as store:
                    store.set_decision(stable_key, status, note)
            except (KeyError, TypeError, ValueError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            self._json(HTTPStatus.OK, {"ok": True, "stable_key": stable_key, "status": status})

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method not allowed"})

    return DashboardHandler


def serve_dashboard(config: AppConfig, web_root: Path) -> None:
    if config.runtime.dashboard_host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("dashboard must bind to loopback")
    server = ThreadingHTTPServer(
        (config.runtime.dashboard_host, config.runtime.dashboard_port),
        _handler(config, web_root),
    )
    server.daemon_threads = True
    try:
        print(
            f"Japan Housing dashboard: http://{config.runtime.dashboard_host}:{config.runtime.dashboard_port}"
        )
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
