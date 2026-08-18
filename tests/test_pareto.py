from __future__ import annotations

from japan_housing.pareto import annotate_pareto_frontier, pareto_frontier_keys


def housing(
    stable_key: str,
    price: int,
    location: float,
    *,
    track: str = "landing",
    classification: str = "eligible",
    decision: str = "new",
) -> dict[str, object]:
    return {
        "stable_key": stable_key,
        "classification": classification,
        "decision_status": decision,
        "track": track,
        "ranking_price_yen": price,
        "priority_distance_component": location * 0.10,
        "priority_transit_component": location * 0.20,
        "priority_bicycle_component": location * 0.10,
    }


def test_frontier_keeps_real_price_location_tradeoffs(app_config) -> None:
    records = [
        housing("cheap-far", 60_000, 90),
        housing("balanced", 70_000, 60),
        housing("dominated", 75_000, 70),
        housing("close", 90_000, 30),
    ]

    frontier, candidates, cohorts = pareto_frontier_keys(records, app_config.ranking)

    assert frontier == {"cheap-far", "balanced", "close"}
    assert candidates == 4
    assert cohorts == {"landing": 4}


def test_equal_metrics_are_preserved_and_tracks_are_independent(app_config) -> None:
    records = [
        housing("landing-a", 70_000, 50),
        housing("landing-b", 70_000, 50),
        housing("long-term", 50_000, 20, track="long_term"),
    ]

    frontier, _, _ = pareto_frontier_keys(records, app_config.ranking)

    assert frontier == {"landing-a", "landing-b", "long-term"}


def test_non_actionable_and_unverified_records_are_not_candidates(app_config) -> None:
    records = [
        housing("review", 40_000, 10, classification="review"),
        housing("rejected", 45_000, 10, decision="not_interested"),
        housing("reserved", 50_000, 10, decision="reserved"),
        housing("choice", 70_000, 40),
    ]

    summary = annotate_pareto_frontier(records, app_config.ranking)

    assert summary.candidate_count == 1
    assert summary.frontier_count == 1
    assert [record["pareto_frontier"] for record in records] == [False, False, False, True]
    assert [record["pareto_candidate"] for record in records] == [False, False, False, True]


def test_missing_or_invalid_metrics_fail_closed(app_config) -> None:
    missing = housing("missing", 70_000, 40)
    missing["priority_transit_component"] = None
    invalid = housing("invalid", 70_000, 40)
    invalid["ranking_price_yen"] = float("nan")

    frontier, candidates, cohorts = pareto_frontier_keys([missing, invalid], app_config.ranking)

    assert frontier == set()
    assert candidates == 0
    assert cohorts == {}
