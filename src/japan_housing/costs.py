"""Calendar-aware stay expenses, liquidity and conditional contract terms.

Move-out is exclusive. Unknown amounts never acquire an invented upper bound.
Deposits belong to cash flow; only their known forfeited portion is expense.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from fractions import Fraction
from functools import lru_cache

from .models import CostScenario, HousingRecord


@lru_cache(maxsize=4096)
def add_months(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    return date(year, month + 1, min(day.day, calendar.monthrange(year, month + 1)[1]))


@lru_cache(maxsize=1024)
def month_fraction(start: date, end: date, basis: str = "anniversary") -> Fraction:
    """Prorate using actual calendar days, or successive move-in anniversaries."""
    result = Fraction(0)
    current = start
    index = 0
    while current < end:
        if basis == "calendar_daily":
            boundary = add_months(current.replace(day=1), 1)
            denominator = calendar.monthrange(current.year, current.month)[1]
        else:
            boundary = add_months(start, index + 1)
            denominator = (boundary - current).days
        stop = min(end, boundary)
        result += Fraction((stop - current).days, denominator)
        current = stop
        index += 1
    return result


def cost_scenario(
    record: HousingRecord,
    months: int,
    *,
    move_in: date | None = None,
    move_out: date | None = None,
    notice_given: date | None = None,
) -> CostScenario:
    if not 1 <= months <= 36:
        raise ValueError("stay must be between one and 36 months")
    start = move_in or date(2000, 1, 1)
    end = move_out or add_months(start, months)
    if end <= start or end > add_months(start, 36):
        raise ValueError("move-out must follow move-in by at most 36 months")
    unknown = list(record.unknown_costs)
    reasons: list[str] = []
    ledger: list[dict[str, object]] = []
    eligible: bool | None = True
    minimum = (
        add_months(start, record.minimum_stay_months)
        if record.minimum_stay_months is not None
        else start + timedelta(days=record.minimum_stay_days)
        if record.minimum_stay_days is not None
        else None
    )
    if minimum is None:
        eligible = None
        reasons.append("minimum stay requires confirmation")
    elif end < minimum:
        eligible = False
        reasons.append(f"minimum stay ends {minimum.isoformat()}")
    if record.maximum_stay_days is not None and (end - start).days > record.maximum_stay_days:
        eligible = False
        reasons.append("stay exceeds the maximum contract duration")
    if record.available_from:
        try:
            if date.fromisoformat(record.available_from) > start:
                eligible = False
                reasons.append(f"room is available from {record.available_from}")
        except ValueError:
            eligible = None if eligible is True else eligible
            reasons.append("availability date is invalid")

    billing = record.billing_basis
    factor = month_fraction(start, end, billing)
    normalizer = month_fraction(start, end)
    if billing == "unknown" and end != add_months(start, months):
        unknown.append("partial-month billing rule")
    recurring = record.recurring_total_yen
    if recurring is None:
        unknown.append("recurring total")
    monthly_total = round((recurring or 0) * factor)
    ledger.append(
        {
            "label": "Recurring charges",
            "amount_yen": monthly_total if recurring is not None else None,
            "kind": "expense",
        }
    )

    initial = record.initial_cost_yen
    if initial is None:
        unknown.append("initial cost")
    entry = initial or 0
    deposit = record.deposit_yen
    refund = record.refundable_deposit_yen
    refundable_unknown = deposit is not None and deposit > 0 and refund is None
    if deposit is not None:
        if record.deposit_in_initial_cost:
            entry -= deposit
        entry = max(0, entry)
        if refund is None and deposit:
            unknown.append("deposit refund conditions")
        else:
            entry += deposit - (refund or 0)
    elif refund is not None:
        unknown.append("deposit amount")
    ledger.append(
        {
            "label": "Non-refundable entry charges",
            "amount_yen": entry if initial is not None or entry else None,
            "kind": "expense",
        }
    )
    if deposit is not None:
        ledger.append(
            {
                "label": "Deposit paid (cash, not added again to expense)",
                "amount_yen": deposit,
                "kind": "cash",
            }
        )
    exit_fee = record.exit_cost_yen
    if exit_fee is None:
        unknown.append("exit cost")
    ledger.append(
        {"label": "Ordinary departure charges", "amount_yen": exit_fee, "kind": "expense"}
    )

    penalty = 0
    penalty_boundary = (
        add_months(start, record.early_exit_before_months)
        if record.early_exit_before_months
        else minimum
    )
    confirmed_penalty_boundary = (
        penalty_boundary
        if record.early_exit_before_months
        and (
            record.early_exit_penalty_yen is not None
            or record.early_exit_penalty_months is not None
        )
        else None
    )
    if penalty_boundary is not None and end < penalty_boundary:
        if record.early_exit_penalty_yen is None and record.early_exit_penalty_months is None:
            unknown.append("early-departure charges")
        else:
            penalty += record.early_exit_penalty_yen or 0
            if record.early_exit_penalty_months is not None:
                if recurring is None:
                    unknown.append("early-departure recurring charge")
                penalty += round((recurring or 0) * record.early_exit_penalty_months)
    deadline = end - timedelta(days=record.notice_days) if record.notice_days is not None else None
    if record.notice_days is None:
        reasons.append("move-out notice period requires confirmation")
    elif notice_given is not None and notice_given > deadline:
        unknown.append("late-notice charges")

    discount = 0
    campaign = record.campaign
    if campaign:
        if not campaign.get("verified") or move_in is None:
            reasons.append("campaign not applied: eligibility or dates unverified")
        else:
            valid_from = date.fromisoformat(campaign["move_in_from"])
            valid_until = date.fromisoformat(campaign["move_in_until"])
            campaign_end = add_months(start, int(campaign["minimum_months"]))
            if valid_from <= start <= valid_until:
                if campaign.get("early_exit_fixed_yen") or campaign.get(
                    "early_exit_recurring_months"
                ):
                    confirmed_penalty_boundary = max(
                        confirmed_penalty_boundary or start, campaign_end
                    )
                if campaign_end > (minimum or start):
                    minimum = campaign_end
                if end >= campaign_end:
                    waived_entry = min(entry, int(campaign.get("waived_initial_yen", 0)))
                    free_rent = min(factor, Fraction(str(campaign.get("free_rent_months", 0))))
                    if record.rent_yen is None and free_rent:
                        unknown.append("base rent for campaign discount")
                    discount = waived_entry + round((record.rent_yen or 0) * free_rent)
                    reasons.append("campaign applies; maintenance remains payable")
                else:
                    reasons.append("campaign canceled for early departure; no discount applied")
                    penalty += int(campaign.get("early_exit_fixed_yen", 0))
                    penalty += round(
                        (recurring or 0) * float(campaign.get("early_exit_recurring_months", 0))
                    )
    if discount:
        ledger.append(
            {"label": "Qualified campaign discount", "amount_yen": -discount, "kind": "expense"}
        )
    if penalty:
        ledger.append(
            {"label": "Early-departure penalty", "amount_yen": penalty, "kind": "expense"}
        )
    total = monthly_total + entry + (exit_fee or 0) + penalty - discount
    unknown = list(dict.fromkeys(unknown))
    if recurring is None:
        known_total = None
        effective = None
    else:
        known_total = total
        effective = round(Fraction(total) / normalizer)
    cash_complete = (
        initial is not None
        and recurring is not None
        and record.upfront_rent_months is not None
        and (record.deposit_in_initial_cost or deposit is not None)
        and not record.unknown_costs
    )
    cash = None
    if initial is not None and recurring is not None:
        cash = initial + (0 if record.deposit_in_initial_cost else deposit or 0)
        if record.upfront_rent_months is not None:
            cash += recurring * record.upfront_rent_months
        # Payment timing of discounts must be explicit; expense savings do not imply liquidity.
        cash -= min(cash, int(campaign.get("upfront_discount_yen", 0))) if discount else 0
    upper = (
        total + (deposit or 0)
        if refundable_unknown and unknown == ["deposit refund conditions"]
        else total
        if not unknown
        else None
    )
    return CostScenario(
        months,
        known_total,
        effective,
        not unknown,
        tuple(unknown),
        start.isoformat() if move_in else "",
        end.isoformat() if move_in else "",
        (end - start).days,
        cash,
        cash_complete,
        refund,
        upper,
        eligible,
        tuple(reasons),
        confirmed_penalty_boundary.isoformat() if confirmed_penalty_boundary and move_in else "",
        deadline.isoformat() if deadline and move_in else "",
        discount,
        penalty,
        tuple(ledger),
    )
