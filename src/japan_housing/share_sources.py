"""Room-scoped parsers for direct operators and the Tokyo Sharehouse portal."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from .html import title, visible_text
from .models import HousingRecord, ListingRef


def _amount(pattern: str, text: str) -> int | None:
    match = re.search(pattern, text, re.I)
    return int(match.group(1).replace(",", "")) if match else None


def parse_share_source(ref: ListingRef, label: str, document: str) -> list[HousingRecord]:
    # Imported here to keep the existing parser helpers the single implementation.
    from .parsing import _coordinates, _date, _prefecture, _record, _structured_objects

    text = visible_text(document)
    name = title(document).split(":TOKYO")[0].split(" | ")[0]
    prefecture = _prefecture(text[:6000])
    lat, lon, basis = _coordinates(ref, document, prefecture)
    common: dict[str, Any] = dict(
        property_name=name,
        prefecture=prefecture,
        latitude=lat,
        longitude=lon,
        coordinate_basis=basis,
        housing_type="private_room",
        contract_type="monthly",
        metrics_evidence={
            "provenance": {"source_url": ref.url, "scope": "property", "kind": "published"}
        },
    )
    result = []
    if ref.source_id == "modern_living":
        for data in _structured_objects(document):
            if "Room" not in str(data.get("@type")):
                continue
            offer = data.get("offers", {})
            if not isinstance(offer, dict) or offer.get("url", data.get("url")) != ref.url:
                continue
            address = data.get("address", {})
            unit = re.search(r"[-—]\s*([\w-]+)$", str(data.get("name", "")))
            if not unit:
                continue
            rent = offer.get("price")
            if not isinstance(rent, int) or rent <= 0:
                continue
            available = str(offer.get("availability", "")).endswith("InStock")
            availability_date = _date(str(offer.get("availabilityStarts", "")))
            area = data.get("floorSize", {}).get("value")
            common.update(
                prefecture=_prefecture(str(address.get("addressRegion", ""))),
                city=str(address.get("addressLocality", "")),
            )
            result.append(
                _record(
                    ref,
                    label,
                    document,
                    **common,
                    title=str(data["name"]),
                    unit_name=unit.group(1),
                    rent_yen=rent,
                    room_area_m2=float(area) if isinstance(area, int | float) else None,
                    private_space=True,
                    furnished=True,
                    recurring_total_yen=None,
                    advertised_monthly_from_yen=rent,
                    price_basis="room_base_rent",
                    price_validation="mandatory_fees_unverified",
                    unknown_costs=("mandatory monthly fees", "initial cost", "exit cost"),
                    available_from=availability_date,
                    available_text="Available now"
                    if available
                    else f"Occupied / tentative {availability_date or 'unknown'}",
                    source_availability_status="vacancy" if available else "occupied",
                    availability_confidence="available_now" if available else "unknown",
                    foreigner_evidence=(
                        "Operator publishes accommodation for international residents",
                    ),
                    guarantor_evidence=("No guarantor required",)
                    if "No guarantor required" in text
                    else (),
                )
            )
    elif ref.source_id == "gg_house":
        admin = _amount(r"Administrative fee\s*([\d,]+)\s*JPY", text)
        departure_fee = _amount(r"Cancellation fee\s*([\d,]+)\s*JPY", text)
        zero_deposit = bool(re.search(r"Key money / Deposit\s*0month / 0month", text))
        city = re.search(r"\b([A-Za-z-]+-ku)\b", text)
        common.update(city=city.group(1) if city else "", prefecture=prefecture or "Tokyo")
        for body in re.findall(r'<li>\s*(<div class="vr-head">.*?)</li>', document, re.S):
            room = re.search(r'class="room-no">([^<]+)', body)
            if not room:
                continue
            row = visible_text(body)
            rent = _amount(r"Rent\s*([\d,]+)yen", row)
            fee = _amount(r"Common fee\s*([\d,]+)yen", row)
            total = _amount(r"Monthly total\s*([\d,]+)yen", row)
            if rent is None or fee is None or total != rent + fee:
                continue
            when = _date(row)
            available = "Available" in row or "Vacant soon" in row
            common["address"] = (
                re.search(r"Street address\n([^\n]+)", text).group(1)
                if re.search(r"Street address\n([^\n]+)", text)
                else ""
            )
            result.append(
                _record(
                    ref,
                    label,
                    document,
                    **common,
                    external_id=f"{ref.external_id}:{room.group(1)}",
                    title=f"{name} · {room.group(1)}",
                    unit_name=room.group(1),
                    private_space=True,
                    furnished=True,
                    desk="Desk" in text,
                    rent_yen=rent,
                    management_fee_yen=fee,
                    mandatory_monthly_fees_yen=0,
                    recurring_total_yen=total,
                    initial_cost_yen=admin,
                    exit_cost_yen=departure_fee,
                    deposit_yen=0 if zero_deposit else None,
                    refundable_deposit_yen=0 if zero_deposit else None,
                    unknown_costs=()
                    if admin is not None and departure_fee is not None and zero_deposit
                    else ("complete initial and departure costs",),
                    price_basis="available_room",
                    price_validation="matched",
                    available_from=when,
                    available_text=f"Available from {when}" if when else row[:120],
                    availability_confidence="explicit" if when and available else "unknown",
                    source_availability_status="vacancy" if available else "unknown",
                    # The page offers short stays with a surcharge; do not interpret the
                    # one-year fixed term as a hard one-year minimum.
                    minimum_stay_months=None,
                    early_exit_before_months=3,
                    early_exit_penalty_months=1,
                    showers=_amount(r"(\d+) shower rooms", text),
                    toilets=_amount(r"(\d+) toilets", text),
                    occupancy_restrictions=("age_18_39",) if "18-39 years" in text else (),
                    foreigner_evidence=("Operator publishes multilingual application information",),
                    source_evidence=(
                        row,
                        "Published fixed-term agreement; confirm registration, applicant "
                        "screening and notice",
                    ),
                )
            )
    elif ref.source_id == "tokyo_sharehouse":
        minimum = _amount(r"Min Contract\s*(\d+)\s*Months", text)
        common["address"] = (
            re.search(r"Area\n([^\n]+)", text).group(1)
            if re.search(r"Area\n([^\n]+)", text)
            else ""
        )
        common["city"] = (
            re.search(r"([\w-]+-ku)", common["address"]).group(1)
            if re.search(r"([\w-]+-ku)", common["address"])
            else ""
        )
        for body in re.split(r'<div class="room-item">', document)[1:]:
            body = body.split("<!-- /class:room -->", 1)[0]
            room = re.search(r'class="room-num">([^<]+)', body)
            if not room:
                continue
            row = visible_text(body)
            rent = _amount(r"Rent\s*[￥¥\\]\s*([\d,]+)", row)
            fee = _amount(r"Utility Fee\s*[￥¥\\]\s*([\d,]+)", row)
            area = re.search(r"Space:\s*(\d+(?:\.\d+)?)", row)
            available = "btnAvailable_eng" in body
            when = _date(row)
            result.append(
                _record(
                    ref,
                    label,
                    document,
                    **common,
                    external_id=f"{ref.external_id}:{room.group(1)}",
                    title=f"{name} · {room.group(1)}",
                    unit_name=room.group(1),
                    room_area_m2=float(area.group(1)) if area else None,
                    floor=_amount(r"\b(\d+)F\b", row),
                    private_space="Private" in row,
                    furnished="bed-on" in body,
                    desk="desk-on" in body,
                    rent_yen=rent,
                    management_fee_yen=fee,
                    recurring_total_yen=rent + fee
                    if rent is not None and fee is not None
                    else None,
                    unknown_costs=(
                        "initial fee and deposit wording requires reconciliation",
                        "exit cost",
                    ),
                    minimum_stay_months=minimum,
                    price_basis="room_components",
                    price_validation="published_components",
                    available_from=when,
                    available_text="Available now"
                    if available
                    else f"Available from {when}"
                    if when
                    else "Room availability unverified",
                    availability_confidence="available_now"
                    if available
                    else "explicit"
                    if when
                    else "unknown",
                    source_availability_status="vacancy"
                    if available
                    else "soon"
                    if when
                    else "unknown",
                    foreigner_evidence=("Non-Japanese application requirements published",)
                    if "Non-Japanese" in text
                    else (),
                    source_evidence=(row[:700], "Portal fee fields require operator confirmation"),
                )
            )
    return result


def gg_campaign(document: str, ref: ListingRef, record: HousingRecord) -> dict[str, Any]:
    """Require the current published September campaign text and eligible IDs."""
    text = visible_text(document)
    # The source explicitly includes IDs with L/J/K/F; aliases such as G21/K13 count.
    eligible_id = bool(re.search(r"\b[LJKF]\d+\b", record.property_name, re.I))
    if not eligible_id or "2026" not in text or "Minimum stay of 3 months" not in text:
        return {}
    if "September 30" not in text or "30,000" not in text or "16,500" not in text:
        return {}
    return {
        "verified": False,
        "source_url": ref.url,
        "observed_at": datetime.now().astimezone().isoformat(),
        "move_in_from": "2026-09-01",
        "move_in_until": "2026-09-30",
        "minimum_months": 3,
        "waived_initial_yen": 30000,
        "free_rent_months": 1,
        # The ordinary ¥16,500 cancellation charge is already in exit_cost_yen.
        "early_exit_fixed_yen": 0,
        "early_exit_recurring_months": 0,
        "eligibility_note": (
            "Confirm new customer, no existing reservation, eligible room and exact dates"
        ),
    }


SOURCE_CATALOG = (
    {
        "id": "modern_living",
        "label": "Modern Living",
        "url": "https://modernlivingjapan.com/en/properties",
        "mode": "automated",
        "purpose": "Room inventory; full fee quote still required",
    },
    {
        "id": "gg_house",
        "label": "GG House / Tokyo β",
        "url": "https://gghouse.co.jp/en/",
        "mode": "automated",
        "purpose": "Room inventory, fees and dated campaign evidence",
    },
    {
        "id": "tokyo_sharehouse",
        "label": "Tokyo Sharehouse",
        "url": "https://tokyosharehouse.com/eng/",
        "mode": "automated",
        "purpose": "Room-level portal discovery; reconcile operator terms",
    },
    {
        "id": "shares",
        "label": "Shares",
        "url": "https://shares.house/",
        "mode": "reviewed_import",
        "purpose": (
            "Robots check unavailable/disallowed during audit; import reviewed room evidence"
        ),
    },
    {
        "id": "xross",
        "label": "XROSS HOUSE",
        "url": "https://x-house.co.jp/en/",
        "mode": "reviewed_import",
        "purpose": "Import exact-room quote including departure fees",
    },
    {
        "id": "hituji",
        "label": "Hituji",
        "url": "https://www.hituji.jp/",
        "mode": "reviewed_import",
        "purpose": "Independent operator and facility discovery",
    },
    {
        "id": "freshroom",
        "label": "FRESHROOM",
        "url": "https://www.freshroom.jp/",
        "mode": "reviewed_import",
        "purpose": "Direct operator room evidence",
    },
    {
        "id": "navitime",
        "label": "NAVITIME",
        "url": "https://api-sdk.navitime.co.jp/api/",
        "mode": "routing",
        "purpose": "Japan transit, fares and transfers; provider credentials required",
    },
    {
        "id": "ekispert",
        "label": "Ekispert",
        "url": "https://ekispert.jp/products/api",
        "mode": "reviewed_import",
        "purpose": "Alternative transit and commuter-pass quotes",
    },
    {
        "id": "gsi",
        "label": "Official hazard maps",
        "url": "https://disaportal.gsi.go.jp/",
        "mode": "reference",
        "purpose": "Address-specific hazard evidence; verify floor and map date",
    },
)
