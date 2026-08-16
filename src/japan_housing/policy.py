"""Evidence-first classification and comparable cost scenarios."""

from __future__ import annotations

from datetime import date

from .config import SearchProfile
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


def classify(record: HousingRecord, profile: SearchProfile) -> Classification:
    reasons: list[str] = []
    cautions: list[str] = []
    excluded: list[str] = []

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

    available = _availability(record)
    if available and available > profile.move_in_latest:
        cautions.append(f"available after landing window: {available.isoformat()}")
    elif record.availability_confidence in {"explicit", "available_now"}:
        reasons.append(f"availability evidence: {record.available_text or record.available_from}")
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
        deadline_penalty = abs((available - profile.move_in_earliest).days)
    elif record.availability_confidence != "available_now":
        deadline_penalty = 9999
    scenario = landing_90 if track == "landing" else long_term
    rank_key = (
        0 if name == "eligible" else 1 if name == "review" else 2,
        0 if record.registration_confidence == "confirmed" else 1,
        0 if scenario.complete else 1,
        deadline_penalty,
        scenario.effective_monthly_yen or 10**9,
        record.walk_minutes_min or 999,
        record.stable_key,
    )
    return Classification(
        name,
        track,
        tuple(reasons),
        tuple(cautions),
        rank_key,
        landing_30,
        landing_60,
        landing_90,
        long_term,
    )
