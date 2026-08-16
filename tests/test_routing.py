from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from japan_housing.config import AppConfig
from japan_housing.models import HOUSING_RECORD_SCHEMA_VERSION, HousingRecord
from japan_housing.policy import classify
from japan_housing.routing import enrich_commutes, representative_departure
from japan_housing.sources import SourceSpec
from japan_housing.state import StateStore


def _record() -> HousingRecord:
    return HousingRecord(
        schema_version=HOUSING_RECORD_SCHEMA_VERSION,
        source_id="fixture",
        source_label="Fixture",
        external_id="one",
        url="https://example.invalid/one",
        title="Route fixture",
        prefecture="Tokyo",
        latitude=35.68,
        longitude=139.66,
        coordinate_basis="fixture",
        housing_type="1K",
        private_space=True,
        recurring_total_yen=65_000,
        initial_cost_yen=0,
        exit_cost_yen=0,
        available_from="2026-09-15",
        available_text="2026-09-15",
        availability_confidence="explicit",
        contract_type="monthly",
        foreigner_evidence=("accepted",),
        overseas_application_evidence=("online",),
        registration_evidence=("confirmed",),
        registration_confidence="confirmed",
    )


def _spec() -> SourceSpec:
    return SourceSpec(
        "fixture",
        "Fixture",
        ("https://example.invalid/list",),
        frozenset({"example.invalid"}),
        (r"/one$",),
    )


def _promote(store: StateStore, config: AppConfig) -> HousingRecord:
    record = _record()
    store.ensure_sources((_spec(),))
    store.begin_source_run("initial", "fixture")
    store.stage(
        "initial",
        "page",
        record,
        classify(record, config.profile, config.ranking, routing=config.routing),
    )
    store.promote("initial", _spec())
    return record


def test_representative_departure_is_next_configured_weekday(app_config: AppConfig) -> None:
    now = datetime(2026, 8, 17, 9, 0, tzinfo=ZoneInfo("Asia/Tokyo"))

    departure = representative_departure(app_config.routing, now)

    assert departure.isoformat() == "2026-08-18T08:30:00+09:00"


def test_route_matrix_is_batched_cached_and_aggregated(
    app_config: AppConfig, tmp_path: Path
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "routes.googleapis.com"
        assert request.headers["x-goog-api-key"] == "test-key"
        assert "test-key" not in str(request.url)
        payload = __import__("json").loads(request.content)
        mode = payload["travelMode"]
        calls.append(mode)
        assert len(payload["origins"]) == 1
        assert len(payload["destinations"]) == 3
        assert ("departureTime" in payload) is (mode == "TRANSIT")
        base = 1200 if mode == "TRANSIT" else 1800
        return httpx.Response(
            200,
            json=[
                {
                    "originIndex": 0,
                    "destinationIndex": index,
                    "condition": "ROUTE_EXISTS",
                    "duration": f"{base + index * 60}s",
                    "distanceMeters": 8000 + index * 100,
                    "status": {},
                }
                for index in range(3)
            ],
        )

    transport = httpx.MockTransport(handler)
    with StateStore(tmp_path / "state.sqlite3") as store:
        record = _promote(store, app_config)
        result = enrich_commutes(store, app_config, "test-key", transport=transport)
        summary = store.commute_summary(record, app_config.routing, app_config.ranking)
        coverage = store.commute_coverage(app_config.routing)
        reclassified = store.reclassify_active(
            app_config.profile,
            app_config.ranking,
            app_config.routing,
        )
        ranked = store.active_records()[0]
        cached = enrich_commutes(
            store,
            app_config,
            "test-key",
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(AssertionError("cache was bypassed"))
            ),
        )

    assert calls == ["TRANSIT", "BICYCLE"]
    assert result.status == "complete"
    assert result.requested_elements == 6
    assert result.routes_found == 6
    assert summary.transit_minutes_mean == 21.0
    assert summary.bicycle_minutes_mean == 31.0
    assert summary.transit_coverage == 3
    assert summary.bicycle_coverage == 3
    assert coverage["coverage_ratio"] == 1.0
    assert reclassified == 1
    assert ranked["transit_minutes_mean"] == 21.0
    assert ranked["bicycle_minutes_mean"] == 31.0
    assert ranked["priority_transit_component"] < 10.0
    assert ranked["priority_bicycle_component"] < 10.0
    assert cached.requests == 0


def test_provider_errors_are_secret_safe(app_config: AppConfig, tmp_path: Path) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(403, json={"error": "denied"}))
    with StateStore(tmp_path / "state.sqlite3") as store:
        _promote(store, app_config)
        result = enrich_commutes(store, app_config, "secret-key", transport=transport)

    assert result.status == "failed"
    assert result.error == "Google Routes HTTP 403"
    assert "secret-key" not in result.error


def test_incomplete_provider_matrix_is_not_cached(app_config: AppConfig, tmp_path: Path) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=[]))
    with StateStore(tmp_path / "state.sqlite3") as store:
        _promote(store, app_config)
        result = enrich_commutes(store, app_config, "test-key", transport=transport)
        cached_rows = store.connection.execute("SELECT COUNT(*) FROM commute_routes").fetchone()[0]

    assert result.status == "failed"
    assert result.error == "Google Routes returned an incomplete matrix"
    assert cached_rows == 0
