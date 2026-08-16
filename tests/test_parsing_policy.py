from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from japan_housing.config import AppConfig
from japan_housing.models import HOUSING_RECORD_SCHEMA_VERSION, HousingRecord, ListingRef
from japan_housing.parsing import normalize_records
from japan_housing.policy import classify, cost_scenario, policy_config_hash


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

    assert [record.external_id for record in records] == [
        "page-1:101",
        "page-1:102",
        "page-1:104",
    ]
    assert records[0].recurring_total_yen == 77_000
    assert records[0].advertised_monthly_from_yen == 68_000
    assert records[0].price_basis == "available_room"
    assert records[0].price_validation == "matched"
    assert records[0].source_availability_status == "vacancy"
    assert records[0].available_from == "2026-09-17"
    assert records[0].coordinate_basis == "provider_detail_map_link"
    assert records[0].registration_confidence == "confirmed"
    eligible = classify(records[0], app_config.profile, app_config.ranking)
    assert eligible.name == "eligible"
    assert any("+9,000 yen" in reason for reason in eligible.reasons)
    female_only = classify(records[1], app_config.profile, app_config.ranking)
    assert female_only.name == "excluded"
    assert "female-only room" in female_only.reasons
    after_window = classify(records[2], app_config.profile, app_config.ranking)
    assert after_window.name == "excluded"
    assert "available after landing window: 2026-10-01" in after_window.reasons


def test_oakhouse_uses_available_room_price_not_advertised_from_price(
    app_config: AppConfig, project_root: Path
) -> None:
    document = (project_root / "tests" / "fixtures" / "oakhouse.html").read_text(encoding="utf-8")
    document = document.replace('data-sort_price="77000"', 'data-sort_price="83000"', 1)
    document = document.replace("Rent: ¥59,000", "Rent: ¥65,000", 1)
    ref = ListingRef("oakhouse", "1", "https://www.oakhouse.jp/eng/house/1", "fingerprint")

    record = normalize_records(ref, "Oakhouse", document)[0]
    outcome = classify(record, app_config.profile, app_config.ranking)

    assert record.advertised_monthly_from_yen == 68_000
    assert record.recurring_total_yen == 83_000
    assert outcome.name == "excluded"
    assert any("exceeds ¥80,000" in reason for reason in outcome.reasons)


def test_oakhouse_price_component_mismatch_cannot_be_eligible(
    app_config: AppConfig, project_root: Path
) -> None:
    document = (project_root / "tests" / "fixtures" / "oakhouse.html").read_text(encoding="utf-8")
    document = document.replace('data-sort_price="77000"', 'data-sort_price="76000"', 1)
    ref = ListingRef("oakhouse", "1", "https://www.oakhouse.jp/eng/house/1", "fingerprint")

    record = normalize_records(ref, "Oakhouse", document)[0]
    outcome = classify(record, app_config.profile, app_config.ranking)

    assert record.recurring_total_yen == 77_000
    assert record.price_validation == "mismatch_conservative_max"
    assert "available-room total mismatch" in record.unknown_costs
    assert outcome.name == "review"


def test_oakhouse_vacancy_soon_must_be_inside_move_window(
    app_config: AppConfig, project_root: Path
) -> None:
    document = (project_root / "tests" / "fixtures" / "oakhouse.html").read_text(encoding="utf-8")
    document = document.replace("2026/10/01 ~", "2026/09/26 ~")
    ref = ListingRef("oakhouse", "1", "https://www.oakhouse.jp/eng/house/1", "fingerprint")

    record = normalize_records(ref, "Oakhouse", document)[-1]

    assert record.source_availability_status == "soon"
    assert record.available_from == "2026-09-26"
    assert classify(record, app_config.profile, app_config.ranking).name == "eligible"


def test_vacancy_available_before_move_window_is_not_penalized(
    app_config: AppConfig, project_root: Path
) -> None:
    document = (project_root / "tests" / "fixtures" / "oakhouse.html").read_text(encoding="utf-8")
    document = document.replace("2026/09/17 ~", "2026/08/20 ~", 1)
    ref = ListingRef("oakhouse", "1", "https://www.oakhouse.jp/eng/house/1", "fingerprint")

    outcome = classify(
        normalize_records(ref, "Oakhouse", document)[0],
        app_config.profile,
        app_config.ranking,
    )

    assert outcome.name == "eligible"
    assert outcome.rank_key[4] == 0


