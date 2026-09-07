from __future__ import annotations

import csv
import io
import json
from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from japan_housing.costs import add_months, cost_scenario, month_fraction
from japan_housing.decision import (
    decision_summary,
    default_preferences,
    project_listing,
    read_preferences,
    save_preferences,
    scenario_frontier,
    validate_preferences,
)
from japan_housing.decision_routes import fetch_room_routes
from japan_housing.decision_service import DecisionService
from japan_housing.decision_statistics import distribution, filtered_statistics
from japan_housing.evidence import (
    EvidenceStore,
    merge_route_history,
    validate_fields,
    validate_routes,
)
from japan_housing.models import HousingRecord, iso_now
from japan_housing.state import StateStore


@pytest.fixture
def room():
    return HousingRecord(
        1,
        "reviewed",
        "Operator",
        "101",
        "https://example.com/101",
        "Room 101",
        prefecture="Tokyo",
        city="Nakano",
        latitude=35.7,
        longitude=139.6,
        coordinate_basis="user_confirmed",
        housing_type="private_room",
        private_space=True,
        contract_type="monthly",
        rent_yen=40000,
        recurring_total_yen=50000,
        initial_cost_yen=20000,
        exit_cost_yen=5000,
        minimum_stay_months=1,
        billing_basis="anniversary",
        deposit_yen=0,
        refundable_deposit_yen=0,
        upfront_rent_months=1,
        availability_confidence="available_now",
        available_text="Available now",
        source_availability_status="vacancy",
        registration_confidence="confirmed",
        foreigner_evidence=("Accepts foreign residents",),
        overseas_application_evidence=("Applications from overseas accepted",),
        observed_at=iso_now(),
        detail_verified_at=iso_now(),
    )


@pytest.fixture
def config(app_config, tmp_path):
    return replace(
        app_config, runtime=replace(app_config.runtime, state_path=tmp_path / "housing.db")
    )


def item(record):
    return {
        **json.loads(json.dumps(record.to_dict())),
        "stable_key": record.stable_key,
        "freshness": "current",
        "decision_status": "new",
    }


def test_calendar_anniversary_and_leap_year_are_not_ninety_days(room):
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert month_fraction(date(2026, 9, 10), date(2026, 12, 10)) == 3
    scenario = cost_scenario(room, 3, move_in=date(2026, 9, 10))
    assert scenario.nights == 91
    assert scenario.known_total_yen == 175000
    assert scenario.contract_eligible is True


def test_deposit_expense_cash_and_refund_are_not_double_counted(room):
    room.initial_cost_yen = 50000
    room.deposit_yen = 20000
    room.refundable_deposit_yen = 10000
    s = cost_scenario(room, 2, move_in=date(2026, 9, 10))
    assert s.known_total_yen == 145000
    assert s.cash_before_keys_yen == 100000
    assert s.refundable_cash_yen == 10000
    assert s.cash_complete and s.complete
    room.deposit_in_initial_cost = False
    s = cost_scenario(room, 2)
    assert s.known_total_yen == 165000 and s.cash_before_keys_yen == 120000


def test_unknown_deposit_refund_is_a_bounded_floor_not_a_fake_quote(room):
    room.initial_cost_yen = 50000
    room.deposit_yen = 20000
    room.refundable_deposit_yen = None
    s = cost_scenario(room, 2)
    assert s.known_total_yen == 135000 and s.cost_upper_yen == 155000
    assert not s.complete
    room.exit_cost_yen = None
    assert cost_scenario(room, 2).cost_upper_yen is None


def test_unknown_payment_timing_and_missing_fees_keep_cash_incomplete(room):
    room.upfront_rent_months = None
    assert not cost_scenario(room, 2).cash_complete
    assert cost_scenario(room, 2).cash_before_keys_yen == 20000
    room.upfront_rent_months = 1
    room.unknown_costs = ("guarantee fee",)
    assert not cost_scenario(room, 2).cash_complete
    room.recurring_total_yen = None
    assert cost_scenario(room, 2).effective_monthly_yen is None


