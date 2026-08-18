"""Pareto helpers for comparable housing price and location trade-offs."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from math import isfinite
from typing import Any

from .config import RankingConfig

NON_ACTIONABLE_DECISIONS = frozenset({"reserved", "not_interested"})


@dataclass(frozen=True, slots=True)
class ParetoSummary:
    candidate_count: int
    frontier_count: int
    cohort_counts: dict[str, int]


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    numeric = float(value)
    return numeric if isfinite(numeric) else None


def pareto_location_score(record: dict[str, Any], ranking: RankingConfig) -> float | None:
    """Return normalized non-price burden from distance, transit, and bicycle components."""

    location_weight = ranking.distance_weight + ranking.transit_weight + ranking.bicycle_weight
    components = tuple(
        _number(record.get(name))
        for name in (
            "priority_distance_component",
            "priority_transit_component",
            "priority_bicycle_component",
        )
    )
    if location_weight <= 0 or any(value is None for value in components):
        return None
    return round(sum(value for value in components if value is not None) / location_weight, 2)


def _candidate(
    record: dict[str, Any], ranking: RankingConfig
) -> tuple[str, float, float, str] | None:
    if record.get("classification") != "eligible":
        return None
    if str(record.get("decision_status") or "new") in NON_ACTIONABLE_DECISIONS:
        return None
    stable_key = str(record.get("stable_key") or "")
    price = _number(record.get("ranking_price_yen"))
    location = pareto_location_score(record, ranking)
    if not stable_key or price is None or price <= 0 or location is None or location < 0:
        return None
    track = str(record.get("track") or "both")
    return track, price, location, stable_key


def pareto_frontier_keys(
    records: Iterable[dict[str, Any]], ranking: RankingConfig
) -> tuple[set[str], int, dict[str, int]]:
    """Find non-dominated housing choices in O(n log n) time.

    Price and normalized location burden are both minimized. Housing tracks are
    independent so a long-term lease cannot hide a viable short landing room.
    Equal price/location choices remain together on the frontier.
    """

    grouped: dict[str, list[tuple[float, float, str]]] = defaultdict(list)
    for record in records:
        candidate = _candidate(record, ranking)
        if candidate is None:
            continue
        track, price, location, stable_key = candidate
        grouped[track].append((price, location, stable_key))

    frontier: set[str] = set()
    cohort_counts: dict[str, int] = {}
    for track, candidates in grouped.items():
        candidates.sort(key=lambda item: (item[0], item[1], item[2]))
        cohort_counts[track] = len(candidates)
        best_location_at_lower_price = float("inf")
        index = 0
        while index < len(candidates):
            price = candidates[index][0]
            end = index + 1
            while end < len(candidates) and candidates[end][0] == price:
                end += 1
            price_group = candidates[index:end]
            group_best_location = price_group[0][1]
            if group_best_location < best_location_at_lower_price:
                frontier.update(
                    stable_key
                    for _price, location, stable_key in price_group
                    if location == group_best_location
                )
            best_location_at_lower_price = min(best_location_at_lower_price, group_best_location)
            index = end
    return frontier, sum(cohort_counts.values()), cohort_counts


def annotate_pareto_frontier(
    records: list[dict[str, Any]], ranking: RankingConfig
) -> ParetoSummary:
    frontier, candidate_count, cohort_counts = pareto_frontier_keys(records, ranking)
    for record in records:
        candidate = _candidate(record, ranking)
        record["pareto_location_score"] = pareto_location_score(record, ranking)
        record["pareto_candidate"] = candidate is not None
        record["pareto_frontier"] = str(record.get("stable_key") or "") in frontier
    return ParetoSummary(candidate_count, len(frontier), cohort_counts)
