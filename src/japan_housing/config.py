"""TOML configuration loading with strict validation and safe defaults."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

DEFAULT_USER_AGENT = "JapanHousingBot/0.1 (+local personal housing search)"


@dataclass(frozen=True, slots=True)
class SearchProfile:
    move_in_earliest: date
    move_in_latest: date
    landing_duration_months_min: int
    landing_duration_months_max: int
    long_term_comparison_months: int
    monthly_budget_yen: int
    visa_status: str
    japanese_level: str
    applicant_gender: str
    target_prefectures: tuple[str, ...]
    allowed_housing_types: frozenset[str]
    private_space_required: bool
    registration_required: bool
    commute_anchors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    state_path: Path
    artifacts_path: Path
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8766
    global_workers: int = 8
    per_host_workers: int = 2
    max_queued_tasks: int = 16
    per_host_interval_seconds: float = 0.75
    list_page_budget_per_source: int = 100
    detail_budget_per_source: int = 300
    request_budget_per_run: int = 1200
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 20.0
    response_limit_bytes: int = 5 * 1024 * 1024
    max_attempts: int = 3
    circuit_failure_threshold: int = 5
    checkpoint_every: int = 25
    alert_limit: int = 20
    partial_retry_minutes: int = 60
    report_retention_days: int = 30
    state_retention_days: int = 90
    notification_max_attempts: int = 3
    user_agent: str = DEFAULT_USER_AGENT


@dataclass(frozen=True, slots=True)
class RankingConfig:
    anchor_label: str = "Kichijoji / Shibuya / Shinjuku midpoint"
    anchor_latitude: float = 35.6840741
    anchor_longitude: float = 139.6604630
    price_weight: float = 0.60
    distance_weight: float = 0.40
    price_reference_yen: int = 80_000
    distance_reference_km: float = 20.0
    missing_distance_penalty_km: float = 30.0
    maximum_distance_km: float = 50.0


@dataclass(frozen=True, slots=True)
class AppConfig:
    profile: SearchProfile
    runtime: RuntimeConfig
    ranking: RankingConfig


def _positive(name: str, value: int | float) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be positive")


def load_config(path: Path) -> AppConfig:
    with path.open("rb") as handle:
        payload = tomllib.load(handle)
    profile_data = payload.get("profile") or {}
    runtime_data = payload.get("runtime") or {}
    ranking_data = payload.get("ranking") or {}
    try:
        profile = SearchProfile(
            move_in_earliest=date.fromisoformat(str(profile_data["move_in_earliest"])),
            move_in_latest=date.fromisoformat(str(profile_data["move_in_latest"])),
            landing_duration_months_min=int(profile_data["landing_duration_months_min"]),
            landing_duration_months_max=int(profile_data["landing_duration_months_max"]),
            long_term_comparison_months=int(profile_data["long_term_comparison_months"]),
            monthly_budget_yen=int(profile_data["monthly_budget_yen"]),
            visa_status=str(profile_data["visa_status"]).lower(),
            japanese_level=str(profile_data["japanese_level"]).upper(),
            applicant_gender=str(profile_data["applicant_gender"]).lower(),
            target_prefectures=tuple(str(value) for value in profile_data["target_prefectures"]),
            allowed_housing_types=frozenset(
                str(value) for value in profile_data["allowed_housing_types"]
            ),
            private_space_required=bool(profile_data["private_space_required"]),
            registration_required=bool(profile_data["registration_required"]),
            commute_anchors=tuple(str(value) for value in profile_data.get("commute_anchors", ())),
        )
    except KeyError as exc:
        raise ValueError(f"missing profile configuration: {exc.args[0]}") from exc
    if profile.move_in_latest < profile.move_in_earliest:
        raise ValueError("move_in_latest cannot be before move_in_earliest")
    if profile.landing_duration_months_max < profile.landing_duration_months_min:
        raise ValueError("landing duration maximum cannot be below minimum")
    if profile.japanese_level not in {"N1", "N2", "N3", "N4", "N5"}:
        raise ValueError("japanese_level must be N1 through N5")
    if profile.applicant_gender not in {"male", "female", "other", "unspecified"}:
        raise ValueError("applicant_gender is unsupported")
    known_types = {"private_room", "1R", "1K"}
    if not profile.allowed_housing_types or not profile.allowed_housing_types <= known_types:
        raise ValueError("allowed_housing_types contains an unsupported value")
    for name in (
        "landing_duration_months_min",
        "landing_duration_months_max",
        "long_term_comparison_months",
        "monthly_budget_yen",
    ):
        _positive(name, getattr(profile, name))

    runtime = RuntimeConfig(
        state_path=Path(runtime_data.get("state_path", "state/housing.sqlite3")),
        artifacts_path=Path(runtime_data.get("artifacts_path", "artifacts")),
        dashboard_host=str(runtime_data.get("dashboard_host", "127.0.0.1")),
        dashboard_port=int(runtime_data.get("dashboard_port", 8766)),
        global_workers=int(runtime_data.get("global_workers", 8)),
        per_host_workers=int(runtime_data.get("per_host_workers", 2)),
        max_queued_tasks=int(runtime_data.get("max_queued_tasks", 16)),
        per_host_interval_seconds=float(runtime_data.get("per_host_interval_seconds", 0.75)),
        list_page_budget_per_source=int(runtime_data.get("list_page_budget_per_source", 100)),
        detail_budget_per_source=int(runtime_data.get("detail_budget_per_source", 300)),
        request_budget_per_run=int(runtime_data.get("request_budget_per_run", 1200)),
        connect_timeout_seconds=float(runtime_data.get("connect_timeout_seconds", 5.0)),
        read_timeout_seconds=float(runtime_data.get("read_timeout_seconds", 20.0)),
        response_limit_bytes=int(runtime_data.get("response_limit_bytes", 5 * 1024 * 1024)),
        max_attempts=int(runtime_data.get("max_attempts", 3)),
        circuit_failure_threshold=int(runtime_data.get("circuit_failure_threshold", 5)),
        checkpoint_every=int(runtime_data.get("checkpoint_every", 25)),
        alert_limit=int(runtime_data.get("alert_limit", 20)),
        partial_retry_minutes=int(runtime_data.get("partial_retry_minutes", 60)),
        report_retention_days=int(runtime_data.get("report_retention_days", 30)),
        state_retention_days=int(runtime_data.get("state_retention_days", 90)),
        notification_max_attempts=int(runtime_data.get("notification_max_attempts", 3)),
        user_agent=str(runtime_data.get("user_agent", DEFAULT_USER_AGENT)),
    )
    if runtime.dashboard_host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("dashboard_host must be a loopback address")
    if not 1024 <= runtime.dashboard_port <= 65535:
        raise ValueError("dashboard_port must be between 1024 and 65535")
    for name in (
        "global_workers",
        "per_host_workers",
        "max_queued_tasks",
        "list_page_budget_per_source",
        "detail_budget_per_source",
        "request_budget_per_run",
        "response_limit_bytes",
        "max_attempts",
        "circuit_failure_threshold",
        "checkpoint_every",
        "alert_limit",
        "partial_retry_minutes",
        "report_retention_days",
        "state_retention_days",
        "notification_max_attempts",
    ):
        _positive(name, getattr(runtime, name))
    if runtime.max_queued_tasks < runtime.global_workers:
        raise ValueError("max_queued_tasks must be at least global_workers")
    if runtime.per_host_workers > runtime.global_workers:
        raise ValueError("per_host_workers cannot exceed global_workers")
    for name in ("per_host_interval_seconds", "connect_timeout_seconds", "read_timeout_seconds"):
        _positive(name, getattr(runtime, name))

    ranking = RankingConfig(
        anchor_label=str(
            ranking_data.get("anchor_label", "Kichijoji / Shibuya / Shinjuku midpoint")
        ),
        anchor_latitude=float(ranking_data.get("anchor_latitude", 35.6840741)),
        anchor_longitude=float(ranking_data.get("anchor_longitude", 139.6604630)),
        price_weight=float(ranking_data.get("price_weight", 0.60)),
        distance_weight=float(ranking_data.get("distance_weight", 0.40)),
        price_reference_yen=int(ranking_data.get("price_reference_yen", 80_000)),
        distance_reference_km=float(ranking_data.get("distance_reference_km", 20.0)),
        missing_distance_penalty_km=float(ranking_data.get("missing_distance_penalty_km", 30.0)),
        maximum_distance_km=float(ranking_data.get("maximum_distance_km", 50.0)),
    )
    if not ranking.anchor_label.strip():
        raise ValueError("ranking anchor_label cannot be empty")
    if not -90 <= ranking.anchor_latitude <= 90:
        raise ValueError("ranking anchor_latitude must be between -90 and 90")
    if not -180 <= ranking.anchor_longitude <= 180:
        raise ValueError("ranking anchor_longitude must be between -180 and 180")
    for name in (
        "price_weight",
        "distance_weight",
        "price_reference_yen",
        "distance_reference_km",
        "missing_distance_penalty_km",
        "maximum_distance_km",
    ):
        _positive(name, getattr(ranking, name))
    if abs(ranking.price_weight + ranking.distance_weight - 1.0) > 1e-9:
        raise ValueError("ranking price_weight and distance_weight must sum to 1")
    if ranking.missing_distance_penalty_km < ranking.distance_reference_km:
        raise ValueError("missing_distance_penalty_km must be at least distance_reference_km")
    if ranking.maximum_distance_km < ranking.distance_reference_km:
        raise ValueError("maximum_distance_km must be at least distance_reference_km")
    return AppConfig(profile, runtime, ranking)
