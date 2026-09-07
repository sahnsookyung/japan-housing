"""Loopback-only dashboard and narrow decision API."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import AppConfig
from .decision_service import DecisionService
from .evidence import EDITABLE, EvidenceStore, validate_fields
from .models import ACTIONABLE_CLASSIFICATIONS
from .pareto import BALANCED_PERSPECTIVE_ID, is_pareto_candidate, pareto_frontier_keys
from .report import load_latest
from .state import StateStore

MAX_DECISION_BODY = 8 * 1024
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 200
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/housing-pareto.mjs": ("housing-pareto.mjs", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/decision-app.mjs": ("decision-app.mjs", "text/javascript; charset=utf-8"),
    "/decision-map.mjs": ("decision-map.mjs", "text/javascript; charset=utf-8"),
    "/pin-history.mjs": ("pin-history.mjs", "text/javascript; charset=utf-8"),
    "/view-state.mjs": ("view-state.mjs", "text/javascript; charset=utf-8"),
    "/room-evidence.mjs": ("room-evidence.mjs", "text/javascript; charset=utf-8"),
    "/decision-charts.mjs": ("decision-charts.mjs", "text/javascript; charset=utf-8"),
    "/decision.css": ("decision.css", "text/css; charset=utf-8"),
    "/vendor/leaflet.js": ("vendor/leaflet.js", "text/javascript; charset=utf-8"),
    "/vendor/leaflet.css": ("vendor/leaflet.css", "text/css; charset=utf-8"),
}

DASHBOARD_RECORD_FIELDS = frozenset(
    {
        "available_from",
        "available_text",
        "bicycle_minutes_mean",
        "bicycle_minutes_scored",
        "bicycle_route_coverage",
        "cautions",
        "city",
        "classification",
        "cluster_key",
        "commute_anchor_count",
        "commute_routes",
        "decision_note",
        "decision_status",
        "decision_updated_at",
        "distance_to_anchor_km",
        "freshness",
        "housing_type",
        "initial_cost_yen",
        "landing_90",
        "latitude",
        "long_term",
        "longitude",
        "name",
        "nearest_stations",
        "pareto_distances_km",
        "pareto_location_scores",
        "prefecture",
        "price_validation",
        "priority_cheaper_score",
        "priority_closer_score",
        "priority_score",
        "property_name",
        "rank_key",
        "ranking_price_basis",
        "ranking_price_yen",
        "recurring_total_yen",
        "registration_confidence",
        "source_id",
        "source_label",
        "stable_key",
        "title",
        "track",
        "transit_minutes_mean",
        "transit_minutes_scored",
        "transit_route_coverage",
        "url",
        "advertised_monthly_from_yen",
    }
)

PARETO_RECORD_FIELDS = frozenset(
    {
        "city",
        "classification",
        "decision_status",
        "freshness",
        "pareto_location_scores",
        "prefecture",
        "ranking_price_yen",
        "stable_key",
        "title",
        "track",
        "url",
    }
)


class _SnapshotCache:
    """Thread-safe cache of the immutable, atomically published report."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.RLock()
        self._pointer_mtime_ns = -1
        self._payload: dict[str, Any] | None = None
        self._records_by_key: dict[str, dict[str, Any]] = {}

    def load(self) -> dict[str, Any]:
        pointer = self.root / "latest.json"
        modified = pointer.stat().st_mtime_ns
        with self._lock:
            self._load_locked(modified)
            return self._payload

    def _load_locked(self, pointer_mtime_ns: int | None = None) -> None:
        if pointer_mtime_ns is None:
            pointer_mtime_ns = (self.root / "latest.json").stat().st_mtime_ns
        if self._payload is not None and pointer_mtime_ns == self._pointer_mtime_ns:
            return
        payload, _ = load_latest(self.root)
        self._payload = payload
        self._records_by_key = {
            str(item.get("stable_key")): item
            for item in payload.get("housing") or ()
            if isinstance(item, dict) and item.get("stable_key")
        }
        self._pointer_mtime_ns = pointer_mtime_ns

    def summary(self) -> dict[str, Any]:
        with self._lock:
            self._load_locked()
            assert self._payload is not None
            return {key: value for key, value in self._payload.items() if key != "housing"}

    def listing_page(self, parameters: dict[str, list[str]], config: AppConfig) -> dict[str, Any]:
        with self._lock:
            self._load_locked()
            assert self._payload is not None
            return dashboard_listing_page(self._payload, parameters, config)

    def update_decision(self, stable_key: str, status: str, note: str) -> None:
        with self._lock:
            item = self._records_by_key.get(stable_key)
            if item is not None:
                item["decision_status"] = status
                item["decision_note"] = note


