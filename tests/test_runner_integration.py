from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import japan_housing.runner as runner
from japan_housing.config import AppConfig
from japan_housing.http import HttpMetrics
from japan_housing.models import HOUSING_RECORD_SCHEMA_VERSION, HousingRecord, ListingRef
from japan_housing.sources import DiscoveryBatch, SourceSpec
from japan_housing.state import CachedPage


class _FixtureClient:
    def __init__(self, config: object) -> None:
        self.metrics = HttpMetrics()

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None


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

    def fetch_detail(self, ref: ListingRef) -> str:
        return self.document


def _cached_record(schema_version: int = HOUSING_RECORD_SCHEMA_VERSION) -> HousingRecord:
    return HousingRecord(
        schema_version=schema_version,
        source_id="oakhouse",
        source_label="Oakhouse",
        external_id="1:101",
        url="https://www.oakhouse.jp/eng/house/1",
        title="Room 101",
    )


def test_oakhouse_cache_is_never_reused_past_four_hours() -> None:
    ref = ListingRef("oakhouse", "1", "https://www.oakhouse.jp/eng/house/1", "fingerprint")
    cache = CachedPage(
        "fingerprint",
        datetime.now().astimezone() - timedelta(hours=5),
        (_cached_record(),),
        ("excluded",),
    )

    assert not runner._cache_reusable(ref, cache)


def test_old_record_schema_forces_detail_revalidation() -> None:
    ref = ListingRef("oakhouse", "1", "https://www.oakhouse.jp/eng/house/1", "fingerprint")
    cache = CachedPage(
        "fingerprint",
        datetime.now().astimezone(),
        (_cached_record(schema_version=1),),
        ("eligible",),
    )

    assert not runner._cache_reusable(ref, cache)


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

    outcome = runner.run_refresh(config, force=True)

    assert outcome.exit_code == 0
    assert outcome.source_results[0].status == "healthy"
    assert outcome.report.job_count == 3
    assert outcome.report.json_path.exists()
    markdown = outcome.report.markdown_path.read_text(encoding="utf-8")
    assert "Available-room monthly price: ¥77,000" in markdown
    assert "Property advertised monthly from: ¥68,000" in markdown
    assert (runtime.artifacts_path / "latest.json").exists()