def test_campaign_calendar_boundary_and_no_double_penalty(room):
    room.rent_yen = 33000
    room.recurring_total_yen = 49500
    room.initial_cost_yen = 30000
    room.exit_cost_yen = 16500
    room.early_exit_before_months = 3
    room.early_exit_penalty_months = 1
    room.campaign = {
        "verified": True,
        "move_in_from": "2026-09-01",
        "move_in_until": "2026-09-30",
        "minimum_months": 3,
        "waived_initial_yen": 30000,
        "free_rent_months": 1,
    }
    s = cost_scenario(room, 3, move_in=date(2026, 9, 10))
    assert s.known_total_yen == 132000 and s.effective_monthly_yen == 44000
    assert s.discount_yen == 63000 and s.penalty_yen == 0
    assert s.penalty_free_exit_date == "2026-12-10"
    short = cost_scenario(room, 2, move_in=date(2026, 9, 10))
    assert (
        short.known_total_yen == 195000 and short.discount_yen == 0 and short.penalty_yen == 49500
    )
    before = cost_scenario(room, 3, move_in=date(2026, 9, 10), move_out=date(2026, 12, 9))
    assert before.discount_yen == 0 and before.penalty_yen == 49500
    assert cost_scenario(room, 3, move_in=date(2026, 10, 1)).discount_yen == 0
    room.campaign["verified"] = False
    assert cost_scenario(room, 3, move_in=date(2026, 9, 10)).discount_yen == 0


def test_contract_dates_notice_and_partial_month_uncertainty(room):
    room.minimum_stay_months = 3
    room.notice_days = 30
    room.billing_basis = "unknown"
    s = cost_scenario(
        room,
        2,
        move_in=date(2026, 9, 10),
        move_out=date(2026, 11, 9),
        notice_given=date(2026, 11, 1),
    )
    assert s.contract_eligible is False
    assert s.penalty_free_exit_date == ""
    assert s.notice_deadline == "2026-10-10"
    assert {"partial-month billing rule", "late-notice charges"} <= set(s.unknown_costs)
    room.available_from = "2026-10-01"
    assert cost_scenario(room, 3, move_in=date(2026, 9, 10)).contract_eligible is False


def test_zero_area_placeholder_is_unknown(room, config):
    room.room_area_m2 = 0
    preferences = default_preferences(config)
    preferences["minimum_room_m2"] = 5
    projected = project_listing(room.to_dict(), preferences, config)
    assert projected["room_area_m2"] is None


def test_preferences_persist_pins_with_remote_and_unknown_destinations(config):
    p = default_preferences(config)
    p["destinations"][0]["latitude"] = 35.8
    save_preferences(config, p)
    assert read_preferences(config) == validate_preferences(p)
    assert len(p["destinations"]) == 11
    assert any(d["kind"] == "remote" and d["latitude"] is None for d in p["destinations"])
    assert next(d for d in p["destinations"] if d["id"] == "lunaris")["enabled"] is False
    for invalid in (float("nan"), 91, True):
        bad = json.loads(json.dumps(p))
        bad["destinations"][0]["latitude"] = invalid
        with pytest.raises(ValueError):
            validate_preferences(bad)
    bad = json.loads(json.dumps(p))
    bad["destinations"].append(bad["destinations"][0])
    with pytest.raises(ValueError):
        validate_preferences(bad)


def with_route(record, prefs):
    d = prefs["destinations"][0]
    prefs["destinations"] = [d]
    d["verified"] = True
    record.metrics_evidence = {
        "routes": [
            {
                "destination_id": d["id"],
                "destination_latitude": d["latitude"],
                "destination_longitude": d["longitude"],
                "origin_latitude": record.latitude,
                "origin_longitude": record.longitude,
                "minutes": 30,
                "fare_yen": 300,
                "verified_at": date.today().isoformat(),
                "source_url": "https://example.com/route",
            }
        ]
    }


def test_pin_move_invalidates_routes_and_remote_does_not_count(room, config):
    p = default_preferences(config)
    with_route(room, p)
    p["transport_days_per_month"] = 12
    p["destinations"][0]["office_days_per_week"] = 3
    r = project_listing(item(room), p, config)
    assert r["route_metrics"]["complete"] and r["priority_score"] is not None
    assert r["route_metrics"]["monthly_transport_yen"] == 7200
    assert r["route_metrics"]["weekly_commute_hours"] == 3
    p["destinations"][0]["latitude"] += 0.001
    r = project_listing(item(room), p, config)
    assert not r["route_metrics"]["complete"] and r["priority_score"] is None
    p["destinations"][0]["kind"] = "remote"
    assert project_listing(item(room), p, config)["route_metrics"]["required"] == 0


