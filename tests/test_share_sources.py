import json

from japan_housing.models import ListingRef
from japan_housing.parsing import normalize_records
from japan_housing.share_sources import gg_campaign


def test_modern_living_uses_exact_unit_geo_not_agency_office():
    url = "https://modernlivingjapan.com/en/properties/test-104"
    data = [
        {"@type": "RealEstateAgent", "geo": {"latitude": 35.1, "longitude": 139.1}},
        {
            "@type": ["Accommodation", "Room"],
            "name": "Test building — 104",
            "url": url,
            "geo": {"latitude": 35.7, "longitude": 139.7},
            "address": {"addressRegion": "Tokyo", "addressLocality": "Nakano"},
            "floorSize": {"value": 9.02},
            "offers": {
                "url": url,
                "price": 58000,
                "availability": "https://schema.org/OutOfStock",
                "availabilityStarts": "2026-12-21",
            },
        },
    ]
    html = (
        '<title>Test building</title><script type="application/ld+json">'
        + json.dumps(data)
        + "</script>"
    )
    ref = ListingRef("modern_living", "test-104", url, "")
    record = normalize_records(ref, "Modern Living", html)[0]
    assert (record.latitude, record.longitude) == (35.7, 139.7)
    assert record.source_availability_status == "occupied"
    assert record.room_area_m2 == 9.02 and record.recurring_total_yen is None
    data[1]["@type"] = ["Accommodation", "Apartment"]
    data[1]["description"] = "A furnished 1K apartment"
    html = '<title>Test</title><script type="application/ld+json">' + json.dumps(data) + "</script>"
    assert normalize_records(ref, "Modern Living", html)[0].housing_type == "1K"


def test_gg_room_totals_are_reconciled_and_campaign_is_conditional():
    def row(room, total):
        return (
            f'<li><div class="vr-head"><span class="room-no">{room}</span>'
            "<p>Rent 33,000yen</p><p>Common fee 16,500yen</p>"
            f"<p>Monthly total {total}yen</p><p>Available 2026/09/11</p></div></li>"
        )

    html = (
        "<title>G21 / K13 Sample House</title>"
        "<script>const LAT = 35.7743; const LNG = 139.762;</script>"
        "<p>Administrative fee 30,000 JPY</p>"
        "<p>Cancellation fee 16,500 JPY</p><p>Key money / Deposit 0month / 0month</p>"
        + row("101", "49,500")
        + row("102", "10,000")
    )
    ref = ListingRef("gg_house", "g21", "https://gghouse.co.jp/en/g21/", "")
    records = normalize_records(ref, "GG House", html)
    assert records[0].latitude == 35.7743
    assert len(records) == 1 and records[0].external_id == "g21:101"
    assert records[0].recurring_total_yen == 49500 and records[0].deposit_yen == 0
    assert records[0].minimum_stay_months is None
    campaign = gg_campaign(
        "2026 Minimum stay of 3 months September 30 30,000 16,500", ref, records[0]
    )
    assert campaign["verified"] is False and campaign["minimum_months"] == 3
    assert campaign["early_exit_fixed_yen"] == 0


def test_tokyo_sharehouse_preserves_room_area_desk_off_and_fee_conflict():
    html = """<title>Test House:TOKYO SHAREHOUSE</title>
    <p>Area</p><p>Concept</p><p>Minimum stay 1 month</p>
    <p>OUTLINE</p><p>MAP</p><p>VIEW</p><p>Area</p>
    <p>5-48-3 Takinogawa, Kita-ku, Tokyo</p>
    <div class="room-item"><span class="room-num">207</span><p>Private 2F</p>
    <p>Rent ¥35,000</p><p>Utility Fee ¥15,000</p><p>Space: 4.25</p>
    <i class="desk-off">Desk</i><i class="bed-on">Bed</i><img src="btnAvailable_eng.png">
    <!-- /class:room -->"""
    record = normalize_records(
        ListingRef(
            "tokyo_sharehouse", "2608", "https://tokyosharehouse.com/eng/house/detail/2608/", ""
        ),
        "Tokyo Sharehouse",
        html,
    )[0]
    assert record.address == "5-48-3 Takinogawa, Kita-ku, Tokyo"
    assert record.room_area_m2 == 4.25 and record.desk is False
    assert record.recurring_total_yen == 50000
    assert record.initial_cost_yen is None and record.unknown_costs
    assert record.external_id == "2608:207"
