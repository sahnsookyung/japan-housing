"""Bounded, cached commute enrichment through Google Routes route matrices."""

from __future__ import annotations

import hashlib
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import httpx

from .config import AppConfig, RouteAnchor, RoutingConfig
from .envfile import env_value
from .models import HousingRecord, iso_now

if TYPE_CHECKING:
    from .state import StateStore

ROUTES_API_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
FIELD_MASK = "originIndex,destinationIndex,duration,distanceMeters,status,condition"
MODES = ("TRANSIT", "BICYCLE")


@dataclass(frozen=True, slots=True)
class RouteOrigin:
    key: str
    latitude: float
    longitude: float


@dataclass(frozen=True, slots=True)
class RouteMatrixValue:
    origin_key: str
    origin_latitude: float
    origin_longitude: float
    anchor_key: str
    anchor_id: str
    anchor_label: str
    mode: str
    duration_seconds: int | None
    distance_meters: int | None
    condition: str
    fetched_at: str
    departure_at: str
    schedule_key: str


@dataclass(frozen=True, slots=True)
class CommuteEnrichmentResult:
    status: str
    candidate_origins: int
    fresh_origins: int
    requested_origins: int
    requested_elements: int
    requests: int
    routes_found: int
    routes_unavailable: int
    deferred_origins: int
    departure_at: str
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "candidate_origins": self.candidate_origins,
            "fresh_origins": self.fresh_origins,
            "requested_origins": self.requested_origins,
            "requested_elements": self.requested_elements,
            "requests": self.requests,
            "routes_found": self.routes_found,
            "routes_unavailable": self.routes_unavailable,
            "deferred_origins": self.deferred_origins,
            "departure_at": self.departure_at,
            "error": self.error,
        }


class RoutesApiError(RuntimeError):
    """A secret-safe routing provider failure."""


def coordinate_key(latitude: float, longitude: float) -> str:
    return f"{latitude:.5f},{longitude:.5f}"


def anchor_key(anchor: RouteAnchor) -> str:
    payload = f"{anchor.anchor_id}|{anchor.latitude:.7f}|{anchor.longitude:.7f}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def transit_schedule_key(routing: RoutingConfig) -> str:
    payload = f"{routing.departure_weekday}|{routing.departure_time}|{routing.timezone}|TRANSIT"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def origin_for(record: HousingRecord) -> RouteOrigin | None:
    if record.latitude is None or record.longitude is None:
        return None
    return RouteOrigin(
        coordinate_key(record.latitude, record.longitude),
        record.latitude,
        record.longitude,
    )


def resolve_routes_api_key(env_file: Path | None = Path(".env"), *, required: bool) -> str | None:
    value = env_value("GOOGLE_ROUTES_API_KEY", env_file)
    if not value:
        if required:
            raise ValueError("GOOGLE_ROUTES_API_KEY is not configured")
        return None
    if len(value) > 256 or any(character.isspace() for character in value):
        raise ValueError("GOOGLE_ROUTES_API_KEY has an invalid format")
    return value


def representative_departure(
    routing: RoutingConfig,
    now: datetime | None = None,
) -> datetime:
    timezone = ZoneInfo(routing.timezone)
    current = now.astimezone(timezone) if now else datetime.now(timezone)
    local_time = datetime.strptime(routing.departure_time, "%H:%M").time()
    days_ahead = (routing.departure_weekday - current.weekday()) % 7
    candidate = datetime.combine(current.date() + timedelta(days=days_ahead), local_time, timezone)
    if candidate <= current + timedelta(hours=1):
        candidate += timedelta(days=7)
    return candidate


def _duration_seconds(value: object) -> int | None:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)s", str(value or ""))
    return round(float(match.group(1))) if match else None