def test_old_or_approximate_routes_never_complete_a_score(room, config):
    p = default_preferences(config)
    with_route(room, p)
    p["destinations"][0]["verified"] = False
    assert not project_listing(item(room), p, config)["route_metrics"]["complete"]
    p["destinations"][0]["verified"] = True
    room.metrics_evidence["routes"][0]["verified_at"] = (
        date.today() - timedelta(days=8)
    ).isoformat()
    assert project_listing(item(room), p, config)["route_metrics"]["observed"] == 0
    p["price_weight"] = 1
    assert project_listing(item(room), p, config)["priority_score"] is not None


def test_scenario_budget_stale_and_geographic_filters(room, config):
    p = default_preferences(config)
    p["effective_budget_yen"] = 60000
    assert project_listing(item(room), p, config)["scenario_status"] == "excluded"
    r = item(room)
    r["freshness"] = "stale"
    assert project_listing(r, p, config)["scenario_status"] == "stale"
    p["effective_budget_yen"] = 85000
    room.prefecture = "Osaka"
    assert project_listing(item(room), p, config)["scenario_status"] == "excluded"
    p["housing_region"] = "kansai"
    assert project_listing(item(room), p, config)["scenario_status"] == "eligible"


def test_reviewed_import_is_atomic_and_estimates_remain_separate(config, room):
    payload = {
        "record": json.loads(json.dumps(room.to_dict())),
        "kind": "estimate",
        "source_url": room.url,
    }
    with EvidenceStore(config.runtime.state_path) as store:
        invalid = {**payload, "kind": "bogus"}
        with pytest.raises(ValueError):
            store.import_record(invalid)
        assert not store.merge([])
        key = store.import_record(payload)
        imported = store.merge([])[0]
        assert imported["recurring_total_yen"] is None
        assert imported["estimated_fields"]["recurring_total_yen"] == 50000
        store.add(
            key,
            {"values": {"recurring_total_yen": 55000}, "kind": "confirmed", "source_url": room.url},
        )
        assert store.merge([])[0]["recurring_total_yen"] == 55000
    with StateStore(config.runtime.state_path) as store:
        store.set_decision(key, "shortlisted", "Review fees")
    with EvidenceStore(config.runtime.state_path) as store:
        assert store.merge([])[0]["decision_status"] == "shortlisted"


def test_evidence_rejects_invalid_types_nonfinite_and_future_routes(room, config):
    for fields in (
        {"metrics_evidence": None},
        {"desk": "yes"},
        {"rent_yen": True},
        {"latitude": float("nan")},
        {"deposit_yen": 1000, "refundable_deposit_yen": 2000},
        {"metrics_evidence": {"noise": float("nan")}},
    ):
        with pytest.raises(ValueError):
            validate_fields(fields)
    p = default_preferences(config)
    with_route(room, p)
    room.metrics_evidence["routes"][0]["verified_at"] = (
        date.today() + timedelta(days=1)
    ).isoformat()
    with pytest.raises(ValueError):
        validate_routes(room.metrics_evidence["routes"])


def test_table_export_comparison_use_same_scenario_and_filters(config, room):
    r = item(room)
    other = item(replace(room, external_id="102", title="Other room", initial_cost_yen=40000))
    report = {"run_id": "test", "generated_at": iso_now(), "housing": [r, other]}
    service = DecisionService(config, SimpleNamespace(load=lambda: report))
    params = {"query": ["Room 101"], "status": ["all"], "limit": ["1"]}
    page = service.query(params)
    assert page["total"] == 1 and len(page["records"]) == 1
    exported = json.loads(service.export(params, "json")[0])
    assert [v["stable_key"] for v in exported["records"]] == [r["stable_key"]]
    assert exported["records"][0]["scenario"] == json.loads(
        json.dumps(service.detail(r["stable_key"])["scenario"])
    )
    assert len(exported["records"][0]["exit_scenarios"]) == 3
    assert service.by_id[r["stable_key"]]["exit_scenarios"] == []
    assert "destinations" not in page["records"][0]["route_metrics"]
    comparison = service.compare([r["stable_key"], other["stable_key"]])
    assert comparison["cost_series"][0]["values"][1]["total_yen"] == 125000
    assert comparison["records"][0]["scenario"]["known_total_yen"] == 125000
    csv_rows = list(csv.reader(io.StringIO(service.export(params, "csv")[0].decode("utf-8-sig"))))
    assert len(csv_rows) == 2 and csv_rows[1][4] == "125000"
    with pytest.raises(ValueError):
        service.query({"limit": ["201"]})
    with pytest.raises(ValueError):
        service.compare([r["stable_key"]] * 2)


