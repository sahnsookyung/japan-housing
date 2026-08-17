from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from threading import Barrier

import japan_housing.runner as runner
from japan_housing.config import AppConfig
from japan_housing.http import HttpMetrics, HttpResponse
from japan_housing.models import HOUSING_RECORD_SCHEMA_VERSION, HousingRecord, ListingRef
from japan_housing.notify import ReportMessageResult
from japan_housing.routing import CommuteEnrichmentResult
from japan_housing.sources import DiscoveryBatch, SourceSpec
from japan_housing.state import CachedPage


class _FixtureClient:
    def __init__(self, config: object) -> None:
        self.metrics = HttpMetrics()

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def snapshot(self, hosts: frozenset[str]) -> dict[str, object]:
        return {
            "requests": 0,
            "bytes": 0,
            "retries": 0,
            "failures": 0,
            "circuits": 0,
            "durations": (),
            "statuses": {},
        }


class _FixtureAdapter:
    def __init__(self, spec: SourceSpec, document: str) -> None:
        self.spec = spec
        self.document = document

    def discover(self, cursor: str | None, budget: int) -> DiscoveryBatch:
        ref = ListingRef(
            self.spec.source_id,
            "fixture-page",
            "https://www.oakhouse.jp/eng/house/999",
            "fixture-fingerprint",
        )
        return DiscoveryBatch((ref,), None, 1, True)

    def fetch_detail(
        self,
        ref: ListingRef,
        *,
        etag: str = "",
        last_modified: str = "",
    ) -> HttpResponse:
        return HttpResponse(ref.url, 200, self.document, len(self.document), {})


def _cached_record(schema_version: int = HOUSING_RECORD_SCHEMA_VERSION) -> HousingRecord:
    return HousingRecord(
        schema_version=schema_version,
        source_id="oakhouse",
        source_label="Oakhouse",
        external_id="1:101",
        url="https://www.oakhouse.jp/eng/house/1",
        title="Room 101",
    )


def test_oakhouse_cache_is_never_reused_past_four_hours(app_config: AppConfig) -> None:
    ref = ListingRef("oakhouse", "1", "https://www.oakhouse.jp/eng/house/1", "fingerprint")
    cache = CachedPage(
        "fingerprint",
        datetime.now().astimezone() - timedelta(hours=5),
        (_cached_record(),),
        ("excluded",),
    )

    assert not runner._cache_reusable(ref, cache, app_config)


def test_old_record_schema_forces_detail_revalidation(app_config: AppConfig) -> None:
    ref = ListingRef("oakhouse", "1", "https://www.oakhouse.jp/eng/house/1", "fingerprint")
    cache = CachedPage(
        "fingerprint",
        datetime.now().astimezone(),
        (_cached_record(schema_version=1),),
        ("eligible",),
    )

    assert not runner._cache_reusable(ref, cache, app_config)


def test_cache_ttl_uses_current_policy_not_stored_classification(app_config: AppConfig) -> None:
    ref = ListingRef("best_estate", "1", "https://www.best-estate.jp/en/property/1/", "fp")
    record = HousingRecord(
        schema_version=HOUSING_RECORD_SCHEMA_VERSION,
        source_id="best_estate",
        source_label="Best-Estate.jp",
        external_id="1",
        url=ref.url,
        title="Current candidate",
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
        availability_confidence="explicit",
        foreigner_evidence=("accepted",),
        overseas_application_evidence=("online",),
        registration_evidence=("confirmed",),
        registration_confidence="confirmed",
    )
    cache = CachedPage(
        "fp",
        datetime.now().astimezone() - timedelta(hours=5),
        (record,),
        ("excluded",),
    )

    assert not runner._cache_reusable(ref, cache, app_config)


