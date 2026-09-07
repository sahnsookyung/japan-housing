from __future__ import annotations

import json
import os
import sqlite3
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


def test_existing_records_schema_adds_freshness_before_its_index(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite3"
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            """
            CREATE TABLE records (
                stable_key TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                classification TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            )
            """
        )

    with StateStore(state_path) as store:
        columns = {row["name"] for row in store.connection.execute("PRAGMA table_info(records)")}
        indexes = {row["name"] for row in store.connection.execute("PRAGMA index_list(records)")}

    assert "freshness" in columns
    assert "records_freshness_revalidation" in indexes


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


def test_transactional_notification_outbox_preserves_overflow(
    app_config: AppConfig, tmp_path: Path
) -> None:
    records = [replace(_record(), external_id=f"unit-{index}") for index in range(25)]
    with StateStore(tmp_path / "state.sqlite3") as store:
        store.ensure_sources((_spec(),))
        store.begin_source_run("bulk", "fixture")
        store.stage_many(
            "bulk",
            (
                (
                    f"page-{index}",
                    record,
                    classify(record, app_config.profile, app_config.ranking),
                )
                for index, record in enumerate(records)
            ),
        )
        store.promote("bulk", _spec(), enqueue_changes=True)

        pending = store.pending_notifications()
        assert len(pending) == 25
        for item in pending[:20]:
            assert store.claim_notification(item["stable_key"], item["fingerprint"])
            store.finish_notification(item["stable_key"], item["fingerprint"], "sent")

        assert len(store.pending_notifications()) == 5


def test_unverified_records_are_not_routed_or_delivered(
    app_config: AppConfig, tmp_path: Path
) -> None:
    with StateStore(tmp_path / "state.sqlite3") as store:
        store.ensure_sources((_spec(),))
        store.begin_source_run("blocked", "fixture")
        record = replace(
            _record(),
            registration_evidence=(),
            registration_confidence="unknown",
        )
        outcome = classify(record, app_config.profile, app_config.ranking)
        assert outcome.name == "unverified"
        store.stage("blocked", "page-1", record, outcome)
        store.promote("blocked", _spec(), enqueue_changes=True)

        assert store.routing_candidates() == []
        assert store.pending_notifications() == []
        assert (
            store.enqueue_notification_changes(
                [
                    {
                        "type": "digest",
                        "stable_key": record.stable_key,
                        "fingerprint": record.fingerprint(),
                        "classification": "unverified",
                        "record": record.to_dict(),
                    }
                ]
            )
            == 0
        )


def test_queued_change_is_suppressed_if_record_becomes_unverified(
    app_config: AppConfig, tmp_path: Path
) -> None:
    with StateStore(tmp_path / "state.sqlite3") as store:
        store.ensure_sources((_spec(),))
        record = _record()
        store.begin_source_run("actionable", "fixture")
        store.stage(
            "actionable",
            "page-1",
            record,
            classify(record, app_config.profile, app_config.ranking),
        )
        store.promote("actionable", _spec(), enqueue_changes=True)
        assert len(store.pending_notifications()) == 1
        assert len(store.routing_candidates()) == 1

        with store.connection:
            store.connection.execute(
                "UPDATE records SET classification='unverified' WHERE stable_key=?",
                (record.stable_key,),
            )

        assert store.pending_notifications() == []
        assert store.routing_candidates() == []


def test_abandoned_notification_reservation_fails_closed(
    app_config: AppConfig, tmp_path: Path
) -> None:
    with StateStore(tmp_path / "state.sqlite3") as store:
        store.ensure_sources((_spec(),))
        _promote(store, app_config, "initial")
        key = _record().stable_key
        assert store.reserve_notification(key, "fingerprint")
        old = (datetime.now().astimezone() - timedelta(hours=1)).isoformat()
        with store.connection:
            store.connection.execute(
                "UPDATE notifications SET reserved_at=?",
                (old,),
            )

        assert store.recover_abandoned_notification_reservations(minutes=30) == 1
        assert store.unknown_notifications()[0]["last_error"] == "abandoned reservation"