def test_explicit_sort_uses_metric_across_evidence_tiers_and_missing_last(config, room):
    cheaper = replace(room, external_id="cheap", recurring_total_yen=20000, exit_cost_yen=None)
    unknown = replace(room, external_id="unknown", recurring_total_yen=None)
    report = {
        "run_id": "sort",
        "generated_at": iso_now(),
        "housing": [item(room), item(unknown), item(cheaper)],
    }
    service = DecisionService(config, SimpleNamespace(load=lambda: report))
    rows = service.query({"sort": ["price"]})["records"]
    assert [r["stable_key"] for r in rows] == [
        cheaper.stable_key,
        room.stable_key,
        unknown.stable_key,
    ]
    assert service.query({})["records"][0]["stable_key"] == room.stable_key
    page = service.query({"offset": ["500"]})
    assert page["offset"] == 0 and len(page["records"]) == 3
    assert page["map_room_count"] == 3 and page["map_total"] == 1
    with pytest.raises(ValueError, match="decision filter"):
        service.query({"decision": ["invented"]})


def test_statistics_keep_unknowns_and_quote_floors_separate(config, room):
    prefs = default_preferences(config)
    complete = project_listing(item(room), prefs, config)
    floor = project_listing(
        item(replace(room, external_id="floor", exit_cost_yen=None)), prefs, config
    )
    missing = project_listing(
        item(replace(room, external_id="missing", recurring_total_yen=None)), prefs, config
    )
    dismissed = {**complete, "decision_status": "not_interested"}
    stats = filtered_statistics([complete, floor, missing, dismissed])
    assert stats["filtered"] == 4 and stats["potential"] == 3
    summary = decision_summary([complete, floor, missing, dismissed])
    assert summary["potential"] == 3 and summary["cost_complete"] == 1
    assert stats["complete_monthly"]["n"] == 1
    assert stats["complete_monthly"]["median"] == complete["ranking_price_yen"]
    assert stats["known_minimum_monthly"]["n"] == 1
    assert stats["known_minimum_monthly"]["missing"] == 1
    assert stats["area_m2"]["median"] is None
    assert (
        stats["common_cost_gaps"]
        == filtered_statistics([missing, floor, complete])["common_cost_gaps"]
    )
    assert distribution([None, float("nan"), float("inf"), True, 0, 10]) == {
        "n": 2,
        "missing": 4,
        "min": 0,
        "median": 5,
        "max": 10,
    }
    assert filtered_statistics([])["cash_yen"]["median"] is None


def test_fresh_routes_use_cache_without_provider_credentials(room, config, monkeypatch):
    p = default_preferences(config)
    with_route(room, p)
    monkeypatch.setattr(
        "japan_housing.decision_routes.resolve_routes_api_key",
        lambda *a, **k: pytest.fail("cache miss"),
    )
    assert len(fetch_room_routes(item(room), p, config)) == 1


def test_undo_reuses_original_route_after_new_pin_route_was_saved(room, config, monkeypatch):
    p = default_preferences(config)
    with_route(room, p)
    original = json.loads(json.dumps(p))
    p["destinations"][0]["latitude"] += 0.001
    p["destinations"][0]["verified"] = False
    save_preferences(config, p)
    report = {"run_id": "pin-history", "generated_at": iso_now(), "housing": [item(room)]}
    service = DecisionService(config, SimpleNamespace(load=lambda: report))
    moved_route = {
        **room.metrics_evidence["routes"][0],
        "destination_latitude": p["destinations"][0]["latitude"],
        "minutes": 40,
    }
    monkeypatch.setattr(
        "japan_housing.decision_service.fetch_room_routes", lambda *args: [moved_route]
    )
    service.fetch_routes(room.stable_key)
    save_preferences(config, original)
    restored = service.detail(room.stable_key)
    assert len(restored["metrics_evidence"]["routes"]) == 2
    assert restored["route_metrics"]["complete"]
    assert restored["route_metrics"]["mean_minutes"] == 30
    monkeypatch.setattr(
        "japan_housing.decision_routes.resolve_routes_api_key",
        lambda *a, **k: pytest.fail("undo must reuse original cached route"),
    )
    assert fetch_room_routes(restored, original, config)[0]["minutes"] == 30