def _project_record(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key in DASHBOARD_RECORD_FIELDS}


def _project_pareto_record(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key in PARETO_RECORD_FIELDS}


def _parameter(parameters: dict[str, list[str]], name: str, default: str) -> str:
    values = parameters.get(name)
    return str(values[0]) if values else default


def _integer_parameter(
    parameters: dict[str, list[str]],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(_parameter(parameters, name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _listing_text(item: dict[str, Any]) -> str:
    return " ".join(
        str(value)
        for value in (
            item.get("title"),
            item.get("property_name"),
            item.get("prefecture"),
            item.get("city"),
            *(item.get("nearest_stations") or ()),
        )
        if value
    ).casefold()


def _ranking_key(item: dict[str, Any], view: str, perspective_id: str) -> tuple[Any, ...]:
    if view == "pareto":
        scores = item.get("pareto_location_scores") or {}
        location = scores.get(perspective_id) if isinstance(scores, dict) else None
        return (
            float(location) if isinstance(location, int | float) else float("inf"),
            int(item.get("ranking_price_yen") or 10**12),
            str(item.get("stable_key") or ""),
        )
    rank = list(item.get("rank_key") or (999, 999, 999, 10**9, 9999, 999, ""))
    while len(rank) < 7:
        rank.append(999)
    if view == "closer":
        rank[3] = item.get("priority_closer_score", 10**9)
    elif view == "cheaper":
        rank[3] = item.get("priority_cheaper_score", 10**9)
    return tuple(rank)


def dashboard_listing_page(
    payload: dict[str, Any],
    parameters: dict[str, list[str]],
    config: AppConfig,
) -> dict[str, Any]:
    """Filter, recompute Pareto membership, and paginate one dashboard request."""

    classification = _parameter(parameters, "classification", "actionable")
    if classification not in {
        "actionable",
        "eligible",
        "review",
        "unverified",
        "excluded",
        "stale",
        "all",
    }:
        raise ValueError("classification is unsupported")
    track = _parameter(parameters, "track", "all")
    if track not in {"all", "landing", "long_term", "both"}:
        raise ValueError("track is unsupported")
    decision = _parameter(parameters, "decision", "all")
    allowed_decisions = {
        "all",
        "new",
        "shortlisted",
        "contacted",
        "application_started",
        "reserved",
        "not_interested",
    }
    if decision not in allowed_decisions:
        raise ValueError("decision is unsupported")
    view = _parameter(parameters, "ranking", "balanced")
    if view not in {"balanced", "closer", "cheaper", "pareto"}:
        raise ValueError("ranking is unsupported")
    perspective = _parameter(parameters, "perspective", BALANCED_PERSPECTIVE_ID)
    perspectives = {
        BALANCED_PERSPECTIVE_ID,
        *(anchor.anchor_id for anchor in config.routing.anchors),
    }
    if perspective not in perspectives:
        raise ValueError("perspective is unsupported")
    query = _parameter(parameters, "query", "").strip().casefold()
    if len(query) > 200:
        raise ValueError("query is too long")
    max_cost = _integer_parameter(parameters, "max_cost", 0, minimum=0, maximum=10_000_000)
    offset = _integer_parameter(parameters, "offset", 0, minimum=0, maximum=1_000_000)
    limit = _integer_parameter(
        parameters,
        "limit",
        DEFAULT_PAGE_SIZE,
        minimum=1,
        maximum=MAX_PAGE_SIZE,
    )

    records: list[dict[str, Any]] = []
    for item in payload.get("housing") or ():
        if not isinstance(item, dict):
            continue
        item_classification = str(item.get("classification") or "")
        freshness = str(item.get("freshness") or "current")
        if classification == "stale":
            if freshness != "stale":
                continue
        elif classification != "all":
            if freshness != "current":
                continue
            if (
                classification == "actionable"
                and item_classification not in ACTIONABLE_CLASSIFICATIONS
            ):
                continue
            if classification != "actionable" and item_classification != classification:
                continue
        if track != "all" and item.get("track") != track:
            continue
        if decision != "all" and item.get("decision_status") != decision:
            continue
        recurring = item.get("recurring_total_yen")
        if max_cost and (not isinstance(recurring, int) or recurring > max_cost):
            continue
        if query and query not in _listing_text(item):
            continue
        records.append(item)

    frontier, _candidate_count, _cohorts = pareto_frontier_keys(
        records,
        config.ranking,
        perspective,
    )
    pareto_records = [
        item for item in records if is_pareto_candidate(item, config.ranking, perspective)
    ]
    displayed = (
        [item for item in records if str(item.get("stable_key")) in frontier]
        if view == "pareto"
        else list(records)
    )
    displayed.sort(key=lambda item: _ranking_key(item, view, perspective))
    page = displayed[offset : offset + limit]
    return {
        "records": [_project_record(item) for item in page],
        "pareto_records": [_project_pareto_record(item) for item in pareto_records],
        "frontier_ids": sorted(frontier),
        "total": len(displayed),
        "filtered_total": len(records),
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(page) < len(displayed),
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
    snapshots = _SnapshotCache(config.runtime.artifacts_path)
    decisions = DecisionService(config, snapshots)

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "JapanHousingDashboard/0.1"

        def log_message(self, format: str, *args: object) -> None:
            # Keep the normal concise access log; request bodies and secrets are never logged.
            super().log_message(format, *args)

        def _headers(
            self,
            status: int,
            content_type: str,
            length: int,
            *,
            etag: str = "",
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
            if etag:
                self.send_header("ETag", etag)
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data: https://tile.openstreetmap.org; "
                "style-src 'self' 'unsafe-inline'; script-src 'self'; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            )
            self.end_headers()

        def _json(self, status: int, payload: object) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            etag = f'"{hashlib.sha256(body).hexdigest()[:24]}"'
            if self.command == "GET" and self.headers.get("If-None-Match") == etag:
                self._headers(
                    HTTPStatus.NOT_MODIFIED,
                    "application/json; charset=utf-8",
                    0,
                    etag=etag,
                )
                return
            self._headers(status, "application/json; charset=utf-8", len(body), etag=etag)
            self.wfile.write(body)

        def _valid_local_request(self) -> bool:
            return host_header_is_loopback(self.headers.get("Host", ""))

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if not self._valid_local_request():
                self._json(HTTPStatus.FORBIDDEN, {"error": "loopback Host header required"})
                return
            if path in {"/api/decision", "/api/listing", "/api/compare", "/api/export"}:
                params = parse_qs(urlparse(self.path).query, keep_blank_values=True)
                try:
                    if path == "/api/decision":
                        self._json(200, decisions.query(params))
                    elif path == "/api/listing":
                        self._json(200, decisions.detail(params.get("id", [""])[0]))
                    elif path == "/api/compare":
                        self._json(200, decisions.compare(params.get("id", [])))
                    else:
                        body, mime = decisions.export(params, params.get("format", ["json"])[0])
                        self._headers(200, mime, len(body))
                        self.wfile.write(body)
                except (ValueError, KeyError, TypeError) as exc:
                    self._json(400, {"error": str(exc)})
                except (OSError, RuntimeError, sqlite3.Error) as exc:
                    self._json(503, {"error": str(exc)})
                return
            if path == "/api/report":
                try:
                    self._json(HTTPStatus.OK, snapshots.summary())
                except (OSError, ValueError, RuntimeError) as exc:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
                return
            if path == "/api/listings":
                try:
                    payload = snapshots.listing_page(
                        parse_qs(urlparse(self.path).query, keep_blank_values=True),
                        config,
                    )
                    self._json(HTTPStatus.OK, payload)
                except ValueError as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                except (OSError, RuntimeError, sqlite3.Error) as exc:
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
            path = urlparse(self.path).path
            if path in {"/api/preferences", "/api/evidence", "/api/import", "/api/routes"}:
                if not self._valid_local_request() or self.headers.get("X-Japan-Housing") != "1":
                    self._json(403, {"error": "local dashboard request required"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._json(415, {"error": "JSON required"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 65536:
                        raise ValueError("request exceeds the 64 KiB limit")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ValueError("request must be a JSON object")
                    if path == "/api/preferences":
                        self._json(200, decisions.save(payload))
                    elif path == "/api/routes":
                        self._json(200, decisions.fetch_routes(payload.get("stable_key", "")))
                    else:
                        with decisions.lock:
                            with EvidenceStore(config.runtime.state_path) as evidence:
                                if path == "/api/evidence":
                                    key = payload.get("stable_key", "")
                                    current = decisions.detail(key)
                                    validate_fields(payload.get("values"))
                                    if payload.get("kind") != "estimate":
                                        validate_fields(
                                            {
                                                **{
                                                    k: v
                                                    for k, v in current.items()
                                                    if k in EDITABLE
                                                },
                                                **payload["values"],
                                            }
                                        )
                                    evidence.add(key, payload)
                                else:
                                    key = evidence.import_record(payload)
                            decisions.invalidate()
                        self._json(200, {"stable_key": key})
                except (ValueError, KeyError, TypeError) as exc:
                    self._json(400, {"error": str(exc)})
                except (OSError, RuntimeError, sqlite3.Error) as exc:
                    self._json(503, {"error": str(exc)})
                return
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
                snapshots.update_decision(stable_key, status, note)
                decisions.invalidate()
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
