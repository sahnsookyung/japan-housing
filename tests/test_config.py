from __future__ import annotations

from pathlib import Path

import pytest

from japan_housing.config import DEFAULT_USER_AGENT, load_config


def test_profile_matches_move_window_and_safe_runtime(project_root: Path) -> None:
    config = load_config(project_root / "config" / "profile.toml")

    assert config.profile.move_in_earliest.isoformat() == "2026-09-15"
    assert config.profile.move_in_latest.isoformat() == "2026-09-28"
    assert config.profile.monthly_budget_yen == 80_000
    assert config.profile.commute_anchors == (
        "Kichijoji Station",
        "Shibuya Station",
        "Shinjuku Station",
    )
    assert config.ranking.anchor_label == "Kichijoji / Shibuya / Shinjuku midpoint"
    station_coordinates = (
        (35 + 42 / 60 + 10 / 3600, 139 + 34 / 60 + 47 / 3600),
        (35 + 39 / 60 + 31 / 3600, 139 + 42 / 60 + 5 / 3600),
        (35 + 41 / 60 + 27 / 3600, 139 + 42 / 60 + 1 / 3600),
    )
    centroid = tuple(
        sum(point[index] for point in station_coordinates) / len(station_coordinates)
        for index in range(2)
    )
    assert config.ranking.anchor_latitude == pytest.approx(centroid[0], abs=0.0000001)
    assert config.ranking.anchor_longitude == pytest.approx(centroid[1], abs=0.0000001)
    assert config.ranking.price_weight == 0.60
    assert config.ranking.distance_weight == 0.40
    assert config.ranking.price_reference_yen == 80_000
    assert config.ranking.distance_reference_km == 20.0
    assert config.ranking.maximum_distance_km == 50.0
    assert config.runtime.dashboard_host == "127.0.0.1"
    assert config.runtime.user_agent == DEFAULT_USER_AGENT


def test_invalid_move_window_is_rejected(project_root: Path, tmp_path: Path) -> None:
    profile = (project_root / "config" / "profile.toml").read_text(encoding="utf-8")
    invalid = profile.replace('move_in_latest = "2026-09-28"', 'move_in_latest = "2026-09-01"')
    path = tmp_path / "invalid.toml"
    path.write_text(invalid, encoding="utf-8")

    with pytest.raises(ValueError, match="cannot be before"):
        load_config(path)


def test_missing_runtime_values_use_real_defaults(project_root: Path, tmp_path: Path) -> None:
    profile = (project_root / "config" / "profile.toml").read_text(encoding="utf-8")
    profile_only = profile.split("[runtime]", 1)[0]
    path = tmp_path / "defaults.toml"
    path.write_text(profile_only, encoding="utf-8")

    config = load_config(path)

    assert config.runtime.user_agent == DEFAULT_USER_AGENT
    assert config.runtime.global_workers == 8
    assert config.runtime.partial_retry_minutes == 60
    assert config.runtime.report_retention_days == 30


def test_ranking_weights_must_sum_to_one(project_root: Path, tmp_path: Path) -> None:
    profile = (project_root / "config" / "profile.toml").read_text(encoding="utf-8")
    invalid = profile.replace("distance_weight = 0.40", "distance_weight = 0.60")
    path = tmp_path / "invalid-ranking.toml"
    path.write_text(invalid, encoding="utf-8")

    with pytest.raises(ValueError, match="must sum to 1"):
        load_config(path)
