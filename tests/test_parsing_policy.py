from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from japan_housing.config import AppConfig
from japan_housing.models import (
    HOUSING_RECORD_SCHEMA_VERSION,
    AnchorCommute,
    CommuteSummary,
    HousingRecord,
    ListingRef,
    expected_housing_record_schema_version,
)
from japan_housing.parsing import (
    normalize_records,
    oakhouse_snapshot_verified,
    oakhouse_verified_empty,
    source_verified_empty,
)
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


def _commute(app_config: AppConfig, transit: float, bicycle: float) -> CommuteSummary:
    anchors = tuple(
        AnchorCommute(
            anchor.anchor_id,
            anchor.label,
            transit,
            10.0,
            "ROUTE_EXISTS",
            bicycle,
            8.0,
            "ROUTE_EXISTS",
        )
        for anchor in app_config.routing.anchors
    )
    count = len(anchors)
    return CommuteSummary(
        anchors,
        transit,
        bicycle,
        transit,
        bicycle,
        count,
        count,
        count,
        f"{transit}-{bicycle}",
    )


@pytest.mark.parametrize("source_id", ("oakhouse", "best_estate", "wagaya"))
def test_parser_schema_versions_are_scoped_to_the_source(
    source_id: str,
    project_root: Path,
) -> None:
    record = _parse(project_root / "tests" / "fixtures", source_id)[0]

    assert record.schema_version == expected_housing_record_schema_version(source_id)


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


def test_oakhouse_empty_inventory_requires_structural_evidence(project_root: Path) -> None:
    document = (project_root / "tests" / "fixtures" / "oakhouse.html").read_text(encoding="utf-8")
    drifted = document.replace("p-room__caset__row", "unknown-room-row")
    closed = document.replace('data-status="vacancy"', 'data-status="novacancy"').replace(
        'data-status="soon"', 'data-status="novacancy"'
    )
    partially_drifted = document.replace("p-room__caset__row", "unknown-room-row", 1)

    assert not oakhouse_verified_empty(drifted)
    assert oakhouse_verified_empty(closed)
    assert not oakhouse_snapshot_verified(partially_drifted, 2)


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
    assert outcome.name == "unverified"
    assert any("available-room total mismatch" in caution for caution in outcome.cautions)


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


