"""Evidence-first classification and comparable cost scenarios."""

from __future__ import annotations

from datetime import date
from math import asin, cos, radians, sin, sqrt

from .config import RankingConfig, SearchProfile
from .models import Classification, CostScenario, HousingRecord


def cost_scenario(record: HousingRecord, months: int) -> CostScenario:
    unknown = list(record.unknown_costs)
    if record.recurring_total_yen is None:
        unknown.append("recurring total")
        return CostScenario(months, None, None, False, tuple(dict.fromkeys(unknown)))
    total = record.recurring_total_yen * months
    if record.initial_cost_yen is None:
        unknown.append("initial cost")
    else:
        total += record.initial_cost_yen
    if record.exit_cost_yen is None:
        unknown.append("exit cost")
    else:
        total += record.exit_cost_yen
    unknown = list(dict.fromkeys(unknown))
    return CostScenario(months, total, round(total / months), not unknown, tuple(unknown))


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


def _priority(
    record: HousingRecord,
    profile: SearchProfile,
    ranking: RankingConfig,
) -> tuple[float, float, float, float | None, int | None]:
    ranking_price = record.recurring_total_yen
    price_ratio = ranking_price / profile.monthly_budget_yen if ranking_price is not None else 2.0
    distance = None
    if record.latitude is not None and record.longitude is not None:
        distance = straight_line_distance_km(
            record.latitude,
            record.longitude,
            ranking.anchor_latitude,
            ranking.anchor_longitude,
        )
    scored_distance = distance if distance is not None else ranking.missing_distance_penalty_km
    price_component = 100 * ranking.price_weight * price_ratio
    distance_component = (
        100 * ranking.distance_weight * scored_distance / ranking.distance_reference_km
    )
    return (
        round(price_component + distance_component, 2),
        round(price_component, 2),
        round(distance_component, 2),
        round(distance, 2) if distance is not None else None,
        ranking_price,
    )


def classify(
    record: HousingRecord,
    profile: SearchProfile,
    ranking: RankingConfig,
) -> Classification:
    reasons: list[str] = []
    cautions: list[str] = []
    excluded: list[str] = []
    is_oakhouse = record.source_id == "oakhouse"

    if record.prefecture and record.prefecture not in profile.target_prefectures:
        excluded.append(f"outside target prefectures: {record.prefecture}")
    elif record.prefecture:
        reasons.append(f"target prefecture: {record.prefecture}")
    else:
        cautions.append("prefecture not verified")

    if record.housing_type not in profile.allowed_housing_types:
        if record.housing_type == "unknown":
            cautions.append("housing type not verified")
        else:
            excluded.append(f"unsupported housing type: {record.housing_type}")
    if profile.private_space_required:
        if record.private_space is False:
            excluded.append("no private sleeping room")
        elif record.private_space is None:
            cautions.append("private room not verified")
        else:
            reasons.append("private sleeping space")

    if "female_only" in record.occupancy_restrictions and profile.applicant_gender == "male":
        excluded.append("female-only room")
    if "male_only" in record.occupancy_restrictions and profile.applicant_gender == "female":
        excluded.append("male-only room")

    if record.recurring_total_yen is None:
        cautions.append("mandatory recurring total is incomplete")
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
        elif record.price_validation in {"matched", "components_only"}:
            reasons.append("available-room rent and mandatory monthly fee verified")
        else:
            cautions.append(
                f"available-room price validation requires review: {record.price_validation}"
            )
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
        if is_oakhouse:
            excluded.append(message)
        else:
            cautions.append(message)
    elif record.availability_confidence in {"explicit", "available_now"}:
        reasons.append(f"availability evidence: {availability_evidence}")
    elif is_oakhouse:
        excluded.append("Oakhouse availability date is not verified for the landing window")
    else:
        cautions.append("move-in availability requires confirmation")

    if not record.foreigner_evidence:
        cautions.append("foreigner acceptance not verified")
    else:
        reasons.append("foreigner acceptance evidence present")
    if not record.overseas_application_evidence:
        cautions.append("overseas application path not verified")
    if profile.registration_required:
        if record.registration_confidence == "confirmed":
            reasons.append("address registration confirmed by provider evidence")
        elif record.registration_confidence == "probable":
            cautions.append("address registration is probable but must be confirmed")
        else:
            cautions.append("address registration must be confirmed before booking")

    priority_score, price_component, distance_component, distance, ranking_price = _priority(
        record, profile, ranking
    )
    if distance is None:
        cautions.append(
            f"distance to {ranking.anchor_label} unavailable; "
            f"ranking applies a {ranking.missing_distance_penalty_km:g} km penalty"
        )
    else:
        reasons.append(f"{distance:.2f} km straight-line to {ranking.anchor_label}")

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

    if excluded:
        name = "excluded"
        reasons.extend(excluded)
    elif cautions:
        name = "review"
    else:
        name = "eligible"
    deadline_penalty = 0
    if available:
        deadline_penalty = max(0, (available - profile.move_in_earliest).days)
    elif record.availability_confidence != "available_now":
        deadline_penalty = 9999
    scenario = landing_90 if track == "landing" else long_term
    rank_key = (
        0 if name == "eligible" else 1 if name == "review" else 2,
        0 if record.registration_confidence == "confirmed" else 1,
        0 if scenario.complete else 1,
        priority_score,
        deadline_penalty,
        record.walk_minutes_min or 999,
        record.stable_key,
    )
    return Classification(
        name,
        track,
        tuple(reasons),
        tuple(cautions),
        rank_key,
        priority_score,
        price_component,
        distance_component,
        distance,
        ranking_price,
        landing_30,
        landing_60,
        landing_90,
        long_term,
    )