class GoogleRoutesClient:
    def __init__(
        self,
        api_key: str,
        config: AppConfig,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._response_limit = min(config.runtime.response_limit_bytes, 2 * 1024 * 1024)
        self._client = httpx.Client(
            timeout=httpx.Timeout(
                connect=config.runtime.connect_timeout_seconds,
                read=config.runtime.read_timeout_seconds,
                write=config.runtime.read_timeout_seconds,
                pool=config.runtime.connect_timeout_seconds,
            ),
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": api_key,
                "X-Goog-FieldMask": FIELD_MASK,
                "User-Agent": config.runtime.user_agent,
            },
            transport=transport,
        )

    def __enter__(self) -> GoogleRoutesClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def compute_matrix(
        self,
        mode: str,
        origins: list[RouteOrigin],
        anchors: tuple[RouteAnchor, ...],
        departure: datetime,
        schedule_key: str,
    ) -> list[RouteMatrixValue]:
        if mode not in MODES:
            raise ValueError("unsupported route mode")
        if not origins or len(origins) * len(anchors) > (100 if mode == "TRANSIT" else 625):
            raise ValueError("route matrix batch is empty or exceeds the provider limit")
        request: dict[str, Any] = {
            "origins": [self._waypoint(item.latitude, item.longitude) for item in origins],
            "destinations": [
                self._waypoint(anchor.latitude, anchor.longitude) for anchor in anchors
            ],
            "travelMode": mode,
        }
        departure_at = ""
        if mode == "TRANSIT":
            departure_at = departure.isoformat(timespec="seconds")
            request["departureTime"] = departure_at
        response = self._post(request)
        try:
            payload = response.json()
        except ValueError as exc:
            raise RoutesApiError("Google Routes returned invalid JSON") from exc
        if not isinstance(payload, list):
            raise RoutesApiError("Google Routes returned an invalid matrix")
        fetched_at = iso_now()
        results: list[RouteMatrixValue] = []
        observed_pairs: set[tuple[int, int]] = set()
        for item in payload:
            if not isinstance(item, dict):
                continue
            origin_index = item.get("originIndex")
            destination_index = item.get("destinationIndex")
            if not isinstance(origin_index, int) or not isinstance(destination_index, int):
                continue
            if not 0 <= origin_index < len(origins) or not 0 <= destination_index < len(anchors):
                raise RoutesApiError("Google Routes returned an out-of-range matrix index")
            pair = (origin_index, destination_index)
            if pair in observed_pairs:
                raise RoutesApiError("Google Routes returned a duplicate matrix element")
            observed_pairs.add(pair)
            origin = origins[origin_index]
            anchor = anchors[destination_index]
            condition = str(item.get("condition") or "UNKNOWN")
            duration = _duration_seconds(item.get("duration"))
            distance = item.get("distanceMeters")
            results.append(
                RouteMatrixValue(
                    origin.key,
                    origin.latitude,
                    origin.longitude,
                    anchor_key(anchor),
                    anchor.anchor_id,
                    anchor.label,
                    mode,
                    duration if condition == "ROUTE_EXISTS" else None,
                    int(distance)
                    if condition == "ROUTE_EXISTS" and isinstance(distance, int)
                    else None,
                    condition,
                    fetched_at,
                    departure_at,
                    schedule_key if mode == "TRANSIT" else "",
                )
            )
        if len(observed_pairs) != len(origins) * len(anchors):
            raise RoutesApiError("Google Routes returned an incomplete matrix")
        return results

    @staticmethod
    def _waypoint(latitude: float, longitude: float) -> dict[str, object]:
        return {
            "waypoint": {"location": {"latLng": {"latitude": latitude, "longitude": longitude}}}
        }

    def _post(self, payload: dict[str, Any]) -> httpx.Response:
        for attempt in range(2):
            try:
                response = self._client.post(ROUTES_API_URL, json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == 0:
                    time.sleep(0.5 * random.uniform(0.75, 1.25))
                    continue
                raise RoutesApiError(
                    f"Google Routes transport failure: {type(exc).__name__}"
                ) from exc
            if len(response.content) > self._response_limit:
                raise RoutesApiError("Google Routes response exceeded the configured size limit")
            if response.status_code < 400:
                return response
            retryable = response.status_code == 429 or response.status_code >= 500
            if retryable and attempt == 0:
                retry_after = response.headers.get("retry-after", "")
                try:
                    delay = min(10.0, max(0.0, float(retry_after)))
                except ValueError:
                    delay = 0.5
                time.sleep(delay)
                continue
            raise RoutesApiError(f"Google Routes HTTP {response.status_code}")
        raise RoutesApiError("Google Routes request failed")


def enrich_commutes(
    store: StateStore,
    config: AppConfig,
    api_key: str,
    *,
    force: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> CommuteEnrichmentResult:
    routing = config.routing
    if not routing.enabled:
        return CommuteEnrichmentResult("disabled", 0, 0, 0, 0, 0, 0, 0, 0, "")
    store.reclassify_active(config.profile, config.ranking, routing)
    records = store.routing_candidates()
    origins_by_key = {
        origin.key: origin for record in records if (origin := origin_for(record)) is not None
    }
    origins = list(origins_by_key.values())
    departure = representative_departure(routing)
    schedule_key = transit_schedule_key(routing)
    due_by_mode = {
        mode: store.commute_origins_due(origins, routing, mode, force=force) for mode in MODES
    }
    due_keys = {origin.key for values in due_by_mode.values() for origin in values}
    fresh_origins = len(origins_by_key.keys() - due_keys)
    requested_origins = 0
    requested_elements = 0
    requests = 0
    routes_found = 0
    routes_unavailable = 0
    error = ""
    remaining_elements = routing.max_elements_per_run
    pending = {mode: list(values) for mode, values in due_by_mode.items()}
    try:
        with GoogleRoutesClient(api_key, config, transport=transport) as client:
            while any(pending.values()) and remaining_elements >= len(routing.anchors):
                made_progress = False
                for mode in MODES:
                    maximum_origins = min(
                        routing.batch_origins,
                        remaining_elements // len(routing.anchors),
                    )
                    if not pending[mode] or maximum_origins <= 0:
                        continue
                    batch = pending[mode][:maximum_origins]
                    del pending[mode][:maximum_origins]
                    values = client.compute_matrix(
                        mode,
                        batch,
                        routing.anchors,
                        departure,
                        schedule_key,
                    )
                    store.save_commute_routes(values)
                    elements = len(batch) * len(routing.anchors)
                    remaining_elements -= elements
                    requested_elements += elements
                    requested_origins += len(batch)
                    requests += 1
                    routes_found += sum(value.condition == "ROUTE_EXISTS" for value in values)
                    routes_unavailable += sum(value.condition != "ROUTE_EXISTS" for value in values)
                    made_progress = True
                if not made_progress:
                    break
    except RoutesApiError as exc:
        error = str(exc)
    if requested_elements:
        store.reclassify_active(config.profile, config.ranking, routing)
    deferred = sum(len(values) for values in pending.values())
    if error:
        status = "failed" if not requested_elements else "partial"
    elif deferred:
        status = "partial"
    else:
        status = "complete"
    return CommuteEnrichmentResult(
        status,
        len(origins),
        fresh_origins,
        requested_origins,
        requested_elements,
        requests,
        routes_found,
        routes_unavailable,
        deferred,
        departure.isoformat(timespec="seconds"),
        error,
    )