def test_best_estate_is_kept_for_registration_review(
    app_config: AppConfig, project_root: Path
) -> None:
    record = _parse(project_root / "tests" / "fixtures", "best_estate")[0]

    assert record.housing_type == "1K"
    assert record.address == "Tokyo Shinjuku-ku Takadanobaba 1-2-3"
    assert record.city == "Shinjuku-ku"
    assert record.recurring_total_yen == 70_000
    assert record.latitude == 35.7126
    assert record.longitude == 139.7038
    assert record.available_from == "2026-09-20"
    outcome = classify(record, app_config.profile, app_config.ranking)
    assert outcome.name == "review"
    assert any("registration" in caution for caution in outcome.cautions)


def test_coordinate_inconsistent_with_prefecture_is_not_scored(
    app_config: AppConfig, project_root: Path
) -> None:
    document = (project_root / "tests" / "fixtures" / "best_estate.html").read_text(
        encoding="utf-8"
    )
    document = document.replace('"longitude": "139.7038"', '"longitude": "137.5549"')
    ref = ListingRef(
        "best_estate",
        "1",
        "https://www.best-estate.jp/en/property/1/",
        "fingerprint",
    )

    record = normalize_records(ref, "Best-Estate.jp", document)[0]
    outcome = classify(record, app_config.profile, app_config.ranking)

    assert record.latitude is None
    assert record.longitude is None
    assert record.coordinate_basis.endswith("inconsistent_with_tokyo")
    assert outcome.distance_to_anchor_km is None


def test_wagaya_parser_preserves_cost_and_evidence(
    app_config: AppConfig, project_root: Path
) -> None:
    record = _parse(project_root / "tests" / "fixtures", "wagaya")[0]

    assert record.prefecture == "Kanagawa"
    assert record.recurring_total_yen == 68_000
    assert record.initial_cost_yen == 120_000
    assert record.walk_minutes_min == 8
    assert record.coordinate_basis == "provider_detail_map_link"
    assert record.availability_confidence == "explicit"
    assert classify(record, app_config.profile, app_config.ranking).name == "review"


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
    assert classify(record, app_config.profile, app_config.ranking).name == "excluded"


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


def test_priority_combines_price_and_straight_line_distance(
    app_config: AppConfig,
    project_root: Path,
) -> None:
    base = _parse(project_root / "tests" / "fixtures", "best_estate")[0]
    cheaper_farther = replace(
        base,
        external_id="cheap-far",
        recurring_total_yen=50_000,
        latitude=35.7580,
        longitude=139.7016,
    )
    pricier_near = replace(
        base,
        external_id="pricey-near",
        recurring_total_yen=75_000,
        latitude=35.6580,
        longitude=139.7016,
    )

    cheap_outcome = classify(cheaper_farther, app_config.profile, app_config.ranking)
    near_outcome = classify(pricier_near, app_config.profile, app_config.ranking)

    assert cheap_outcome.distance_to_anchor_km == pytest.approx(11.12, abs=0.02)
    assert near_outcome.distance_to_anchor_km == 0
    assert near_outcome.priority_score < cheap_outcome.priority_score
    assert near_outcome.rank_key < cheap_outcome.rank_key


def test_complete_landing_cost_drives_ranking_price(
    app_config: AppConfig,
    project_root: Path,
) -> None:
    record = _parse(project_root / "tests" / "fixtures", "oakhouse")[0]

    outcome = classify(record, app_config.profile, app_config.ranking)

    assert outcome.ranking_price_yen == round((77_000 * 3 + 50_000) / 3)
    assert outcome.ranking_price_basis == "three_month_effective"
    assert outcome.priority_closer_score != outcome.priority_cheaper_score


def test_maximum_straight_line_distance_is_enforced(
    app_config: AppConfig,
    project_root: Path,
) -> None:
    base = _parse(project_root / "tests" / "fixtures", "best_estate")[0]
    distant = replace(
        base,
        prefecture="Chiba",
        latitude=35.73,
        longitude=140.86,
    )

    outcome = classify(distant, app_config.profile, app_config.ranking)

    assert outcome.name == "excluded"
    assert any("exceeds maximum useful distance" in reason for reason in outcome.reasons)


def test_missing_coordinates_receive_explicit_distance_penalty(
    app_config: AppConfig,
    project_root: Path,
) -> None:
    base = _parse(project_root / "tests" / "fixtures", "best_estate")[0]
    missing = replace(base, latitude=None, longitude=None, coordinate_basis="unknown")

    outcome = classify(missing, app_config.profile, app_config.ranking)

    assert outcome.distance_to_anchor_km is None
    assert outcome.priority_distance_component == 60.0
    assert any("30 km penalty" in caution for caution in outcome.cautions)


def test_policy_hash_is_stable_for_unordered_profile_sets(app_config: AppConfig) -> None:
    reordered = replace(
        app_config.profile,
        allowed_housing_types=frozenset(["1K", "private_room", "1R"]),
    )

    assert policy_config_hash(reordered, app_config.ranking) == policy_config_hash(
        app_config.profile, app_config.ranking
    )