def test_non_authoritative_records_become_stale_then_revalidate_or_close(
    app_config: AppConfig, tmp_path: Path
) -> None:
    spec = replace(
        _spec(),
        authoritative_disappearance=False,
        stale_after_hours=1,
        stale_revalidation_budget=1,
    )
    with StateStore(tmp_path / "state.sqlite3") as store:
        store.ensure_sources((spec,))
        _promote(store, app_config, "initial")
        old = (datetime.now().astimezone() - timedelta(hours=2)).isoformat()
        with store.connection:
            store.connection.execute("UPDATE records SET last_seen_at=?", (old,))
        assert store.apply_listing_freshness((spec,)) == {"fixture": 1}
        assert store.active_records()[0]["freshness"] == "stale"
        refs = store.stale_revalidation_refs(
            "fixture", exclude_page_ids=set(), limit=spec.stale_revalidation_budget
        )
        assert [ref.external_id for ref in refs] == ["page-1"]

        store.begin_source_run("fresh", "fixture")
        record = _record()
        store.stage(
            "fresh",
            "page-1",
            record,
            classify(record, app_config.profile, app_config.ranking),
        )
        store.promote("fresh", spec)
        assert store.active_records()[0]["freshness"] == "current"

        store.begin_source_run("closed", "fixture")
        result = store.promote("closed", spec, complete=False, closed_page_ids={"page-1"})
        assert result.closed == 1
        assert store.active_records() == []


def test_outdated_record_schema_is_quarantined_even_for_authoritative_source(
    app_config: AppConfig, tmp_path: Path
) -> None:
    with StateStore(tmp_path / "state.sqlite3") as store:
        store.ensure_sources((_spec(),))
        _promote(store, app_config, "initial")
        row = store.connection.execute("SELECT stable_key, record_json FROM records").fetchone()
        payload = json.loads(row["record_json"])
        payload["schema_version"] = HOUSING_RECORD_SCHEMA_VERSION - 1
        with store.connection:
            store.connection.execute(
                "UPDATE records SET record_json=?, freshness='current' WHERE stable_key=?",
                (json.dumps(payload), row["stable_key"]),
            )

        store.apply_listing_freshness((_spec(),))

        assert store.active_records()[0]["freshness"] == "stale"
        assert store.routing_candidates() == []


