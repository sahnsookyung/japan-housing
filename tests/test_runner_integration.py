from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import japan_housing.runner as runner
from japan_housing.config import AppConfig
from japan_housing.http import HttpMetrics
from japan_housing.models import ListingRef
from japan_housing.sources import DiscoveryBatch, SourceSpec


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
    assert outcome.report.job_count == 2
    assert outcome.report.json_path.exists()
    assert (runtime.artifacts_path / "latest.json").exists()