def test_full_refresh_promotes_records_and_publishes_report(
    app_config: AppConfig,
    project_root: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = replace(
        app_config.runtime,
        state_path=tmp_path / "state.sqlite3",
        artifacts_path=tmp_path / "artifacts",
        global_workers=1,
        per_host_workers=1,
        max_queued_tasks=1,
    )
    config = replace(app_config, runtime=runtime)
    spec = SourceSpec(
        "oakhouse",
        "Oakhouse",
        ("https://www.oakhouse.jp/eng/house_index",),
        frozenset({"www.oakhouse.jp"}),
        (r"/eng/house/\d+$",),
        authoritative_disappearance=True,
    )
    document = (project_root / "tests" / "fixtures" / "oakhouse.html").read_text(encoding="utf-8")
    adapter = _FixtureAdapter(spec, document)
    monkeypatch.setattr(runner, "source_specs", lambda: (spec,))
    monkeypatch.setattr(runner, "build_adapters", lambda client: (adapter,))
    monkeypatch.setattr(runner, "BoundedHttpClient", _FixtureClient)
    route_forces: list[bool] = []

    def commute_stub(*args, force: bool = False, **kwargs) -> CommuteEnrichmentResult:
        route_forces.append(force)
        return CommuteEnrichmentResult(
            status="complete",
            candidate_origins=0,
            fresh_origins=0,
            requested_origins=0,
            requested_elements=0,
            requests=0,
            routes_found=0,
            routes_unavailable=0,
            deferred_origins=0,
            departure_at="",
        )

    monkeypatch.setattr(runner, "enrich_commutes", commute_stub)

    outcome = runner.run_refresh(config, force=True, routes_api_key="test-key")

    assert outcome.exit_code == 2
    assert outcome.report.status == "partial"
    assert outcome.source_results[0].status == "healthy"
    assert outcome.report.job_count == 3
    assert outcome.report.json_path.exists()
    markdown = outcome.report.markdown_path.read_text(encoding="utf-8")
    assert "Available-room monthly price: ¥77,000" in markdown
    assert "Property advertised monthly from: ¥68,000" in markdown
    assert (runtime.artifacts_path / "latest.json").exists()
    assert route_forces == [False]


def test_oakhouse_parser_ambiguity_preserves_cached_rooms(
    app_config: AppConfig,
    project_root: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = replace(
        app_config.runtime,
        state_path=tmp_path / "state.sqlite3",
        artifacts_path=tmp_path / "artifacts",
        global_workers=1,
        per_host_workers=1,
        max_queued_tasks=1,
    )
    config = replace(app_config, runtime=runtime)
    spec = SourceSpec(
        "oakhouse",
        "Oakhouse",
        ("https://www.oakhouse.jp/eng/house_index",),
        frozenset({"www.oakhouse.jp"}),
        (r"/eng/house/\d+$",),
        authoritative_disappearance=True,
    )
    document = (project_root / "tests" / "fixtures" / "oakhouse.html").read_text(encoding="utf-8")
    adapter = _FixtureAdapter(spec, document)
    monkeypatch.setattr(runner, "source_specs", lambda: (spec,))
    monkeypatch.setattr(runner, "build_adapters", lambda client: (adapter,))
    monkeypatch.setattr(runner, "BoundedHttpClient", _FixtureClient)
    first = runner.run_refresh(config, force=True)
    assert first.report.job_count == 3
    with runner.StateStore(runtime.state_path) as store, store.connection:
        store.connection.execute(
            "UPDATE detail_cache SET fetched_at=?",
            ((datetime.now().astimezone() - timedelta(hours=5)).isoformat(),),
        )
    adapter.document = document.replace("p-room__caset__row", "unrecognized-room-row")

    second = runner.run_refresh(config, force=True)

    assert second.report.job_count == 3
    assert second.source_results[0].metrics.parse_failures == 1
    assert second.source_results[0].status == "partial"


def test_independent_sources_fetch_concurrently(
    app_config: AppConfig,
    project_root: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = replace(
        app_config.runtime,
        state_path=tmp_path / "state.sqlite3",
        artifacts_path=tmp_path / "artifacts",
        global_workers=2,
        per_host_workers=1,
        max_queued_tasks=2,
    )
    config = replace(app_config, runtime=runtime)
    barrier = Barrier(2)
    specs = (
        SourceSpec(
            "oakhouse",
            "Oakhouse",
            ("https://oak.example/list",),
            frozenset({"oak.example"}),
            (r"/detail/1$",),
        ),
        SourceSpec(
            "best_estate",
            "Best-Estate.jp",
            ("https://best.example/list",),
            frozenset({"best.example"}),
            (r"/detail/1$",),
        ),
    )

    class ConcurrentAdapter:
        def __init__(self, spec: SourceSpec) -> None:
            self.spec = spec

        def discover(self, cursor: str | None, budget: int) -> DiscoveryBatch:
            ref = ListingRef(
                self.spec.source_id,
                "fixture",
                f"https://{next(iter(self.spec.allowed_hosts))}/detail/1",
                "fixture",
            )
            return DiscoveryBatch((ref,), None, 1, True)

        def fetch_detail(
            self,
            ref: ListingRef,
            *,
            etag: str = "",
            last_modified: str = "",
        ) -> HttpResponse:
            barrier.wait(timeout=3)
            document = (
                project_root / "tests" / "fixtures" / f"{self.spec.source_id}.html"
            ).read_text(encoding="utf-8")
            return HttpResponse(ref.url, 200, document, len(document), {})

    adapters = tuple(ConcurrentAdapter(spec) for spec in specs)
    monkeypatch.setattr(runner, "source_specs", lambda: specs)
    monkeypatch.setattr(runner, "build_adapters", lambda client: adapters)
    monkeypatch.setattr(runner, "BoundedHttpClient", _FixtureClient)

    outcome = runner.run_refresh(config, force=True)

    assert outcome.exit_code == 2
    assert [result.status for result in outcome.source_results] == ["healthy", "healthy"]
    assert outcome.report.job_count == 4


def test_report_delivery_failure_makes_run_partial(
    app_config: AppConfig,
    project_root: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = replace(
        app_config.runtime,
        state_path=tmp_path / "state.sqlite3",
        artifacts_path=tmp_path / "artifacts",
        global_workers=1,
        per_host_workers=1,
        max_queued_tasks=1,
    )
    config = replace(app_config, runtime=runtime)
    spec = SourceSpec(
        "oakhouse",
        "Oakhouse",
        ("https://www.oakhouse.jp/eng/house_index",),
        frozenset({"www.oakhouse.jp"}),
        (r"/eng/house/\d+$",),
    )
    document = (project_root / "tests" / "fixtures" / "oakhouse.html").read_text(encoding="utf-8")
    monkeypatch.setattr(runner, "source_specs", lambda: (spec,))
    monkeypatch.setattr(
        runner,
        "build_adapters",
        lambda client: (_FixtureAdapter(spec, document),),
    )
    monkeypatch.setattr(runner, "BoundedHttpClient", _FixtureClient)
    monkeypatch.setattr(
        runner,
        "sync_latest_report_message",
        lambda *args, **kwargs: ReportMessageResult("failed", ""),
    )

    outcome = runner.run_refresh(
        config,
        force=True,
        notify=True,
        webhook_url="https://discord.com/api/webhooks/12345/test-token",
        bootstrap=True,
    )

    assert outcome.source_results[0].status == "healthy"
    assert outcome.report_message is not None
    assert outcome.report_message.status == "failed"
    assert outcome.exit_code == 2