def test_report_separates_current_decisions_from_quarantined_inventory(
    app_config: AppConfig, tmp_path: Path
) -> None:
    with StateStore(tmp_path / "state.sqlite3") as store:
        store.ensure_sources((_spec(),))
        first = _record()
        second = replace(first, external_id="unit-2", title="Second unit")
        store.begin_source_run("initial", "fixture")
        for page_id, record in (("page-1", first), ("page-2", second)):
            store.stage(
                "initial",
                page_id,
                record,
                classify(record, app_config.profile, app_config.ranking),
            )
        store.promote("initial", _spec(), complete=True)
        with store.connection:
            store.connection.execute(
                "UPDATE records SET freshness='stale' WHERE stable_key=?",
                (first.stable_key,),
            )

        payload = build_report_payload(store, "report", [], {"offline": True}, app_config)

    assert payload["classification_counts"] == {
        "eligible": 1,
        "review": 0,
        "unverified": 1,
        "excluded": 0,
    }
    assert payload["current_classification_counts"] == {
        "eligible": 1,
        "review": 0,
        "unverified": 0,
        "excluded": 0,
    }
    assert payload["freshness_counts"] == {"current": 1, "stale": 1}
    stale = next(item for item in payload["housing"] if item["freshness"] == "stale")
    assert stale["classification"] == "unverified"
    assert "listing is stale" in " ".join(stale["cautions"])


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
    assert "effective_monthly_yen" in csv_text.splitlines()[0]
    assert "cash_before_keys_yen" in csv_text.splitlines()[0]
    assert "refundable_cash_yen" in csv_text.splitlines()[0]
    assert "move_out" in csv_text.splitlines()[0]
    assert latest["schema_version"] == 10
    assert latest["evidence_policy"] == {
        "mode": "separate_unverified",
        "strict_evidence": True,
        "classification_invariant_ok": True,
        "manual_review_count": 0,
        "current_manual_review_count": 0,
        "unverified_count": 0,
        "current_unverified_count": 0,
    }
    assert latest["ranking"]["pareto"]["complexity"] == "O(p * n log n), p <= 4"
    assert latest["ranking"]["pareto"]["candidate_count"] == 1
    assert [perspective["id"] for perspective in latest["ranking"]["pareto"]["perspectives"]] == [
        "balanced",
        "kichijoji",
        "shibuya",
        "shinjuku",
    ]
    assert latest["ranking"]["pareto"]["frontier_counts"] == {
        "balanced": 1,
        "kichijoji": 1,
        "shibuya": 1,
        "shinjuku": 1,
    }
    assert latest["housing"][0]["pareto_candidate"] is True
    assert latest["housing"][0]["pareto_frontier"] is True
    assert isinstance(latest["housing"][0]["pareto_location_score"], float)
    assert set(latest["housing"][0]["pareto_location_scores"]) == {
        "balanced",
        "kichijoji",
        "shibuya",
        "shinjuku",
    }
    assert (
        latest["housing"][0]["scenario"]["move_in"]
        == latest["decision_scenario"]["preferences"]["move_in"]
    )
    assert "'=formula-like title" in csv_text
    markdown = published.markdown_path.read_text(encoding="utf-8")
    assert "Saved decision scenario" in markdown
    assert "known floors" in markdown
    assert "confirmed office pins" in markdown
    assert "scored" not in markdown
    manifest = json.loads((published.directory / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["files"]) == {"housing.json", "housing.md", "housing.csv"}
    assert '\n  "schema_version"' not in published.json_path.read_text(encoding="utf-8")
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
        store.finish_source_run(
            "initial",
            "fixture",
            "partial",
            SourceMetrics(
                discovered=2,
                detail_fetch_failures=2,
                supplemental_detail_network=3,
                supplemental_detail_failures=1,
                stale_revalidation_candidates=4,
                stale_revalidated=1,
            ),
            "cap",
        )
        payload = build_report_payload(store, "report", [], {"offline": True}, app_config)

    assert payload["status"] == "partial"
    assert payload["source_coverage"][0]["status"] == "partial"
    assert payload["source_coverage"][0]["verified_page_ratio"] == 0.0
    assert payload["source_coverage"][0]["detail_fetch_failures"] == 2
    assert payload["source_coverage"][0]["supplemental_detail_network"] == 3
    assert payload["source_coverage"][0]["supplemental_detail_failures"] == 1
    assert payload["source_coverage"][0]["stale_revalidation_candidates"] == 4
    assert payload["source_coverage"][0]["stale_revalidated"] == 1


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


def test_report_retention_enforces_storage_cap(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    directories = [runs / name for name in ("old", "middle", "current")]
    for index, directory in enumerate(directories):
        directory.mkdir(parents=True)
        (directory / "manifest.json").write_text("{}", encoding="utf-8")
        (directory / "housing.json").write_bytes(b"x" * 100)
        timestamp = (datetime.now().astimezone() - timedelta(hours=3 - index)).timestamp()
        os.utime(directory / "manifest.json", (timestamp, timestamp))
    per_run = sum(path.stat().st_size for path in directories[-1].iterdir())

    removed = prune_reports(
        tmp_path,
        days=30,
        keep_run_id="current",
        max_bytes=per_run * 2,
    )

    assert removed == 1
    assert not directories[0].exists()
    assert directories[1].exists()
    assert directories[2].exists()


def test_saved_report_retains_reviewed_values_and_their_provenance(app_config, tmp_path):
    from japan_housing.evidence import EvidenceStore

    path = tmp_path / "housing.sqlite3"
    with StateStore(path) as store:
        store.ensure_sources((_spec(),))
        _promote(store, app_config, "initial")
        key = store.active_records()[0]["stable_key"]
        with EvidenceStore(path) as evidence:
            evidence.add(
                key,
                {
                    "values": {"room_area_m2": 12.5},
                    "kind": "confirmed",
                    "source_url": "https://example.com/room-quote",
                    "note": "Measured",
                },
            )
        payload = build_report_payload(store, "reviewed", [], {}, app_config)
    row = next(r for r in payload["housing"] if r["stable_key"] == key)
    assert row["room_area_m2"] == 12.5
    assert row["field_evidence"]["room_area_m2"]["note"] == "Measured"