def test_route_history_is_bounded_and_current_schedule_is_required(room, config):
    from japan_housing.routing import transit_schedule_key

    p = default_preferences(config)
    with_route(room, p)
    route = room.metrics_evidence["routes"][0]
    history = merge_route_history(
        [{**route, "destination_latitude": i} for i in range(80)], [route]
    )
    assert len(history) == 60 and history[0] == route
    route["schedule_key"] = transit_schedule_key(config.routing)
    changed = replace(config, routing=replace(config.routing, departure_time="09:30"))
    assert not project_listing(item(room), p, changed)["route_metrics"]["complete"]
    with pytest.raises(ValueError, match="schedule key"):
        validate_routes([{**route, "schedule_key": {"invalid": "object"}}])


def test_geographic_frontier_uses_weighted_haversine_without_inventing_transit(room, config):
    from japan_housing.policy import straight_line_distance_km

    p = default_preferences(config)
    first = {**p["destinations"][0], "latitude": 35.7, "longitude": 139.7}
    second = {**first, "id": "other", "longitude": 139.8, "weight": 3}
    p["destinations"] = [first, second]
    r = project_listing(item(room), p, config)
    expected = (
        straight_line_distance_km(35.7, 139.6, 35.7, 139.7)
        + 3 * straight_line_distance_km(35.7, 139.6, 35.7, 139.8)
    ) / 4
    assert r["geographic_metrics"]["mean_km"] == pytest.approx(expected)
    assert r["route_metrics"]["mean_minutes"] is None and r["priority_score"] is None
    dominated = {**r, "stable_key": "expensive", "ranking_price_yen": r["ranking_price_yen"] + 1}
    tie = {**r, "stable_key": "equal"}
    assert scenario_frontier([dominated, r, tie], "distance") == {r["stable_key"], "equal"}
    assert scenario_frontier([r]) == set()
    p["destinations"][1]["latitude"] = None
    assert not project_listing(item(room), p, config)["geographic_metrics"]["complete"]


def test_chart_plots_provisional_matches_beyond_the_table_page_without_promoting_them(room, config):
    pending = [
        item(
            replace(
                room, external_id=f"pending-{i}", title=f"Pending room {i}", initial_cost_yen=None
            )
        )
        for i in range(12)
    ]
    report = {
        "run_id": "all-potential",
        "generated_at": iso_now(),
        "housing": [item(room), *pending],
    }
    service = DecisionService(config, SimpleNamespace(load=lambda: report))
    page = service.query({"metric": ["distance"], "limit": ["1"]})
    assert len(page["records"]) == 1 and len(page["points"]) == 13
    assert page["frontier_coverage"]["provisional"] == 12
    assert page["frontier_coverage"]["complete_cases"] == 1
    assert page["frontier_ids"] == [room.stable_key]
    floors = [point for point in page["points"] if point["provisional"]]
    assert all(point["price_is_floor"] and not point["frontier"] for point in floors)
    assert all("initial cost" in point["unknown_costs"] for point in floors)
    # Full filters, not table pagination, determine the plot.
    filtered = service.query({"metric": ["distance"], "query": ["Pending room 3"]})
    assert [point["id"] for point in filtered["points"]] == [pending[3]["stable_key"]]


def test_chart_accounts_for_missing_values_and_keeps_hard_failures_out(room, config):
    missing_cost = item(replace(room, external_id="no-cost", recurring_total_yen=None))
    missing_pin = item(replace(room, external_id="no-pin", latitude=None, longitude=None))
    excluded = item(replace(room, external_id="over-budget", recurring_total_yen=100000))
    stale = {**item(replace(room, external_id="old")), "freshness": "stale"}
    reserved = {**item(replace(room, external_id="booked")), "decision_status": "reserved"}
    report = {
        "run_id": "unplotted",
        "generated_at": iso_now(),
        "housing": [item(room), missing_cost, missing_pin, excluded, stale, reserved],
    }
    service = DecisionService(config, SimpleNamespace(load=lambda: report))
    page = service.query({"metric": ["distance"], "status": ["all"]})
    coverage = page["frontier_coverage"]
    assert coverage["potential"] == 3 and coverage["outside_chart_scope"] == 3
    assert coverage["plottable"] == 1 and coverage["unplotted"] == 2
    reasons = {r["id"]: r["reasons"] for r in page["unplotted"]}
    assert "Stay expense is unknown" in reasons[missing_cost["stable_key"]]
    assert "Room or enabled office coordinates are missing" in reasons[missing_pin["stable_key"]]
    transit = service.query({"metric": ["transit"]})
    assert transit["total"] == 3
    assert (
        service.query({"decision": ["reserved"]})["records"][0]["stable_key"]
        == reserved["stable_key"]
    )
    assert not transit["points"] and transit["frontier_coverage"]["unplotted"] == 3
    assert all(
        any("Fresh routes" in reason for reason in r["reasons"]) for r in transit["unplotted"]
    )


