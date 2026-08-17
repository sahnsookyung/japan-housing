from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from japan_housing.config import AppConfig
from japan_housing.models import (
    HOUSING_RECORD_SCHEMA_VERSION,
    HousingRecord,
    ListingRef,
    SourceMetrics,
)
from japan_housing.policy import classify
from japan_housing.report import build_report_payload, load_latest, prune_reports, publish_report
from japan_housing.sources import SourceSpec
from japan_housing.state import StateStore


def _spec() -> SourceSpec:
    return SourceSpec(
        "fixture",
        "Fixture source",
        ("https://source.example/list",),
        frozenset({"source.example"}),
        (r"/detail/\d+",),
        authoritative_disappearance=True,
    )


def _record() -> HousingRecord:
    return HousingRecord(
        schema_version=HOUSING_RECORD_SCHEMA_VERSION,
        source_id="fixture",
        source_label="Fixture source",
        external_id="unit-1",
        url="https://source.example/detail/1",
        title="=formula-like title",
        prefecture="Tokyo",
        latitude=35.6580,
        longitude=139.7016,
        coordinate_basis="fixture",
        housing_type="1K",
        private_space=True,
        rent_yen=60_000,
        management_fee_yen=5_000,
        recurring_total_yen=65_000,
        initial_cost_yen=100_000,
        exit_cost_yen=0,
        available_from="2026-09-16",
        available_text="2026-09-16",
        availability_confidence="explicit",
        foreigner_evidence=("accepted",),
        overseas_application_evidence=("online",),
        registration_evidence=("confirmed",),
        registration_confidence="confirmed",
    )


def _promote(store: StateStore, config: AppConfig, run_id: str) -> None:
    record = _record()
    store.begin_source_run(run_id, "fixture")
    store.stage(
        run_id,
        "page-1",
        record,
        classify(record, config.profile, config.ranking),
    )
    store.promote(run_id, _spec(), complete=True)


def test_partial_snapshot_cannot_close_and_two_complete_misses_can(
    app_config: AppConfig, tmp_path: Path
) -> None:
    with StateStore(tmp_path / "state.sqlite3") as store:
        store.ensure_sources((_spec(),))
        _promote(store, app_config, "initial")
        assert len(store.active_records()) == 1

        store.begin_source_run("partial", "fixture")
        store.promote("partial", _spec(), complete=False)
        assert len(store.active_records()) == 1

        for run_id in ("miss-one", "miss-two"):
            store.begin_source_run(run_id, "fixture")
            store.promote(run_id, _spec(), complete=True)
        assert store.active_records() == []


def test_verified_oakhouse_page_retires_unavailable_room_immediately(
    app_config: AppConfig, tmp_path: Path
) -> None:
    with StateStore(tmp_path / "state.sqlite3") as store:
        store.ensure_sources((_spec(),))
        _promote(store, app_config, "initial")

        store.begin_source_run("verified-closed", "fixture")
        result = store.promote(
            "verified-closed",
            _spec(),
            complete=True,
            verified_page_ids={"page-1"},
        )

        assert result.closed == 1
        assert store.active_records() == []


