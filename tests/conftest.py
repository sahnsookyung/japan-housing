from __future__ import annotations

from pathlib import Path

import pytest

from japan_housing.config import AppConfig, load_config


@pytest.fixture
def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def app_config(project_root: Path) -> AppConfig:
    return load_config(project_root / "config" / "profile.toml")
