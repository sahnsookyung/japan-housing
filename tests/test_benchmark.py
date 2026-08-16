from __future__ import annotations

from pathlib import Path

from japan_housing.config import AppConfig
from japan_housing.runner import benchmark_fixtures


def test_fixture_benchmark_parses_all_sources(app_config: AppConfig, project_root: Path) -> None:
    result = benchmark_fixtures(app_config, project_root / "tests" / "fixtures")

    assert result["records"] == 5
    assert result["bytes"] > 0
