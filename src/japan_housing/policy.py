"""Evidence-first classification and comparable cost scenarios."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from math import asin, cos, radians, sin, sqrt

from .config import RankingConfig, RoutingConfig, SearchProfile
from .costs import cost_scenario
from .models import Classification, CommuteSummary, HousingRecord

POLICY_VERSION = 10
PROVIDER_UPDATE_GRACE_DAYS = 14


def _availability(record: HousingRecord) -> date | None:
    if not record.available_from:
        return None
    try:
        return date.fromisoformat(record.available_from)
    except ValueError:
        return None


def straight_line_distance_km(
    latitude: float,
    longitude: float,
    anchor_latitude: float,
    anchor_longitude: float,
) -> float:
    """Return great-circle distance; this is deliberately not a train-time estimate."""

    latitude_delta = radians(anchor_latitude - latitude)
    longitude_delta = radians(anchor_longitude - longitude)
    start_latitude = radians(latitude)
    end_latitude = radians(anchor_latitude)
    haversine = sin(latitude_delta / 2) ** 2 + (
        cos(start_latitude) * cos(end_latitude) * sin(longitude_delta / 2) ** 2
    )
    return 2 * 6371.0088 * asin(sqrt(min(1.0, haversine)))


def policy_config_hash(
    profile: SearchProfile,
    ranking: RankingConfig,
    routing: RoutingConfig | None = None,
) -> str:
    def canonical(value: object) -> object:
        if isinstance(value, dict):
            return {str(key): canonical(item) for key, item in sorted(value.items())}
        if isinstance(value, (set, frozenset)):
            return sorted(canonical(item) for item in value)
        if isinstance(value, (list, tuple)):
            return [canonical(item) for item in value]
        if isinstance(value, date):
            return value.isoformat()
        return value

    payload = canonical(
        {
            "profile": asdict(profile),
            "ranking": asdict(ranking),
            "routing": asdict(routing) if routing else None,
            "version": POLICY_VERSION,
        }
    )
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class _Priority:
    score: float
    price_component: float
    distance_component: float
    transit_component: float
    bicycle_component: float
    distance: float | None
    closer_score: float
    cheaper_score: float


def _variant_score(price_ratio: float, location_ratio: float, price_weight: float) -> float:
    return round(100 * (price_weight * price_ratio + (1 - price_weight) * location_ratio), 2)


def _priority(
    record: HousingRecord,
    ranking: RankingConfig,
    ranking_price: int | None,
    commute: CommuteSummary | None,
) -> _Priority:
    price_ratio = ranking_price / ranking.price_reference_yen if ranking_price is not None else 2.0
    distance = None
    if record.latitude is not None and record.longitude is not None:
        distance = straight_line_distance_km(
            record.latitude,
            record.longitude,
            ranking.anchor_latitude,
            ranking.anchor_longitude,
        )
    scored_distance = distance if distance is not None else ranking.missing_distance_penalty_km
    distance_ratio = scored_distance / ranking.distance_reference_km
    transit_scored = (
        commute.transit_minutes_scored
        if commute is not None
        else ranking.missing_transit_penalty_minutes
    )
    bicycle_scored = (
        commute.bicycle_minutes_scored
        if commute is not None
        else ranking.missing_bicycle_penalty_minutes
    )
    transit_ratio = transit_scored / ranking.transit_reference_minutes
    bicycle_ratio = bicycle_scored / ranking.bicycle_reference_minutes
    price_component = 100 * ranking.price_weight * price_ratio
    distance_component = 100 * ranking.distance_weight * distance_ratio
    transit_component = 100 * ranking.transit_weight * transit_ratio
    bicycle_component = 100 * ranking.bicycle_weight * bicycle_ratio
    location_weight = ranking.distance_weight + ranking.transit_weight + ranking.bicycle_weight
    location_ratio = (
        ranking.distance_weight * distance_ratio
        + ranking.transit_weight * transit_ratio
        + ranking.bicycle_weight * bicycle_ratio
    ) / location_weight
    return _Priority(
        round(
            price_component + distance_component + transit_component + bicycle_component,
            2,
        ),
        round(price_component, 2),
        round(distance_component, 2),
        round(transit_component, 2),
        round(bicycle_component, 2),
        round(distance, 2) if distance is not None else None,
        _variant_score(price_ratio, location_ratio, 0.40),
        _variant_score(price_ratio, location_ratio, 0.80),
    )


def classify(
    record: HousingRecord,
    profile: SearchProfile,
    ranking: RankingConfig,
    *,
    commute: CommuteSummary | None = None,
    routing: RoutingConfig | None = None,
) -> Classification:
    reasons: list[str] = []
    cautions: list[str] = []
    excluded: list[str] = []
    has_unresolved_requirement = False
    is_oakhouse = record.source_id == "oakhouse"

    if record.provider_next_update_at:
        try:
            provider_next_update = date.fromisoformat(record.provider_next_update_at)
        except ValueError:
            provider_next_update = None
        if (
            provider_next_update is not None
            and provider_next_update + timedelta(days=PROVIDER_UPDATE_GRACE_DAYS) < date.today()
        ):
            excluded.append(
                "provider listing freshness expired: next update was "
                f"{provider_next_update.isoformat()}"
            )

    if record.prefecture and record.prefecture not in profile.target_prefectures:
        excluded.append(f"outside target prefectures: {record.prefecture}")
    elif record.prefecture:
        reasons.append(f"target prefecture: {record.prefecture}")
    else:
        cautions.append("prefecture not verified")
        has_unresolved_requirement = True

    if record.housing_type not in profile.allowed_housing_types:
        if record.housing_type == "unknown":
            cautions.append("housing type not verified")
            has_unresolved_requirement = True
        else:
            excluded.append(f"unsupported housing type: {record.housing_type}")
    if profile.private_space_required:
        if record.private_space is False:
            excluded.append("no private sleeping room")
        elif record.private_space is None:
            cautions.append("private room not verified")
            has_unresolved_requirement = True
        else:
            reasons.append("private sleeping space")

    if "female_only" in record.occupancy_restrictions and profile.applicant_gender == "male":
        excluded.append("female-only room")
    if "male_only" in record.occupancy_restrictions and profile.applicant_gender == "female":
        excluded.append("male-only room")

    if record.recurring_total_yen is None:
        cautions.append("mandatory recurring total is incomplete")
        has_unresolved_requirement = True
    elif record.recurring_total_yen > profile.monthly_budget_yen:
        total = f"¥{record.recurring_total_yen:,}"
        budget = f"¥{profile.monthly_budget_yen:,}"
        excluded.append(f"recurring total {total} exceeds {budget}")
    else:
        reasons.append(f"recurring total ¥{record.recurring_total_yen:,} within budget")
    if record.unknown_costs:
        cautions.append(f"cost evidence incomplete: {', '.join(record.unknown_costs)}")
    if is_oakhouse:
        if record.price_basis != "available_room":
            cautions.append("Oakhouse price is not tied to an available room")
            has_unresolved_requirement = True
        elif record.price_validation in {"matched", "components_only"}:
            reasons.append("available-room rent and mandatory monthly fee verified")
        else:
            cautions.append(
                f"available-room price validation requires review: {record.price_validation}"
            )
            has_unresolved_requirement = True
        if (
            record.advertised_monthly_from_yen is not None
            and record.recurring_total_yen is not None
            and record.advertised_monthly_from_yen != record.recurring_total_yen
        ):
            difference = record.recurring_total_yen - record.advertised_monthly_from_yen
            reasons.append(
                f"available room is {difference:+,} yen versus advertised monthly from price"
            )

    available = _availability(record)
    availability_evidence = record.available_text or record.available_from
    if is_oakhouse and record.source_availability_status not in {"vacancy", "soon"}:
        excluded.append("Oakhouse room is not marked vacant or vacancy-soon")
    elif available and available > profile.move_in_latest:
        message = f"available after landing window: {available.isoformat()}"
        excluded.append(message)
    elif (
        record.source_availability_status == "occupied"
        and record.availability_confidence == "unknown"
    ):
        excluded.append("occupied with no verified move-out date")
    elif record.availability_confidence in {"explicit", "available_now"}:
        reasons.append(f"availability evidence: {availability_evidence}")
    elif is_oakhouse:
        excluded.append("Oakhouse availability date is not verified for the landing window")
    elif profile.strict_evidence:
        excluded.append("move-in date is not verified for the landing window")
    else:
        cautions.append("move-in availability requires confirmation")
        has_unresolved_requirement = True

    if not record.foreigner_evidence:
        cautions.append("foreigner acceptance not verified")
        has_unresolved_requirement = True
    else:
        reasons.append("foreigner acceptance evidence present")
    if not record.overseas_application_evidence:
        cautions.append("overseas application path not verified")
        has_unresolved_requirement = True
    if profile.registration_required:
        if record.registration_confidence == "confirmed":
            reasons.append("address registration confirmed by provider evidence")
        elif record.registration_confidence == "standard_residential":
            reasons.append("standard residential lease supports address registration")
        elif record.registration_confidence == "probable":
            cautions.append("address registration is probable but must be confirmed")
            has_unresolved_requirement = True
        else:
            cautions.append("address registration must be confirmed before booking")
            has_unresolved_requirement = True

    landing_30 = cost_scenario(record, 1)
    landing_60 = cost_scenario(record, 2)
    landing_90 = cost_scenario(record, 3)
    long_term = cost_scenario(record, profile.long_term_comparison_months)
    landing_contract = record.minimum_stay_days is None or record.minimum_stay_days <= 93
    if record.contract_type == "monthly" and landing_contract:
        track = "landing"
    elif record.contract_type == "standard":
        track = "long_term"
    else:
        track = "both"

    scenario = landing_90 if track == "landing" else long_term
    if scenario.complete:
        ranking_price = scenario.effective_monthly_yen
        ranking_price_basis = (
            "three_month_effective" if track == "landing" else "long_term_effective"
        )
    elif scenario.effective_monthly_yen is not None:
        ranking_price = scenario.effective_monthly_yen
        horizon = "three_month" if track == "landing" else "long_term"
        ranking_price_basis = f"{horizon}_known_cost_floor_incomplete_total"
    else:
        ranking_price = record.recurring_total_yen
        ranking_price_basis = "recurring_only_incomplete_total_cost"
    priority = _priority(record, ranking, ranking_price, commute)
    if priority.distance is None:
        cautions.append(
            f"distance to {ranking.anchor_label} unavailable; "
            f"ranking applies a {ranking.missing_distance_penalty_km:g} km penalty"
        )
    else:
        reasons.append(f"{priority.distance:.2f} km straight-line to {ranking.anchor_label}")
        if priority.distance > ranking.maximum_distance_km:
            excluded.append(
                f"{priority.distance:.2f} km exceeds maximum useful distance "
                f"{ranking.maximum_distance_km:g} km"
            )
    if commute and commute.transit_coverage == commute.anchor_count:
        reasons.append(
            f"mean public-transit commute {commute.transit_minutes_mean:g} minutes "
            f"across {commute.anchor_count} anchors"
        )
    if commute and commute.bicycle_coverage == commute.anchor_count:
        reasons.append(
            f"mean bicycle commute {commute.bicycle_minutes_mean:g} minutes "
            f"across {commute.anchor_count} anchors"
        )

    if excluded:
        name = "excluded"
        reasons.extend(excluded)
    elif has_unresolved_requirement:
        name = "unverified" if profile.strict_evidence else "review"
    else:
        name = "eligible"
    deadline_penalty = 0
    if available:
        deadline_penalty = max(0, (available - profile.move_in_earliest).days)
    elif record.availability_confidence != "available_now":
        deadline_penalty = 9999
    classification_rank = {"eligible": 0, "review": 1, "unverified": 2, "excluded": 3}
    rank_key = (
        classification_rank[name],
        0 if record.registration_confidence == "confirmed" else 1,
        0 if scenario.complete else 1,
        priority.score,
        deadline_penalty,
        record.walk_minutes_min or 999,
        record.stable_key,
    )
    return Classification(
        name=name,
        track=track,
        reasons=tuple(reasons),
        cautions=tuple(cautions),
        rank_key=rank_key,
        priority_score=priority.score,
        priority_price_component=priority.price_component,
        priority_distance_component=priority.distance_component,
        priority_transit_component=priority.transit_component,
        priority_bicycle_component=priority.bicycle_component,
        distance_to_anchor_km=priority.distance,
        transit_minutes_mean=commute.transit_minutes_mean if commute else None,
        bicycle_minutes_mean=commute.bicycle_minutes_mean if commute else None,
        transit_minutes_scored=(
            commute.transit_minutes_scored if commute else ranking.missing_transit_penalty_minutes
        ),
        bicycle_minutes_scored=(
            commute.bicycle_minutes_scored if commute else ranking.missing_bicycle_penalty_minutes
        ),
        transit_route_coverage=commute.transit_coverage if commute else 0,
        bicycle_route_coverage=commute.bicycle_coverage if commute else 0,
        commute_anchor_count=commute.anchor_count
        if commute
        else len(routing.anchors)
        if routing
        else 0,
        commute_routes=commute.anchors if commute else (),
        commute_data_hash=commute.data_hash if commute else "not_fetched",
        ranking_price_yen=ranking_price,
        ranking_price_basis=ranking_price_basis,
        priority_closer_score=priority.closer_score,
        priority_cheaper_score=priority.cheaper_score,
        policy_version=POLICY_VERSION,
        policy_config_hash=policy_config_hash(profile, ranking, routing),
        landing_30=landing_30,
        landing_60=landing_60,
        landing_90=landing_90,
        long_term=long_term,
    )