def test_decisions_and_notifications_are_idempotent(app_config: AppConfig, tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.sqlite3") as store:
        store.ensure_sources((_spec(),))
        _promote(store, app_config, "initial")
        key = _record().stable_key
        store.set_decision(key, "shortlisted", "Ask about residence registration")
        assert store.active_records()[0]["decision_status"] == "shortlisted"
        assert store.reserve_notification(key, "fingerprint")
        assert not store.reserve_notification(key, "fingerprint")


def test_known_notification_failure_can_retry_with_a_bound(
    app_config: AppConfig, tmp_path: Path
) -> None:
    with StateStore(tmp_path / "state.sqlite3") as store:
        store.ensure_sources((_spec(),))
        _promote(store, app_config, "initial")
        key = _record().stable_key
        assert store.reserve_notification(key, "fingerprint", max_attempts=2)
        store.finish_notification(key, "fingerprint", "retryable_failed", error="HTTP 500")
        assert not store.reserve_notification(key, "fingerprint", max_attempts=2)
        with store.connection:
            store.connection.execute(
                "UPDATE notifications SET next_attempt_at=?",
                ((datetime.now().astimezone() - timedelta(minutes=1)).isoformat(),),
            )
        assert store.reserve_notification(key, "fingerprint", max_attempts=2)
        store.finish_notification(key, "fingerprint", "retryable_failed", error="HTTP 500")
        with store.connection:
            store.connection.execute(
                "UPDATE notifications SET next_attempt_at=?",
                ((datetime.now().astimezone() - timedelta(minutes=1)).isoformat(),),
            )
        assert not store.reserve_notification(key, "fingerprint", max_attempts=2)


def test_never_fetched_details_are_ordered_before_stale_cache(tmp_path: Path) -> None:
    first = ListingRef("fixture", "1", "https://source.example/detail/1", "one")
    second = ListingRef("fixture", "2", "https://source.example/detail/2", "two")
    with StateStore(tmp_path / "state.sqlite3") as store:
        store.cache_page(first, [_record()], [])
        cache = store.lookup_cache(first)
        ordered = store.order_detail_refs((first, second), {"1": cache, "2": None})
        store.record_detail_attempt(second, "failure")

        assert [item.external_id for item in ordered] == ["2", "1"]
        assert not store.detail_attempt_due(second)


def test_reports_publish_atomically_and_escape_csv_formulas(
    app_config: AppConfig, tmp_path: Path
) -> None:
    state_path = tmp_path / "state.sqlite3"
    with StateStore(state_path) as store:
        store.ensure_sources((_spec(),))
        _promote(store, app_config, "initial")
        payload = build_report_payload(store, "report-1", [], {"offline": True}, app_config)

    published = publish_report(tmp_path / "artifacts", payload)
    latest, latest_path = load_latest(tmp_path / "artifacts")

    assert latest["run_id"] == "report-1"
    assert latest_path == published.json_path
    assert published.markdown_path.exists()
    csv_text = published.csv_path.read_text(encoding="utf-8")
    assert "advertised_monthly_from_yen" in csv_text.splitlines()[0]
    assert "priority_score" in csv_text.splitlines()[0]
    assert csv_text.splitlines()[1].startswith("eligible")
    assert "'=formula-like title" in csv_text
    markdown = published.markdown_path.read_text(encoding="utf-8")
    assert "ranking mean 90.0 min including missing-route penalties" in markdown
    manifest = json.loads((published.directory / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["files"]) == {"housing.json", "housing.md", "housing.csv"}
    with pytest.raises(RuntimeError, match="already exists"):
        publish_report(tmp_path / "artifacts", payload)


def test_report_reclassifies_when_ranking_config_changes(
    app_config: AppConfig, tmp_path: Path
) -> None:
    with StateStore(tmp_path / "state.sqlite3") as store:
        store.ensure_sources((_spec(),))
        _promote(store, app_config, "initial")
        original = build_report_payload(store, "one", [], {"offline": True}, app_config)
        changed_ranking = replace(
            app_config.ranking,
            price_weight=0.40,
            distance_weight=0.15,
            transit_weight=0.30,
            bicycle_weight=0.15,
        )
        changed_config = replace(app_config, ranking=changed_ranking)
        updated = build_report_payload(store, "two", [], {"offline": True}, changed_config)

    assert updated["ranking"]["reclassified_records"] == 1
    assert (
        updated["housing"][0]["policy_config_hash"] != original["housing"][0]["policy_config_hash"]
    )
    assert updated["housing"][0]["priority_score"] != original["housing"][0]["priority_score"]


def test_report_sanitizes_invalid_coordinates_in_persisted_records(
    app_config: AppConfig, tmp_path: Path
) -> None:
    state_path = tmp_path / "state.sqlite3"
    with StateStore(state_path) as store:
        store.ensure_sources((_spec(),))
        record = replace(
            _record(),
            longitude=137.5549,
            coordinate_basis="provider_detail_structured_geo",
        )
        store.begin_source_run("initial", "fixture")
        store.stage(
            "initial",
            "page-1",
            record,
            classify(record, app_config.profile, app_config.ranking),
        )
        store.promote("initial", _spec(), complete=True)
        payload = build_report_payload(store, "report", [], {"offline": True}, app_config)

        persisted = json.loads(
            store.connection.execute(
                "SELECT record_json FROM records WHERE stable_key=?", (record.stable_key,)
            ).fetchone()[0]
        )

    assert payload["ranking"]["reclassified_records"] == 1
    assert payload["housing"][0]["latitude"] is None
    assert payload["housing"][0]["longitude"] is None
    assert payload["housing"][0]["coordinate_basis"].endswith("inconsistent_with_tokyo")
    assert payload["housing"][0]["distance_to_anchor_km"] is None
    assert persisted["longitude"] is None


def test_persisted_partial_source_keeps_offline_report_partial(
    app_config: AppConfig, tmp_path: Path
) -> None:
    with StateStore(tmp_path / "state.sqlite3") as store:
        store.ensure_sources((_spec(),))
        _promote(store, app_config, "initial")
        store.finish_source_run("initial", "fixture", "partial", SourceMetrics(discovered=2), "cap")
        payload = build_report_payload(store, "report", [], {"offline": True}, app_config)

    assert payload["status"] == "partial"
    assert payload["source_coverage"][0]["status"] == "partial"


def test_partial_source_uses_short_retry_interval(app_config: AppConfig, tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.sqlite3") as store:
        store.ensure_sources((_spec(),))
        store.begin_source_run("partial", "fixture")
        store.finish_source_run("partial", "fixture", "partial", SourceMetrics(), "cap")
        assert not store.source_due(_spec(), partial_retry_minutes=60)
        older = (datetime.now().astimezone() - timedelta(minutes=61)).isoformat()
        with store.connection:
            store.connection.execute(
                "UPDATE sources SET last_run_at=? WHERE source_id='fixture'", (older,)
            )
        assert store.source_due(_spec(), partial_retry_minutes=60)


def test_report_retention_only_removes_old_manifested_runs(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    old = runs / "old-run"
    current = runs / "current-run"
    unknown = runs / "unmanaged"
    for directory in (old, current, unknown):
        directory.mkdir(parents=True)
    (old / "manifest.json").write_text("{}", encoding="utf-8")
    (current / "manifest.json").write_text("{}", encoding="utf-8")
    old_timestamp = (datetime.now().astimezone() - timedelta(days=31)).timestamp()
    os.utime(old / "manifest.json", (old_timestamp, old_timestamp))

    removed = prune_reports(tmp_path, days=30, keep_run_id="current-run")

    assert removed == 1
    assert not old.exists()
    assert current.exists()
    assert unknown.exists()
