"""Typed records shared by source adapters, policy, state, and reports."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal

ClassificationName = Literal["eligible", "review", "excluded"]
SourceStatus = Literal["healthy", "partial", "failed", "drifted"]
HOUSING_RECORD_SCHEMA_VERSION = 5


@dataclass(frozen=True, slots=True)
class ListingRef:
    source_id: str
    external_id: str
    url: str
    list_fingerprint: str
    title_hint: str = ""
    last_modified: str | None = None
    latitude: float | None = None
    longitude: float | None = None


@dataclass(slots=True)
class HousingRecord:
    schema_version: int
    source_id: str
    source_label: str
    external_id: str
    url: str
    title: str
    property_name: str = ""
    unit_name: str = ""
    address: str = ""
    prefecture: str = ""
    city: str = ""
    latitude: float | None = None
    longitude: float | None = None
    coordinate_basis: str = "unknown"
    nearest_stations: tuple[str, ...] = ()
    walk_minutes_min: int | None = None
    housing_type: str = "unknown"
    private_space: bool | None = None
    furnished: bool | None = None
    rent_yen: int | None = None
    management_fee_yen: int | None = None
    mandatory_monthly_fees_yen: int | None = None
    recurring_total_yen: int | None = None
    advertised_monthly_from_yen: int | None = None
    price_basis: str = "unknown"
    price_validation: str = "unknown"
    initial_cost_yen: int | None = None
    exit_cost_yen: int | None = None
    unknown_costs: tuple[str, ...] = ()
    available_from: str | None = None
    available_text: str = ""
    availability_confidence: str = "unknown"
    source_availability_status: str = "unknown"
    minimum_stay_days: int | None = None
    maximum_stay_days: int | None = None
    contract_type: str = "unknown"
    foreigner_evidence: tuple[str, ...] = ()
    visa_evidence: tuple[str, ...] = ()
    overseas_application_evidence: tuple[str, ...] = ()
    registration_evidence: tuple[str, ...] = ()
    registration_confidence: str = "unknown"
    guarantor_evidence: tuple[str, ...] = ()
    occupancy_restrictions: tuple[str, ...] = ()
    source_evidence: tuple[str, ...] = ()
    list_fingerprint: str = ""
    observed_at: str = ""
    detail_verified_at: str = ""
    raw_evidence_hash: str = ""
    cluster_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> HousingRecord:
        tuple_fields = {
            "nearest_stations",
            "unknown_costs",
            "foreigner_evidence",
            "visa_evidence",
            "overseas_application_evidence",
            "registration_evidence",
            "guarantor_evidence",
            "occupancy_restrictions",
            "source_evidence",
        }
        normalized = dict(payload)
        for name in tuple_fields:
            normalized[name] = tuple(normalized.get(name) or ())
        return cls(**normalized)

    def fingerprint(self) -> str:
        ignored = {"observed_at", "detail_verified_at", "raw_evidence_hash"}
        payload = {key: value for key, value in self.to_dict().items() if key not in ignored}
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode()).hexdigest()

    @property
    def stable_key(self) -> str:
        return f"{self.source_id}:{self.external_id}"


@dataclass(frozen=True, slots=True)
class CostScenario:
    months: int
    known_total_yen: int | None
    effective_monthly_yen: int | None
    complete: bool
    unknown_costs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnchorCommute:
    anchor_id: str
    anchor_label: str
    transit_minutes: float | None
    transit_distance_km: float | None
    transit_condition: str
    bicycle_minutes: float | None
    bicycle_distance_km: float | None
    bicycle_condition: str


@dataclass(frozen=True, slots=True)
class CommuteSummary:
    anchors: tuple[AnchorCommute, ...]
    transit_minutes_mean: float | None
    bicycle_minutes_mean: float | None
    transit_minutes_scored: float
    bicycle_minutes_scored: float
    transit_coverage: int
    bicycle_coverage: int
    anchor_count: int
    data_hash: str


@dataclass(frozen=True, slots=True)
class Classification:
    name: ClassificationName
    track: str
    reasons: tuple[str, ...]
    cautions: tuple[str, ...]
    rank_key: tuple[Any, ...]
    priority_score: float
    priority_price_component: float
    priority_distance_component: float
    priority_transit_component: float
    priority_bicycle_component: float
    distance_to_anchor_km: float | None
    transit_minutes_mean: float | None
    bicycle_minutes_mean: float | None
    transit_minutes_scored: float
    bicycle_minutes_scored: float
    transit_route_coverage: int
    bicycle_route_coverage: int
    commute_anchor_count: int
    commute_routes: tuple[AnchorCommute, ...]
    commute_data_hash: str
    ranking_price_yen: int | None
    ranking_price_basis: str
    priority_closer_score: float
    priority_cheaper_score: float
    policy_version: int
    policy_config_hash: str
    landing_30: CostScenario
    landing_60: CostScenario
    landing_90: CostScenario
    long_term: CostScenario


@dataclass(slots=True)
class SourceMetrics:
    list_pages: int = 0
    discovered: int = 0
    detail_candidates: int = 0
    detail_network: int = 0
    detail_cache_hits: int = 0
    parsed: int = 0
    parse_failures: int = 0
    detail_fetch_failures: int = 0
    explicit_closed: int = 0
    stale_revalidation_candidates: int = 0
    stale_revalidated: int = 0
    requests: int = 0
    bytes_downloaded: int = 0
    retries: int = 0
    http_failures: int = 0
    circuit_openings: int = 0
    detail_not_modified: int = 0
    detail_deferred: int = 0
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    latency_max_ms: float = 0.0
    cache_hit_ratio: float = 0.0
    verified_coverage_ratio: float = 0.0
    http_status_counts: dict[str, int] = field(default_factory=dict)
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SourceRunResult:
    run_id: str
    source_id: str
    status: SourceStatus
    metrics: SourceMetrics
    error: str = ""
    changes: list[dict[str, Any]] = field(default_factory=list)


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
