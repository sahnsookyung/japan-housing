"""Pareto helpers for comparable housing price and location trade-offs."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from math import isfinite
from typing import Any

from .config import RankingConfig, RoutingConfig
from .policy import straight_line_distance_km

NON_ACTIONABLE_DECISIONS = frozenset({"reserved", "not_interested"})
BALANCED_PERSPECTIVE_ID = "balanced"


@dataclass(frozen=True, slots=True)
class ParetoSummary:
    candidate_count: int
    frontier_count: int
    cohort_counts: dict[str, int]
    candidate_counts: dict[str, int]
    frontier_counts: dict[str, int]
    perspective_cohort_counts: dict[str, dict[str, int]]


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


def _route_by_anchor(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    routes: dict[str, dict[str, Any]] = {}
    for route in record.get("commute_routes") or ():
        if not isinstance(route, dict):
            continue
        anchor_id = str(route.get("anchor_id") or "")
        if anchor_id:
            routes[anchor_id] = route
    return routes


def _anchor_location_score(
    record: dict[str, Any],
    ranking: RankingConfig,
    *,
    anchor_latitude: float,
    anchor_longitude: float,
    route: dict[str, Any] | None,
) -> tuple[float, float | None]:
    latitude = _number(record.get("latitude"))
    longitude = _number(record.get("longitude"))
    distance = (
        straight_line_distance_km(latitude, longitude, anchor_latitude, anchor_longitude)
        if latitude is not None and longitude is not None
        else None
    )
    scored_distance = distance if distance is not None else ranking.missing_distance_penalty_km
    transit = _number(route.get("transit_minutes")) if route else None
    bicycle = _number(route.get("bicycle_minutes")) if route else None
    transit_scored = transit if transit is not None else ranking.missing_transit_penalty_minutes
    bicycle_scored = bicycle if bicycle is not None else ranking.missing_bicycle_penalty_minutes
    location_weight = ranking.distance_weight + ranking.transit_weight + ranking.bicycle_weight
    score = (
        100
        * (
            ranking.distance_weight * scored_distance / ranking.distance_reference_km
            + ranking.transit_weight * transit_scored / ranking.transit_reference_minutes
            + ranking.bicycle_weight * bicycle_scored / ranking.bicycle_reference_minutes
        )
        / location_weight
    )
    return round(score, 2), round(distance, 2) if distance is not None else None


def pareto_perspective_metrics(
    record: dict[str, Any], ranking: RankingConfig, routing: RoutingConfig
) -> tuple[dict[str, float | None], dict[str, float | None]]:
    """Return comparable location burdens and distances for every commute perspective."""

    scores: dict[str, float | None] = {
        BALANCED_PERSPECTIVE_ID: pareto_location_score(record, ranking)
    }
    distances: dict[str, float | None] = {
        BALANCED_PERSPECTIVE_ID: _number(record.get("distance_to_anchor_km"))
    }
    routes = _route_by_anchor(record)
    for anchor in routing.anchors:
        score, distance = _anchor_location_score(
            record,
            ranking,
            anchor_latitude=anchor.latitude,
            anchor_longitude=anchor.longitude,
            route=routes.get(anchor.anchor_id),
        )
        scores[anchor.anchor_id] = score
        distances[anchor.anchor_id] = distance
    return scores, distances


def _perspective_location(
    record: dict[str, Any], ranking: RankingConfig, perspective_id: str
) -> float | None:
    scores = record.get("pareto_location_scores")
    if isinstance(scores, dict):
        score = _number(scores.get(perspective_id))
        if score is not None:
            return score
    if perspective_id == BALANCED_PERSPECTIVE_ID:
        return pareto_location_score(record, ranking)
    return None


def _candidate(
    record: dict[str, Any], ranking: RankingConfig, perspective_id: str
) -> tuple[str, float, float, str] | None:
    if record.get("classification") != "eligible":
        return None
    if str(record.get("freshness") or "current") != "current":
        return None
    if str(record.get("decision_status") or "new") in NON_ACTIONABLE_DECISIONS:
        return None
    stable_key = str(record.get("stable_key") or "")
    price = _number(record.get("ranking_price_yen"))
    location = _perspective_location(record, ranking, perspective_id)
    price_basis = str(record.get("ranking_price_basis") or "")
    if not stable_key or price is None or price <= 0 or location is None or location < 0:
        return None
    # A known-cost floor can rank below a fully priced home only because the
    # provider omitted mandatory entry/exit amounts. Keep it visible, but do
    # not let incomplete evidence dominate the Pareto frontier.
    if "incomplete" in price_basis:
        return None
    track = str(record.get("track") or "both")
    return track, price, location, stable_key


def is_pareto_candidate(
    record: dict[str, Any],
    ranking: RankingConfig,
    perspective_id: str = BALANCED_PERSPECTIVE_ID,
) -> bool:
    """Return whether a record is actionable and comparable in one perspective."""

    return _candidate(record, ranking, perspective_id) is not None


def pareto_frontier_keys(
    records: Iterable[dict[str, Any]],
    ranking: RankingConfig,
    perspective_id: str = BALANCED_PERSPECTIVE_ID,
) -> tuple[set[str], int, dict[str, int]]:
    """Find non-dominated housing choices in O(n log n) time.

    Price and normalized location burden are both minimized. Housing tracks are
    independent so a long-term lease cannot hide a viable short landing room.
    Equal price/location choices remain together on the frontier.
    """

    grouped: dict[str, list[tuple[float, float, str]]] = defaultdict(list)
    for record in records:
        candidate = _candidate(record, ranking, perspective_id)
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
    records: list[dict[str, Any]],
    ranking: RankingConfig,
    routing: RoutingConfig | None = None,
) -> ParetoSummary:
    perspective_ids = [BALANCED_PERSPECTIVE_ID]
    for record in records:
        if routing is None:
            score = pareto_location_score(record, ranking)
            record["pareto_location_scores"] = {BALANCED_PERSPECTIVE_ID: score}
            record["pareto_distances_km"] = {
                BALANCED_PERSPECTIVE_ID: _number(record.get("distance_to_anchor_km"))
            }
        else:
            scores, distances = pareto_perspective_metrics(record, ranking, routing)
            record["pareto_location_scores"] = scores
            record["pareto_distances_km"] = distances
    if routing is not None:
        perspective_ids.extend(anchor.anchor_id for anchor in routing.anchors)

    frontiers: dict[str, set[str]] = {}
    candidate_counts: dict[str, int] = {}
    frontier_counts: dict[str, int] = {}
    perspective_cohort_counts: dict[str, dict[str, int]] = {}
    for perspective_id in perspective_ids:
        frontier, candidate_count, cohort_counts = pareto_frontier_keys(
            records, ranking, perspective_id
        )
        frontiers[perspective_id] = frontier
        candidate_counts[perspective_id] = candidate_count
        frontier_counts[perspective_id] = len(frontier)
        perspective_cohort_counts[perspective_id] = cohort_counts

    balanced_frontier = frontiers[BALANCED_PERSPECTIVE_ID]
    for record in records:
        stable_key = str(record.get("stable_key") or "")
        candidates = {
            perspective_id: _candidate(record, ranking, perspective_id) is not None
            for perspective_id in perspective_ids
        }
        record["pareto_location_score"] = record["pareto_location_scores"][BALANCED_PERSPECTIVE_ID]
        record["pareto_candidates"] = candidates
        record["pareto_frontiers"] = {
            perspective_id: stable_key in frontiers[perspective_id]
            for perspective_id in perspective_ids
        }
        record["pareto_candidate"] = candidates[BALANCED_PERSPECTIVE_ID]
        record["pareto_frontier"] = stable_key in balanced_frontier

    return ParetoSummary(
        candidate_counts[BALANCED_PERSPECTIVE_ID],
        frontier_counts[BALANCED_PERSPECTIVE_ID],
        perspective_cohort_counts[BALANCED_PERSPECTIVE_ID],
        candidate_counts,
        frontier_counts,
        perspective_cohort_counts,
    )
