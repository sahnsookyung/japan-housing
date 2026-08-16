"""Typed records shared by source adapters, policy, state, and reports."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal

ClassificationName = Literal["eligible", "review", "excluded"]
SourceStatus = Literal["healthy", "partial", "failed", "drifted"]
HOUSING_RECORD_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ListingRef:
    source_id: str
    external_id: str
    url: str
    list_fingerprint: str
    title_hint: str = ""
    last_modified: str | None = None


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
    nearest_stations: tuple[str, ...] = ()
    walk_minutes_min: int | None = None
    housing_type: str = "unknown"
    private_space: bool | None = None
    furnished: bool | None = None
    rent_yen: int | None = None
    management_fee_yen: int | None = None
    mandatory_monthly_fees_yen: int | None = None
    recurring_total_yen: int | None = None
    initial_cost_yen: int | None = None
    exit_cost_yen: int | None = None
    unknown_costs: tuple[str, ...] = ()
    available_from: str | None = None
    available_text: str = ""
    availability_confidence: str = "unknown"
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
class Classification:
    name: ClassificationName
    track: str
    reasons: tuple[str, ...]
    cautions: tuple[str, ...]
    rank_key: tuple[Any, ...]
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
    explicit_closed: int = 0
    requests: int = 0
    bytes_downloaded: int = 0
    retries: int = 0
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
