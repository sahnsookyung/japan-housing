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


def test_stale_records_are_not_pareto_candidates(app_config) -> None:
    stale = housing("stale", 50_000, 10)
    stale["freshness"] = "stale"
    current = housing("current", 70_000, 40)

    frontier, candidates, _ = pareto_frontier_keys([stale, current], app_config.ranking)

    assert candidates == 1
    assert frontier == {"current"}


def test_incomplete_cost_floor_cannot_dominate_pareto_frontier(app_config) -> None:
    incomplete = housing("unknown-contract-fees", 40_000, 10)
    incomplete["ranking_price_basis"] = "long_term_known_cost_floor_incomplete_total"
    complete = housing("fully-priced", 70_000, 40)
    complete["ranking_price_basis"] = "long_term_effective"

    frontier, candidates, _ = pareto_frontier_keys([incomplete, complete], app_config.ranking)

    assert candidates == 1
    assert frontier == {"fully-priced"}


def test_missing_or_invalid_metrics_fail_closed(app_config) -> None:
    missing = housing("missing", 70_000, 40)
    missing["priority_transit_component"] = None
    invalid = housing("invalid", 70_000, 40)
    invalid["ranking_price_yen"] = float("nan")

    frontier, candidates, cohorts = pareto_frontier_keys([missing, invalid], app_config.ranking)

    assert frontier == set()
    assert candidates == 0
    assert cohorts == {}


def test_each_route_anchor_gets_an_independent_frontier(app_config) -> None:
    anchors = {anchor.anchor_id: anchor for anchor in app_config.routing.anchors}
    cheap_kichijoji = housing("cheap-kichijoji", 60_000, 50)
    cheap_kichijoji.update(
        {
            "latitude": anchors["kichijoji"].latitude,
            "longitude": anchors["kichijoji"].longitude,
            "commute_routes": [
                {"anchor_id": "kichijoji", "transit_minutes": 8, "bicycle_minutes": 5},
                {"anchor_id": "shibuya", "transit_minutes": 55, "bicycle_minutes": 50},
                {"anchor_id": "shinjuku", "transit_minutes": 45, "bicycle_minutes": 42},
            ],
        }
    )
    close_shibuya = housing("close-shibuya", 70_000, 35)
    close_shibuya.update(
        {
            "latitude": anchors["shibuya"].latitude,
            "longitude": anchors["shibuya"].longitude,
            "commute_routes": [
                {"anchor_id": "kichijoji", "transit_minutes": 50, "bicycle_minutes": 48},
                {"anchor_id": "shibuya", "transit_minutes": 7, "bicycle_minutes": 4},
                {"anchor_id": "shinjuku", "transit_minutes": 16, "bicycle_minutes": 15},
            ],
        }
    )
    records = [cheap_kichijoji, close_shibuya]

    summary = annotate_pareto_frontier(
        records,
        app_config.ranking,
        app_config.routing,
    )

    assert summary.frontier_counts["kichijoji"] == 1
    assert summary.frontier_counts["shibuya"] == 2
    assert cheap_kichijoji["pareto_frontiers"]["kichijoji"] is True
    assert close_shibuya["pareto_frontiers"]["kichijoji"] is False
    assert cheap_kichijoji["pareto_frontiers"]["shibuya"] is True
    assert close_shibuya["pareto_frontiers"]["shibuya"] is True
    assert (
        cheap_kichijoji["pareto_location_scores"]["kichijoji"]
        < close_shibuya["pareto_location_scores"]["kichijoji"]
    )
    assert (
        cheap_kichijoji["pareto_location_score"]
        == cheap_kichijoji["pareto_location_scores"]["balanced"]
    )
