from __future__ import annotations

import json
from pathlib import Path

import pytest

from japan_housing.config import AppConfig
from japan_housing.models import HOUSING_RECORD_SCHEMA_VERSION, HousingRecord
from japan_housing.policy import classify
from japan_housing.report import build_report_payload, load_latest, publish_report
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
    manifest = json.loads((published.directory / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["files"]) == {"housing.json", "housing.md", "housing.csv"}
    with pytest.raises(RuntimeError, match="already exists"):
        publish_report(tmp_path / "artifacts", payload)