def test_best_estate_without_confirmed_registration_remains_visible_but_blocked(
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
    assert outcome.name == "unverified"
    assert any("registration" in caution for caution in outcome.cautions)

    review_profile = replace(app_config.profile, strict_evidence=False)
    review = classify(record, review_profile, app_config.ranking)
    assert review.name == "review"
    assert any("registration" in caution for caution in review.cautions)


def test_best_estate_expired_provider_update_is_retained_as_excluded(
    app_config: AppConfig,
) -> None:
    document = """
        <html><head>
          <title>1K Apartment in Tokyo - Best-Estate</title>
          <meta name="x-property-layout" content="1K">
          <meta name="x-property-rent" content="60000">
        </head><body>
          <div>Maintenance Fee</div><div>5,000 Yen</div>
          <div>Deposit</div><div>0 Yen</div>
          <div>Key Money</div><div>0 Yen</div>
          <div>Home Insurance</div><div>Not required</div>
          <div>Occupancy Date</div><div>Immediately</div>
          <div>Last updated</div><div>2020/01/01</div>
          <div>Next update date</div><div>2020/01/08</div>
        </body></html>
    """
    ref = ListingRef(
        "best_estate",
        "expired-provider-update",
        "https://www.best-estate.jp/en/property/4/",
        "fingerprint",
    )

    record = normalize_records(ref, "Best-Estate.jp", document)[0]
    outcome = classify(record, app_config.profile, app_config.ranking)

    assert record.provider_updated_at == "2020-01-01"
    assert record.provider_next_update_at == "2020-01-08"
    assert outcome.name == "excluded"
    assert "provider listing freshness expired: next update was 2020-01-08" in outcome.reasons


def test_unknown_one_time_costs_do_not_override_verified_hard_requirements(
    app_config: AppConfig,
) -> None:
    record = HousingRecord(
        schema_version=HOUSING_RECORD_SCHEMA_VERSION,
        source_id="fixture",
        source_label="Fixture",
        external_id="cost-uncertainty",
        url="https://example.test/cost-uncertainty",
        title="1K apartment",
        prefecture="Tokyo",
        housing_type="1K",
        private_space=True,
        rent_yen=60_000,
        management_fee_yen=5_000,
        mandatory_monthly_fees_yen=0,
        recurring_total_yen=65_000,
        initial_cost_yen=50_000,
        exit_cost_yen=None,
        unknown_costs=("renter's insurance amount", "exit cost"),
        available_text="Immediately",
        availability_confidence="available_now",
        contract_type="standard",
        foreigner_evidence=("accepted",),
        overseas_application_evidence=("online",),
        registration_evidence=("standard residential lease",),
        registration_confidence="standard_residential",
    )

    outcome = classify(record, app_config.profile, app_config.ranking)

    assert outcome.name == "eligible"
    assert any("cost evidence incomplete" in caution for caution in outcome.cautions)
    assert outcome.long_term.complete is False


def test_non_oak_explicit_availability_after_landing_window_is_excluded(
    app_config: AppConfig, project_root: Path
) -> None:
    document = (project_root / "tests" / "fixtures" / "best_estate.html").read_text(
        encoding="utf-8"
    )
    document = document.replace("2026/09/20", "2026/10/01")
    ref = ListingRef(
        "best_estate",
        "late",
        "https://www.best-estate.jp/en/property/late/",
        "fingerprint",
    )

    outcome = classify(
        normalize_records(ref, "Best-Estate.jp", document)[0],
        app_config.profile,
        app_config.ranking,
    )

    assert outcome.name == "excluded"
    assert "available after landing window: 2026-10-01" in outcome.reasons
    assert any("registration" in caution for caution in outcome.cautions)


def test_strict_profile_excludes_non_oak_listing_without_move_in_window(
    app_config: AppConfig,
) -> None:
    document = """
        <html><head>
          <title>1K Apartment in Tokyo - Best-Estate</title>
          <meta name="x-property-layout" content="1K">
          <meta name="x-property-rent" content="60000">
        </head><body>
          <div>Maintenance Fee</div><div>5,000 Yen</div>
          <div>Deposit</div><div>0 Yen</div>
          <div>Key Money</div><div>0 Yen</div>
          <div>Home Insurance</div><div>Not required</div>
          <div>Occupancy Date</div><div>Consultation</div>
        </body></html>
    """
    ref = ListingRef(
        "best_estate",
        "unknown-move-in",
        "https://www.best-estate.jp/en/property/5/",
        "fingerprint",
    )

    outcome = classify(
        normalize_records(ref, "Best-Estate.jp", document)[0],
        app_config.profile,
        app_config.ranking,
    )

    assert outcome.name == "excluded"
    assert "move-in date is not verified for the landing window" in outcome.reasons


@pytest.mark.parametrize(
    ("provider_date", "expected"),
    (
        ("2026-9-Early", "2026-09-10"),
        ("2026-9-Middle", "2026-09-20"),
        ("2026-9-Late", "2026-09-30"),
        ("2026-9", "2026-09-30"),
        ("2026-10", "2026-10-31"),
    ),
)
def test_provider_availability_periods_use_conservative_end_dates(
    provider_date: str,
    expected: str,
    project_root: Path,
) -> None:
    document = (project_root / "tests" / "fixtures" / "best_estate.html").read_text(
        encoding="utf-8"
    )
    document = document.replace("2026/09/20", provider_date)
    ref = ListingRef(
        "best_estate",
        provider_date,
        "https://www.best-estate.jp/en/property/date/",
        "fingerprint",
    )

    record = normalize_records(ref, "Best-Estate.jp", document)[0]

    assert record.available_from == expected
    assert record.availability_confidence == "explicit"


def test_best_estate_live_fee_components_are_known_without_guessing_variable_guarantee(
    app_config: AppConfig,
) -> None:
    document = """
        <html><head>
          <title>1R Share House For Rent in Tokyo Nakano-ku - Best-Estate</title>
          <meta name="x-property-layout" content="1R">
          <meta name="x-property-rent" content="48,000Yen/month">
        </head><body>
          <div>Maintenance Fee</div><div>16,500 Yen</div>
          <div>Deposit</div><div>0 Yen</div>
          <div>Key Money</div><div>0 Yen</div>
          <div>Home Insurance</div><div>Not required</div>
          <div>Occupancy Date</div><div>Immediately</div>
          <div>Other expenses</div>
          <div>■入居時事務手数料：30,000円(税込)</div>
          <div>■退去時清掃費用：16,500円(税込)</div>
          <div>Guarantor Company</div><div>Subscription required</div>
          <div>Initial Guarantee fee 30%~100% of monthly total rent</div>
          <div>Minimum guarantee fee 20,000 yen</div>
          <div>+ Annual guarantee fee (10,000 yen) or Monthly guarantee fee (1,000 yen~)</div>
          <footer>Monthly Apartments</footer>
        </body></html>
    """
    ref = ListingRef(
        "best_estate",
        "live-fees",
        "https://www.best-estate.jp/en/property/live-fees/",
        "fingerprint",
    )

    record = normalize_records(ref, "Best-Estate.jp", document)[0]
    outcome = classify(record, app_config.profile, app_config.ranking)

    assert record.mandatory_monthly_fees_yen == 1_000
    assert record.recurring_total_yen == 65_500
    assert record.initial_cost_yen == 50_000
    assert record.exit_cost_yen == 16_500
    assert "variable guarantor costs" in record.unknown_costs
    assert record.contract_type == "standard"
    assert outcome.track == "long_term"
    assert outcome.ranking_price_yen == round((65_500 * 12 + 50_000 + 16_500) / 12)
    assert outcome.ranking_price_basis == "long_term_known_cost_floor_incomplete_total"


def test_best_estate_exact_guarantee_and_brokerage_terms_use_the_correct_bases(
    app_config: AppConfig,
) -> None:
    document = """
        <html><head>
          <title>1K Apartment in Kanagawa - Best-Estate</title>
          <meta name="x-property-rent" content="32000">
        </head><body>
          <div>Maintenance Fee</div><div>3,000 Yen</div>
          <div>Deposit</div><div>0 Yen</div>
          <div>Key Money</div><div>0 Yen</div>
          <div>Home Insurance</div><div>Not required</div>
          <div>Room Type</div><div>1K</div>
          <div>Occupancy Date</div><div>Immediately</div>
          <div>仲介手数料家賃の0.55ヵ月分。</div>
          <div>Others</div><div>保証委託料（賃料総額に対し、初回額50％、月額2％）。</div>
        </body></html>
    """
    ref = ListingRef(
        "best_estate",
        "exact-fees",
        "https://www.best-estate.jp/en/property/exact-fees/",
        "fingerprint",
    )

    record = normalize_records(ref, "Best-Estate.jp", document)[0]

    assert record.mandatory_monthly_fees_yen == 700
    assert record.recurring_total_yen == 35_700
    assert record.initial_cost_yen == 35_100
    assert "brokerage fee" not in record.unknown_costs


def test_best_estate_guarantee_company_monthly_total_wording_is_exact() -> None:
    document = """
        <html><head>
          <title>1K Apartment in Kanagawa - Best-Estate</title>
          <meta name="x-property-rent" content="60000">
        </head><body>
          <div>Maintenance Fee</div><div>3,500 Yen</div>
          <div>Deposit</div><div>0 Yen</div>
          <div>Key Money</div><div>0 Yen</div>
          <div>Home Insurance</div><div>Not required</div>
          <div>Room Type</div><div>1K</div>
          <div>Occupancy Date</div><div>Immediately</div>
          <div>Others</div><div>保証会社加入要(初回月額総額50%、月次月額総額2%)。</div>
        </body></html>
    """
    ref = ListingRef(
        "best_estate",
        "alternate-guarantee",
        "https://www.best-estate.jp/en/property/alternate-guarantee/",
        "fingerprint",
    )

    record = normalize_records(ref, "Best-Estate.jp", document)[0]

    assert record.mandatory_monthly_fees_yen == 1_270
    assert record.recurring_total_yen == 64_770
    assert record.initial_cost_yen == 31_750


def test_best_estate_dash_maintenance_does_not_capture_the_next_amount() -> None:
    document = """
        <html><head>
          <title>2K Apartment in Kanagawa - Best-Estate</title>
          <meta name="x-property-rent" content="60000">
        </head><body>
          <div>Maintenance Fee</div><div>- Yen</div>
          <div>Deposit</div><div>- Yen</div>
          <div>Key Money</div><div>- Yen</div>
          <div>Home Insurance</div><div>Not required</div>
          <div>Room Type</div><div>2K</div>
          <div>Occupancy Date</div><div>Immediately</div>
          <div>仲介手数料家賃の0.55ヵ月分。</div>
          <div>Others</div><div>保証委託料（賃料総額に対し、初回額50％、月額2％）。</div>
        </body></html>
    """
    ref = ListingRef(
        "best_estate",
        "dash-maintenance",
        "https://www.best-estate.jp/en/property/dash-maintenance/",
        "fingerprint",
    )

    record = normalize_records(ref, "Best-Estate.jp", document)[0]

    assert record.management_fee_yen == 0
    assert record.mandatory_monthly_fees_yen == 1_200
    assert record.recurring_total_yen == 61_200
    assert record.initial_cost_yen == 63_000
    assert "brokerage fee" not in record.unknown_costs


def test_best_estate_bare_entry_fees_and_smart_lock_floor_are_preserved() -> None:
    document = """
        <html><head>
          <title>1K Apartment in Tokyo - Best-Estate</title>
          <meta name="x-property-rent" content="37000">
        </head><body>
          <div>Maintenance Fee</div><div>3,000 Yen</div>
          <div>Deposit</div><div>0 Yen</div>
          <div>Key Money</div><div>37,000 Yen</div>
          <div>Home Insurance</div><div>Not required</div>
          <div>Room Type</div><div>1K</div>
          <div>Occupancy Date</div><div>Immediately</div>
          <div>Other expenses</div><div>エアコン分解清掃：12100</div>
          <div>２４H緊急サポート：19800</div><div>抗菌施工：13200</div>
          <div>スマートロック利用料1650円がかかります。</div>
          <div>スマモル賃貸プラン未加入の場合、合計3300円(税込)/月</div>
          <div>Guarantor Company</div><div>Subscription required</div>
          <div>Minimum guarantee fee 20,000 yen</div>
          <div>Monthly guarantee fee (1,000 yen~)</div>
        </body></html>
    """
    ref = ListingRef(
        "best_estate",
        "bare-fees",
        "https://www.best-estate.jp/en/property/bare-fees/",
        "fingerprint",
    )

    record = normalize_records(ref, "Best-Estate.jp", document)[0]

    assert record.mandatory_monthly_fees_yen == 2_650
    assert record.recurring_total_yen == 42_650
    assert record.initial_cost_yen == 102_100
    assert "variable smart-lock service fees" in record.unknown_costs


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
    assert classify(record, app_config.profile, app_config.ranking).name == "unverified"


def test_wagaya_lossy_english_layout_uses_japanese_detail_evidence() -> None:
    document = """
        <html><head><title>Room 207 Tokyo | wagaya Japan</title></head><body>
          <h1>Property Details</h1>
          <div>Location</div><div>Tokyo Suginami-ku</div>
          <div>Type</div><div>Corporate Apartment</div>
          <div>Layout</div><div>1bds</div>
          <div>Current Status</div><div>Vacant</div>
          <div>Available from</div><div>Immediately</div>
          <div>Initial Cost Estimate</div><div>Total ¥92,000</div>
          <div>Rent</div><div>¥43,000</div>
          <div>Maintenance Fee</div><div>¥6,000</div>
        </body></html>
    """
    japanese_document = """
        <html><body><h1>物件情報</h1><dl><dt>間取り</dt><dd>1K</dd></dl></body></html>
    """
    ref = ListingRef(
        "wagaya",
        "1104343",
        "https://wagaya-japan.com/en/chintai_detail.php?id=1104343",
        "fingerprint",
    )

    record = normalize_records(
        ref,
        "wagaya Japan",
        document,
        supplemental_document=japanese_document,
    )[0]

    assert record.housing_type == "1K"
    assert record.private_space is True
    assert "Japanese layout: 1K" in record.source_evidence


def test_wagaya_vacant_status_resolves_placeholder_availability() -> None:
    document = """
        <html><head><title>1studio Tokyo | wagaya Japan</title></head><body>
          <h1>Property Details</h1>
          <div>Location</div><div>Tokyo Suginami-ku</div>
          <div>Type</div><div>Mansion</div>
          <div>Layout</div><div>1studio</div>
          <div>Current Status</div><div>Vacant</div>
          <div>Available from</div><div>need to confirm</div>
          <div>Rent</div><div>¥43,000</div>
          <div>Maintenance Fee</div><div>¥6,000</div>
        </body></html>
    """
    ref = ListingRef(
        "wagaya",
        "vacant-placeholder",
        "https://wagaya-japan.com/en/chintai_detail.php?id=1",
        "fingerprint",
    )

    record = normalize_records(ref, "wagaya Japan", document)[0]

    assert record.available_text == "Vacant"
    assert record.availability_confidence == "available_now"
    assert record.source_availability_status == "vacancy"


def test_wagaya_empty_definition_does_not_capture_the_next_label() -> None:
    document = """
        <html><head><title>1studio Tokyo | wagaya Japan</title></head><body>
          <h1>Property Details</h1>
          <dl>
            <dt>Location</dt><dd>Tokyo Suginami-ku</dd>
            <dt>Type</dt><dd>Mansion</dd>
            <dt>Layout</dt><dd>1studio</dd>
            <dt>Current Status</dt><dd>Vacant</dd>
            <dt>Available from</dt><dd>Immediately</dd>
            <dt>Guarantor Fee description</dt><dd></dd>
            <dt>Building Structure</dt><dd>Reinforced concrete</dd>
            <dt>Rent</dt><dd>¥43,000</dd>
            <dt>Maintenance Fee</dt><dd>¥6,000</dd>
          </dl>
        </body></html>
    """
    ref = ListingRef(
        "wagaya",
        "empty-guarantor",
        "https://wagaya-japan.com/en/chintai_detail.php?id=6",
        "fingerprint",
    )

    record = normalize_records(ref, "wagaya Japan", document)[0]

    assert record.guarantor_evidence == ()


def test_wagaya_occupied_without_moveout_date_is_not_a_candidate(
    app_config: AppConfig,
) -> None:
    document = """
        <html><head><title>1studio Tokyo | wagaya Japan</title></head><body>
          <h1>Property Details</h1>
          <div>Location</div><div>Tokyo Suginami-ku</div>
          <div>Type</div><div>Mansion</div>
          <div>Layout</div><div>1studio</div>
          <div>Current Status</div><div>Moving In</div>
          <div>Available from</div><div>need to confirm</div>
          <div>Rent</div><div>¥43,000</div>
          <div>Maintenance Fee</div><div>¥6,000</div>
        </body></html>
    """
    ref = ListingRef(
        "wagaya",
        "occupied-placeholder",
        "https://wagaya-japan.com/en/chintai_detail.php?id=2",
        "fingerprint",
    )

    outcome = classify(
        normalize_records(ref, "wagaya Japan", document)[0],
        app_config.profile,
        app_config.ranking,
    )

    assert outcome.name == "excluded"
    assert "occupied with no verified move-out date" in outcome.reasons


def test_wagaya_guarantee_and_contract_fees_are_included() -> None:
    document = """
        <html><head><title>1studio Tokyo | wagaya Japan</title></head><body>
          <h1>Property Details</h1>
          <div>Location</div><div>Tokyo Suginami-ku</div>
          <div>Type</div><div>Mansion</div>
          <div>Layout</div><div>1studio</div>
          <div>Current Status</div><div>Vacant</div>
          <div>Available from</div><div>Immediately</div>
          <div>Initial Guarantee Fee</div><div>-</div>
          <div>Guarantor Fee description</div>
          <div>An initial guarantee fee of 18,000 yen is charged, and a monthly
            guarantee fee of 4% of the total monthly amount will be incurred.</div>
          <div>Tenant support fee</div><div>￥22,000</div>
          <div>Room cleaning fee</div><div>￥33,000</div>
          <div>Initial Cost Estimate</div><div>Total ¥71,000</div>
          <div>Rent</div><div>¥28,000</div>
          <div>Maintenance Fee</div><div>¥15,000</div>
          <div>Deposit</div><div>¥0</div>
          <div>Key Money</div><div>¥0</div>
          <div>Agent Fee</div><div>¥28,000</div>
          <div>System usage fee: 1500 yen (monthly)</div>
        </body></html>
    """
    ref = ListingRef(
        "wagaya",
        "exact-contract-fees",
        "https://wagaya-japan.com/en/chintai_detail.php?id=3",
        "fingerprint",
    )

    record = normalize_records(ref, "wagaya Japan", document)[0]

    assert record.mandatory_monthly_fees_yen == 3_280
    assert record.recurring_total_yen == 46_280
    assert record.initial_cost_yen == 101_000
    assert "initial guarantee fee" not in record.unknown_costs


def test_wagaya_live_calculator_separates_recurring_and_one_time_costs(
    app_config: AppConfig,
) -> None:
    document = """
        <html><head><title>Room 106 Tokyo | wagaya Japan</title></head><body>
          <h1>Property Details</h1>
          <div>Location</div><div>Tokyo Meguro-ku</div>
          <div>Layout</div><div>1R</div>
          <div>Current Status</div><div>Vacant</div>
          <div>Available from</div><div>Immediately</div>
          <div>Initial Guarantee Fee</div><div>-</div>
          <div>Renter's Insurance</div><div>Required</div>
          <div>Agent Fee</div><div>1months</div>
          <div>Initial costs</div><div>￥30,000</div>
          <div>System usage fee: 1500 yen (monthly)</div>
          <div>System usage fee: 1500 yen (monthly)</div>
          <div>Environmental maintenance fee: 550 yen per month (tax included)</div>
          <div>Key replacement fee: 16,500 yen at the time of contract</div>
          <div>Cleaning fee upon move-out: 52,250 yen</div>
          <div>Internet usage fee: Charged</div>
          <div>Guarantee deposit: Required</div>
          <h2>Initial Cost Estimate</h2><div>Total ¥77,000</div>
          <div>Approximate estimate only. Please inquire for details.</div>
          <div>Rent</div><div>¥31,000</div>
          <div>Maintenance Fee</div><div>¥15,000</div>
          <div>Deposit</div><div>¥0</div>
          <div>Key Money</div><div>¥0</div>
          <div>Agent Fee</div><div>¥31,000</div>
          <footer>Monthly Rental Property</footer>
        </body></html>
    """
    ref = ListingRef(
        "wagaya",
        "live-fees",
        "https://wagaya-japan.com/en/chintai_detail.php?id=1",
        "fingerprint",
    )

    record = normalize_records(ref, "wagaya Japan", document)[0]
    outcome = classify(record, app_config.profile, app_config.ranking)

    assert record.rent_yen == 31_000
    assert record.management_fee_yen == 15_000
    assert record.mandatory_monthly_fees_yen == 2_050
    assert record.recurring_total_yen == 48_050
    assert record.initial_cost_yen == 77_500
    assert record.exit_cost_yen == 52_250
    assert "renter's insurance amount" in record.unknown_costs
    assert "guarantee deposit amount" in record.unknown_costs
    assert "internet usage fee amount" not in record.unknown_costs
    assert "exit cost" not in record.unknown_costs
    assert record.price_validation == "estimate_components_matched_known_floor"
    assert record.contract_type == "standard"
    assert outcome.track == "long_term"
    assert outcome.ranking_price_yen == round((48_050 * 12 + 77_500 + 52_250) / 12)
    assert outcome.ranking_price_basis == "long_term_known_cost_floor_incomplete_total"


def test_wagaya_detail_fields_ignore_navigation_and_online_service_noise(
    app_config: AppConfig,
) -> None:
    document = """
        <html><head>
          <title>Room 208 - Kanagawa Prefecture Yokohama City Aoba Ward
            Utsukushigaoka 2-chome [unit-208] | wagaya Japan</title>
        </head><body>
          <nav>Rent · Share house · Monthly Rental Property</nav>
          <h1>Room 208 Property Details</h1>
          <div>Availability Dependent on Property Location</div>
          <div>Online Contract Explanation</div>
          <div>Type</div><div>Mansion</div>
          <div>Layout</div><div>1studio</div>
          <div>Current Status</div><div>Moving In</div>
          <div>Available from</div><div>2026-09Mid-term (11th to 20th)</div>
          <div>Initial Cost Estimate</div><div>Total ¥207,000</div>
          <div>Rent</div><div>¥101,000</div>
          <div>Maintenance Fee</div><div>¥5,000</div>
          <div>Deposit</div><div>¥101,000</div>
          <div>Key Money</div><div>0months</div>
          <section><h2>Surrounding area</h2>
            <div>Location</div>
            <div>Kanagawa Prefecture Yokohama City Aoba Ward Utsukushigaoka 2-chome</div>
          </section>
        </body></html>
    """
    ref = ListingRef(
        "wagaya",
        "scoped-live",
        "https://wagaya-japan.com/en/chintai_detail.php?id=1",
        "fingerprint",
    )

    record = normalize_records(ref, "wagaya Japan", document)[0]

    assert record.address == ("Kanagawa Prefecture Yokohama City Aoba Ward Utsukushigaoka 2-chome")
    assert record.city == "Yokohama City Aoba Ward"
    assert record.housing_type == "1R"
    assert record.rent_yen == 101_000
    assert record.recurring_total_yen == 106_000
    assert record.available_text == "2026-09Mid-term (11th to 20th)"
    assert record.available_from == "2026-09-20"
    assert record.private_space is True
    assert classify(record, app_config.profile, app_config.ranking).name == "excluded"


def test_wagaya_explicit_removed_post_is_not_normalized() -> None:
    document = """
        <html><head><title>Old room Tokyo | wagaya Japan</title></head><body>
          <h1>Old room Property Details</h1>
          <p>G // The post has been removed</p>
          <footer>Apartment Infomation</footer>
        </body></html>
    """
    ref = ListingRef(
        "wagaya",
        "removed",
        "https://wagaya-japan.com/en/chintai_detail.php?id=2",
        "fingerprint",
    )

    assert source_verified_empty("wagaya", document)
    assert normalize_records(ref, "wagaya Japan", document) == []


def test_wagaya_city_preserves_a_city_name_matching_its_prefecture() -> None:
    document = """
        <html><head><title>Apartment - Inage Ward, Chiba City, Chiba Prefecture | wagaya</title>
        </head><body><h1>Property Details</h1>
          <div>Type</div><div>Mansion</div>
          <div>Layout</div><div>1bds</div>
          <div>Location</div><div>Inage Ward, Chiba City, Chiba Prefecture</div>
          <div>Rent</div><div>¥50,000</div>
          <div>Maintenance Fee</div><div>¥5,000</div>
          <div>Current Status</div><div>Immediately</div>
        </body></html>
    """
    ref = ListingRef(
        "wagaya",
        "chiba-city",
        "https://wagaya-japan.com/en/chintai_detail.php?id=3",
        "fingerprint",
    )

    record = normalize_records(ref, "wagaya Japan", document)[0]

    assert record.city == "Chiba City Inage Ward"
    assert record.housing_type == "unknown"
    assert record.private_space is True
    assert "1bds" in record.source_evidence


@pytest.mark.parametrize(
    ("title_text", "location", "expected"),
    (
        ("Leopalace Hill Top Ichibankan - Kanagawa | wagaya", "Kanagawa Yokohama-shi", "Kanagawa"),
        ("Residence Nakanosakaue - Tokyo | wagaya", "Tokyo Nakano-ku", "Tokyo"),
    ),
)
def test_prefecture_names_must_be_complete_tokens(
    title_text: str,
    location: str,
    expected: str,
) -> None:
    document = f"""
        <html><head><title>{title_text}</title></head><body>
          <h1>Property Details</h1>
          <div>Location</div><div>{location}</div>
          <div>Layout</div><div>1K</div>
          <div>Rent</div><div>50,000 Yen</div>
          <div>Maintenance Fee</div><div>5,000 Yen</div>
          <div>Current Status</div><div>Vacant</div>
        </body></html>
    """
    ref = ListingRef(
        "wagaya",
        "prefecture-token",
        "https://wagaya-japan.com/en/chintai_detail.php?id=token",
        "fingerprint",
        latitude=35.45 if expected == "Kanagawa" else 35.7,
        longitude=139.62 if expected == "Kanagawa" else 139.68,
    )

    record = normalize_records(ref, "wagaya Japan", document)[0]

    assert record.prefecture == expected
    assert record.latitude is not None
    assert record.coordinate_basis == "provider_list_geo"


def test_best_estate_explicit_layout_and_brokerage_percentage_are_preserved(
    app_config: AppConfig,
) -> None:
    document = """
        <html><head>
          <title>2K Apartment For Rent in Saitama Kawaguchishi - Best-Estate</title>
          <meta name="x-property-rent" content="47000">
        </head><body>
          <div>Maintenance Fee</div><div>0 Yen</div>
          <div>Deposit</div><div>- Yen</div>
          <div>Key Money</div><div>- Yen</div>
          <div>Room Type</div><div>2K</div>
          <div>Home Insurance</div><div>Required</div>
          <div>Occupancy Date</div><div>Immediately</div>
          <div>Other expenses</div><div>仲介手数料家賃の55％。</div>
          <div>Guarantor Company</div>
          <div>Subscription required; Initial Guarantee fee 30%~100% of rent.</div>
        </body></html>
    """
    ref = ListingRef(
        "best_estate",
        "broker-fee",
        "https://www.best-estate.jp/en/property/3/",
        "fingerprint",
    )

    record = normalize_records(ref, "Best-Estate.jp", document)[0]
    outcome = classify(record, app_config.profile, app_config.ranking)

    assert record.housing_type == "2K"
    assert record.initial_cost_yen == 25_850
    assert "variable guarantor costs" in record.unknown_costs
    assert "home insurance amount" in record.unknown_costs
    assert outcome.name == "excluded"
    assert "unsupported housing type: 2K" in outcome.reasons


@pytest.mark.parametrize(
    ("source_id", "label", "document"),
    (
        (
            "best_estate",
            "Best-Estate.jp",
            """
                <html><head><title>1K Tokyo - Best-Estate</title>
                <meta name="x-property-rent" content="50000"></head><body>
                <div>Maintenance Fee: ¥5,000</div><div>Available now</div>
                </body></html>
            """,
        ),
        (
            "wagaya",
            "wagaya Japan",
            """
                <html><head><title>1K Tokyo | wagaya Japan</title></head><body>
                <h1>Property Details</h1><div>Location: Tokyo Suginami-ku</div>
                <div>Layout: 1K</div><div>Rent: ¥50,000</div>
                <div>Maintenance Fee: ¥5,000</div><div>Current Status: Vacant</div>
                </body></html>
            """,
        ),
    ),
)
def test_missing_initial_fee_evidence_is_not_reported_as_zero(
    source_id: str,
    label: str,
    document: str,
) -> None:
    ref = ListingRef(
        source_id,
        "missing-fees",
        f"https://example.invalid/{source_id}/missing-fees",
        "fingerprint",
    )

    record = normalize_records(ref, label, document)[0]

    assert record.initial_cost_yen is None
    assert "complete initial cost" in record.unknown_costs


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


def test_oakhouse_station_evidence_is_scoped_to_access_lines(project_root: Path) -> None:
    document = (project_root / "tests" / "fixtures" / "oakhouse.html").read_text(encoding="utf-8")
    document = document.replace(
        "<p>Tokyo Itabashi</p>",
        "<nav>Search by Train Line or Station</nav>"
        "<p>The house is conveniently close to the station</p>"
        "<p>Tokyo Itabashi</p>",
    )
    ref = ListingRef("oakhouse", "1", "https://www.oakhouse.jp/eng/house/1", "fingerprint")

    record = normalize_records(ref, "Oakhouse", document)[0]

    assert record.nearest_stations == ("Oyama Station",)
    assert record.walk_minutes_min == 10


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
        latitude=app_config.ranking.anchor_latitude + 0.1,
        longitude=app_config.ranking.anchor_longitude,
    )
    pricier_near = replace(
        base,
        external_id="pricey-near",
        recurring_total_yen=75_000,
        latitude=app_config.ranking.anchor_latitude,
        longitude=app_config.ranking.anchor_longitude,
    )

    cheap_outcome = classify(
        cheaper_farther,
        app_config.profile,
        app_config.ranking,
        commute=_commute(app_config, 60, 60),
        routing=app_config.routing,
    )
    near_outcome = classify(
        pricier_near,
        app_config.profile,
        app_config.ranking,
        commute=_commute(app_config, 20, 20),
        routing=app_config.routing,
    )

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
    assert outcome.priority_distance_component == 15.0
    assert outcome.priority_transit_component == 40.0
    assert outcome.priority_bicycle_component == 20.0
    assert outcome.name == "unverified"
    assert any("30 km penalty" in caution for caution in outcome.cautions)


def test_policy_hash_is_stable_for_unordered_profile_sets(app_config: AppConfig) -> None:
    reordered = replace(
        app_config.profile,
        allowed_housing_types=frozenset(["1K", "private_room", "1R"]),
    )

    assert policy_config_hash(reordered, app_config.ranking) == policy_config_hash(
        app_config.profile, app_config.ranking
    )
