from __future__ import annotations

from pathlib import Path

from japan_housing.config import AppConfig
from japan_housing.models import HOUSING_RECORD_SCHEMA_VERSION, HousingRecord, ListingRef
from japan_housing.parsing import normalize_records
from japan_housing.policy import classify, cost_scenario


def _parse(fixture_root: Path, source_id: str):
    document = (fixture_root / f"{source_id}.html").read_text(encoding="utf-8")
    ref = ListingRef(
        source_id,
        "page-1",
        f"https://example.invalid/{source_id}/page-1",
        "list-fingerprint",
    )
    return normalize_records(ref, source_id, document)


def test_oakhouse_rooms_are_independent_and_vacant_only(
    app_config: AppConfig, project_root: Path
) -> None:
    records = _parse(project_root / "tests" / "fixtures", "oakhouse")

    assert [record.external_id for record in records] == ["page-1:101", "page-1:102"]
    assert records[0].recurring_total_yen == 77_000
    assert records[0].available_from == "2026-09-17"
    assert records[0].registration_confidence == "confirmed"
    assert classify(records[0], app_config.profile).name == "eligible"
    female_only = classify(records[1], app_config.profile)
    assert female_only.name == "excluded"
    assert "female-only room" in female_only.reasons


def test_best_estate_is_kept_for_registration_review(
    app_config: AppConfig, project_root: Path
) -> None:
    record = _parse(project_root / "tests" / "fixtures", "best_estate")[0]

    assert record.housing_type == "1K"
    assert record.address == "Tokyo Shinjuku-ku Takadanobaba 1-2-3"
    assert record.city == "Shinjuku-ku"
    assert record.recurring_total_yen == 70_000
    assert record.available_from == "2026-09-20"
    outcome = classify(record, app_config.profile)
    assert outcome.name == "review"
    assert any("registration" in caution for caution in outcome.cautions)


def test_wagaya_parser_preserves_cost_and_evidence(
    app_config: AppConfig, project_root: Path
) -> None:
    record = _parse(project_root / "tests" / "fixtures", "wagaya")[0]

    assert record.prefecture == "Kanagawa"
    assert record.recurring_total_yen == 68_000
    assert record.initial_cost_yen == 120_000
    assert record.walk_minutes_min == 8
    assert record.availability_confidence == "explicit"
    assert classify(record, app_config.profile).name == "review"


def test_gender_restrictions_are_never_only_free_text(
    app_config: AppConfig, project_root: Path
) -> None:
    document = (project_root / "tests" / "fixtures" / "best_estate.html").read_text(
        encoding="utf-8"
    )
    document = document.replace("</body>", "<p>Preferences: Female Only</p></body>")
    ref = ListingRef(
        "best_estate",
        "1",
        "https://www.best-estate.jp/en/property/1/",
        "fingerprint",
    )

    record = normalize_records(ref, "Best-Estate.jp", document)[0]

    assert "female_only" in record.occupancy_restrictions
    assert classify(record, app_config.profile).name == "excluded"


def test_oakhouse_prefecture_comes_from_title_before_navigation(
    project_root: Path,
) -> None:
    document = (project_root / "tests" / "fixtures" / "oakhouse.html").read_text(encoding="utf-8")
    document = document.replace(
        "OAKHOUSE ITABASHI | Tokyo Itabashi",
        "APPARTEMENT AICHI | Aichi Kariya",
    ).replace("<p>Tokyo Itabashi</p>", "<nav>Tokyo housing</nav><p>Aichi Kariya</p>")
    ref = ListingRef("oakhouse", "1139", "https://www.oakhouse.jp/eng/house/1139", "fingerprint")

    record = normalize_records(ref, "Oakhouse", document)[0]

    assert record.prefecture == "Aichi"


def test_unknown_costs_are_never_coerced_to_zero() -> None:
    record = HousingRecord(
        schema_version=HOUSING_RECORD_SCHEMA_VERSION,
        source_id="fixture",
        source_label="Fixture",
        external_id="1",
        url="https://example.invalid/1",
        title="Unknown costs",
    )

    scenario = cost_scenario(record, 3)

    assert scenario.known_total_yen is None
    assert scenario.effective_monthly_yen is None
    assert not scenario.complete
    assert "recurring total" in scenario.unknown_costs
