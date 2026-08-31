from japan_housing.dashboard import dashboard_listing_page, host_header_is_loopback


def test_loopback_host_header_supports_ipv4_names_and_ipv6() -> None:
    assert host_header_is_loopback("127.0.0.1:8766")
    assert host_header_is_loopback("localhost:8766")
    assert host_header_is_loopback("[::1]:8766")


def test_loopback_host_header_rejects_malformed_or_remote_hosts() -> None:
    assert not host_header_is_loopback("example.com:8766")
    assert not host_header_is_loopback("localhost:not-a-port")
    assert not host_header_is_loopback("attacker@example.com")


def _listing(key: str, classification: str, price: int, location: float) -> dict[str, object]:
    return {
        "stable_key": key,
        "title": key,
        "url": f"https://example.invalid/{key}",
        "classification": classification,
        "freshness": "current",
        "decision_status": "new",
        "track": "landing",
        "recurring_total_yen": price,
        "ranking_price_yen": price,
        "priority_distance_component": location * 0.1,
        "priority_transit_component": location * 0.2,
        "priority_bicycle_component": location * 0.1,
        "pareto_location_scores": {"balanced": location},
        "rank_key": [0 if classification == "eligible" else 1, 0, 0, price, 0, 0, key],
        "raw_evidence_hash": "must-not-leak-into-dashboard-projection",
    }


def test_dashboard_listing_api_filters_recomputes_frontier_and_paginates(app_config) -> None:
    payload = {
        "housing": [
            _listing("cheap", "eligible", 50_000, 80),
            _listing("close", "eligible", 70_000, 20),
            _listing("review", "review", 55_000, 30),
            _listing("excluded", "excluded", 40_000, 10),
        ]
    }

    first = dashboard_listing_page(
        payload,
        {
            "classification": ["actionable"],
            "ranking": ["balanced"],
            "perspective": ["balanced"],
            "limit": ["1"],
            "offset": ["0"],
        },
        app_config,
    )
    second = dashboard_listing_page(
        payload,
        {
            "classification": ["actionable"],
            "ranking": ["balanced"],
            "perspective": ["balanced"],
            "limit": ["1"],
            "offset": ["1"],
        },
        app_config,
    )

    assert first["total"] == 3
    assert first["has_more"] is True
    assert first["frontier_ids"] == ["cheap", "close"]
    assert len(first["pareto_records"]) == 2
    assert set(first["pareto_records"][0]) <= {
        "city",
        "classification",
        "decision_status",
        "freshness",
        "pareto_location_scores",
        "prefecture",
        "ranking_price_yen",
        "stable_key",
        "title",
        "track",
        "url",
    }
    assert first["records"][0]["stable_key"] == "cheap"
    assert second["records"][0]["stable_key"] == "close"
    assert "raw_evidence_hash" not in first["records"][0]


def test_dashboard_listing_api_rejects_unbounded_page_size(app_config) -> None:
    try:
        dashboard_listing_page(
            {"housing": []},
            {"limit": ["1000"]},
            app_config,
        )
    except ValueError as exc:
        assert "limit must be between" in str(exc)
    else:
        raise AssertionError("unbounded dashboard page was accepted")


def test_dashboard_pareto_payload_excludes_non_actionable_decisions(app_config) -> None:
    reserved = _listing("reserved", "eligible", 40_000, 10)
    reserved["decision_status"] = "reserved"
    result = dashboard_listing_page(
        {"housing": [reserved, _listing("current", "eligible", 50_000, 20)]},
        {},
        app_config,
    )

    assert [item["stable_key"] for item in result["pareto_records"]] == ["current"]
    assert result["frontier_ids"] == ["current"]