def test_complete_observations_at_unconfirmed_pins_are_plotted_as_provisional(room, config):
    p = default_preferences(config)
    with_route(room, p)
    p["destinations"][0]["verified"] = False
    save_preferences(config, p)
    report = {"run_id": "unconfirmed-pin", "generated_at": iso_now(), "housing": [item(room)]}
    service = DecisionService(config, SimpleNamespace(load=lambda: report))
    page = service.query({"metric": ["transit"]})
    assert len(page["points"]) == 1 and page["points"][0]["provisional"]
    assert not page["points"][0]["price_is_floor"] and not page["frontier_ids"]


def test_http_preferences_evidence_import_and_error_boundaries(
    config, room, monkeypatch, project_root
):
    from http.server import ThreadingHTTPServer
    from threading import Thread

    import httpx

    from japan_housing.dashboard import _handler, _SnapshotCache

    report = {"run_id": "api-test", "generated_at": iso_now(), "housing": [item(room)]}
    monkeypatch.setattr(_SnapshotCache, "load", lambda self: report)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(config, project_root / "web"))
    worker = Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    worker.start()
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False
        ) as client:
            headers = {"X-Japan-Housing": "1"}
            assert (
                client.post("/api/preferences", json=default_preferences(config)).status_code == 403
            )
            p = client.get("/api/decision").json()["preferences"]
            p["destinations"][0]["latitude"] = 35.75
            assert client.post("/api/preferences", json=p, headers=headers).status_code == 200
            assert (
                client.get("/api/decision").json()["preferences"]["destinations"][0]["latitude"]
                == 35.75
            )
            assert client.post("/api/import", json=[], headers=headers).status_code == 400
            partial = {
                "stable_key": room.stable_key,
                "source_url": room.url,
                "kind": "confirmed",
                "values": {"refundable_deposit_yen": 10000},
            }
            # Existing deposit is zero; validating only the patch would miss the contradiction.
            assert client.post("/api/evidence", json=partial, headers=headers).status_code == 400
            partial["values"] = {"desk": True}
            assert client.post("/api/evidence", json=partial, headers=headers).status_code == 200
            assert client.get("/api/listing", params={"id": room.stable_key}).json()["desk"] is True
            partial.update(kind="estimate", values={"desk": False})
            assert client.post("/api/evidence", json=partial, headers=headers).status_code == 200
            detail = client.get("/api/listing", params={"id": room.stable_key}).json()
            assert detail["desk"] is True and detail["estimated_fields"]["desk"] is False
            for path in (
                "/decision-app.mjs",
                "/decision-map.mjs",
                "/pin-history.mjs",
                "/decision-charts.mjs",
                "/vendor/leaflet.js",
            ):
                result = client.get(path)
                assert result.status_code == 200 and "javascript" in result.headers["content-type"]
            assert "script-src 'self'" in result.headers["content-security-policy"]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_route_request_uses_saved_pins_and_only_transit(room, config, monkeypatch):
    from japan_housing.models import iso_now
    from japan_housing.routing import RouteMatrixValue

    p = default_preferences(config)
    p["destinations"] = [
        {**p["destinations"][0], "id": str(i), "latitude": 35.6 + i * 0.001} for i in range(20)
    ]
    monkeypatch.setattr(
        "japan_housing.decision_routes.resolve_routes_api_key", lambda *a, **k: "test-key"
    )

    class Client:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def compute_matrix(self, mode, origins, anchors, departure, schedule):
            assert mode == "TRANSIT" and len(origins) == 1 and len(anchors) == 20
            assert anchors[0].latitude == p["destinations"][0]["latitude"]
            origin = origins[0]
            return [
                RouteMatrixValue(
                    origin.key,
                    origin.latitude,
                    origin.longitude,
                    "cache-key",
                    a.anchor_id,
                    a.label,
                    mode,
                    1800,
                    5000,
                    "ROUTE_EXISTS",
                    iso_now(),
                    departure.isoformat(),
                    schedule,
                )
                for a in anchors
            ]

    monkeypatch.setattr("japan_housing.decision_routes.GoogleRoutesClient", Client)
    routes = fetch_room_routes(item(room), p, config)
    assert len(routes) == 20 and all(r["minutes"] == 30 for r in routes)
    assert routes[0]["destination_latitude"] == p["destinations"][0]["latitude"]
    assert "fare_yen" not in routes[0]  # A matrix duration is not a fare quote.
