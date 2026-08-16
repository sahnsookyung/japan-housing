"""Source-aware normalization into conservative housing records."""

from __future__ import annotations

import hashlib
import html as html_module
import json
import re
from datetime import date

from .html import clean_text, evidence_hash, meta, money, title, visible_text
from .geography import validate_coordinates
from .models import HOUSING_RECORD_SCHEMA_VERSION, HousingRecord, ListingRef, iso_now

PREFECTURES = (
    "Hokkaido",
    "Aomori",
    "Iwate",
    "Miyagi",
    "Akita",
    "Yamagata",
    "Fukushima",
    "Ibaraki",
    "Tochigi",
    "Gunma",
    "Saitama",
    "Chiba",
    "Tokyo",
    "Kanagawa",
    "Niigata",
    "Toyama",
    "Ishikawa",
    "Fukui",
    "Yamanashi",
    "Nagano",
    "Gifu",
    "Shizuoka",
    "Aichi",
    "Mie",
    "Shiga",
    "Kyoto",
    "Osaka",
    "Hyogo",
    "Nara",
    "Wakayama",
    "Tottori",
    "Shimane",
    "Okayama",
    "Hiroshima",
    "Yamaguchi",
    "Tokushima",
    "Kagawa",
    "Ehime",
    "Kochi",
    "Fukuoka",
    "Saga",
    "Nagasaki",
    "Kumamoto",
    "Oita",
    "Miyazaki",
    "Kagoshima",
    "Okinawa",
)


def _first(patterns: tuple[str, ...], value: str) -> str:
    for pattern in patterns:
        match = re.search(pattern, value, re.I | re.S)
        if match:
            return clean_text(match.group(1))
    return ""


def _amount_after(label: str, text: str) -> int | None:
    found = _first((rf"{re.escape(label)}\s*[:：]?\s*((?:¥|￥)?\s*[\d,]+)",), text)
    return money(found) if found else None


def _date(value: str) -> str | None:
    for pattern in (
        r"(20\d{2})[./年-](\d{1,2})[./月-](\d{1,2})",
        r"(20\d{2})/(\d{1,2})/(\d{1,2})",
    ):
        match = re.search(pattern, value)
        if not match:
            continue
        try:
            return date(*(int(part) for part in match.groups())).isoformat()
        except ValueError:
            continue
    return None


def _prefecture(value: str) -> str:
    lowered = value.casefold()
    matches = (
        (lowered.find(prefecture.casefold()), prefecture)
        for prefecture in PREFECTURES
        if prefecture.casefold() in lowered
    )
    return min(matches, default=(-1, ""))[1]


def _availability_confidence(value: str) -> str:
    if _date(value):
        return "explicit"
    if re.search(r"\b(?:vacant|available now|immediate(?:ly)?)\b", value, re.I):
        return "available_now"
    return "unknown"


def _structured_objects(document: str) -> list[dict[str, object]]:
    objects: list[dict[str, object]] = []

    def collect(value: object) -> None:
        if isinstance(value, dict):
            objects.append(value)
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    pattern = r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>'
    for match in re.finditer(pattern, document, re.I | re.S):
        try:
            collect(json.loads(html_module.unescape(match.group(1))))
        except (json.JSONDecodeError, TypeError):
            continue
    return objects


def _coordinate(value: object, minimum: float, maximum: float) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if minimum <= result <= maximum else None


def _raw_coordinates(ref: ListingRef, document: str) -> tuple[float | None, float | None, str]:
    """Extract provider-published coordinates without calling a geocoder."""

    for item in _structured_objects(document):
        geo = item.get("geo") if isinstance(item.get("geo"), dict) else item
        if not isinstance(geo, dict):
            continue
        latitude = _coordinate(geo.get("latitude"), 20, 46)
        longitude = _coordinate(geo.get("longitude"), 122, 154)
        if latitude is not None and longitude is not None:
            return latitude, longitude, "provider_detail_structured_geo"

    map_match = re.search(
        r"(?:google\.[^/\"']+/maps\?(?:[^\"']*&)?q=|maps[^\"']*?[?&]q=)"
        r"(-?\d{1,2}(?:\.\d+)?)(?:,|%2C)(-?\d{1,3}(?:\.\d+)?)",
        document,
        re.I,
    )
    if map_match:
        latitude = _coordinate(map_match.group(1), 20, 46)
        longitude = _coordinate(map_match.group(2), 122, 154)
        if latitude is not None and longitude is not None:
            return latitude, longitude, "provider_detail_map_link"

    json_match = re.search(
        r'["\']lat(?:itude)?["\']\s*:\s*["\']?(-?\d{1,2}(?:\.\d+)?)["\']?'
        r'.{0,200}?["\'](?:lng|longitude)["\']\s*:\s*["\']?'
        r'(-?\d{1,3}(?:\.\d+)?)["\']?',
        document,
        re.I | re.S,
    )
    if json_match:
        latitude = _coordinate(json_match.group(1), 20, 46)
        longitude = _coordinate(json_match.group(2), 122, 154)
        if latitude is not None and longitude is not None:
            return latitude, longitude, "provider_detail_embedded_geo"

    latitude = _coordinate(ref.latitude, 20, 46)
    longitude = _coordinate(ref.longitude, 122, 154)
    if latitude is not None and longitude is not None:
        return latitude, longitude, "provider_list_geo"
    return None, None, "unknown"


def _coordinates(
    ref: ListingRef,
    document: str,
    prefecture: str,
) -> tuple[float | None, float | None, str]:
    latitude, longitude, basis = _raw_coordinates(ref, document)
    return validate_coordinates(latitude, longitude, basis, prefecture)


def _schema_type(item: dict[str, object], expected: str) -> bool:
    value = item.get("@type")
    if isinstance(value, list):
        return expected in value
    return value == expected


def _restrictions(text: str) -> tuple[str, ...]:
    result = []
    if re.search(r"\b(?:female|women)[ -]?only\b|女性(?:限定|専用)", text, re.I):
        result.append("female_only")
    if re.search(r"\b(?:male|men)[ -]?only\b|男性(?:限定|専用)", text, re.I):
        result.append("male_only")
    return tuple(result)


def _housing_type(value: str) -> str:
    folded = value.casefold()
    if "share house" in folded or "sharehouse" in folded or "single room" in folded:
        return "private_room"
    if re.search(r"\b1k\b", folded):
        return "1K"
    if re.search(r"\b1r\b", folded):
        return "1R"
    return "unknown"


def _cluster(address: str, property_name: str, unit: str, rent: int | None) -> str:
    value = "|".join(
        re.sub(r"\W+", "", part.casefold())
        for part in (address, property_name, unit, str(rent or ""))
    )
    return hashlib.sha256(value.encode()).hexdigest()[:24] if value.strip("|") else ""


def _record(
    ref: ListingRef,
    label: str,
    document: str,
    *,
    external_id: str | None = None,
    **values: object,
) -> HousingRecord:
    record = HousingRecord(
        schema_version=HOUSING_RECORD_SCHEMA_VERSION,
        source_id=ref.source_id,
        source_label=label,
        external_id=external_id or ref.external_id,
        url=ref.url,
        title=str(values.pop("title", "")) or title(document),
        list_fingerprint=ref.list_fingerprint,
        observed_at=iso_now(),
        detail_verified_at=iso_now(),
        raw_evidence_hash=evidence_hash(document),
        **values,
    )
    record.cluster_key = _cluster(
        record.address, record.property_name or record.title, record.unit_name, record.rent_yen
    )
    return record


def _oakhouse_advertised_monthly_from(document: str, row_start: int) -> int | None:
    prefix = document[:row_start]
    descriptions = list(
        re.finditer(
            r'<p[^>]*class=["\'][^"\']*\bp-room__common__description\b[^"\']*["\'][^>]*>'
            r"(.*?)</p>",
            prefix,
            re.I | re.S,
        )
    )
    if not descriptions:
        return None
    description = visible_text(descriptions[-1].group(1))
    rent_range = description.split("Contract fee", 1)[0]
    advertised_rents = [
        value
        for token in re.findall(r"(?:¥|￥)\s*[\d,]+", rent_range)
        if (value := money(token)) is not None
    ]
    maintenance = _amount_after("Maintenance fee(to be paid monthly)", description)
    if not advertised_rents or maintenance is None:
        return None
    return min(advertised_rents) + maintenance


def _oakhouse_room_price(
    room_text: str, attr: dict[str, str]
) -> tuple[int | None, int | None, int | None, str, tuple[str, ...]]:
    rent = _amount_after("Rent", room_text)
    management = _amount_after("Maintenance fee", room_text)
    row_total = int(attr["data-sort_price"]) if attr.get("data-sort_price", "").isdigit() else None
    component_total = rent + management if rent is not None and management is not None else None
    if component_total is not None and row_total is not None:
        if component_total == row_total:
            return rent, management, component_total, "matched", ()
        return (
            rent,
            management,
            max(component_total, row_total),
            "mismatch_conservative_max",
            ("available-room total mismatch",),
        )
    if component_total is not None:
        return rent, management, component_total, "components_only", ()
    if row_total is not None:
        return (
            rent,
            management,
            row_total,
            "row_total_only",
            ("available-room price components",),
        )
    return rent, management, None, "incomplete", ("available-room recurring total",)


def _best_estate(ref: ListingRef, label: str, document: str) -> list[HousingRecord]:
    text = visible_text(document)
    page_title = title(document).split(" - Best-Estate", 1)[0]
    apartment = next(
        (item for item in _structured_objects(document) if _schema_type(item, "Apartment")),
        {},
    )
    address_data = apartment.get("address") if isinstance(apartment, dict) else None
    address_data = address_data if isinstance(address_data, dict) else {}
    offers = apartment.get("offers") if isinstance(apartment, dict) else None
    offers = offers if isinstance(offers, dict) else {}
    layout = meta(document, "x-property-layout") or _first(
        (r"(?:Layout|Room Type)\s*([^\n]+)",), text
    )
    structured_rent = offers.get("price")
    rent = (
        int(structured_rent)
        if isinstance(structured_rent, int | float) and 0 <= structured_rent <= 100_000_000
        else money(meta(document, "x-property-rent")) or _amount_after("Rent", text)
    )
    management = _amount_after("Maintenance Fee", text)
    recurring = rent + management if rent is not None and management is not None else None
    address_parts = tuple(
        str(address_data.get(name) or "")
        for name in ("addressRegion", "addressLocality", "streetAddress")
    )
    address = " ".join(filter(None, address_parts))
    prefecture = _prefecture(address or page_title + " " + text[:4000])
    city = str(address_data.get("addressLocality") or "") or _first(
        (rf"{prefecture}\s+([^\n]+?-(?:ku|shi))", r"Location\s*([^\n]+)"), text
    )
    available_text = _first(
        (r"(?:Occupancy Date|Available from|Move[- ]?in date)\s*([^\n]+)",), text
    )
    if not available_text and str(offers.get("availability") or "").endswith("InStock"):
        available_text = "Available now"
    housing_type = _housing_type(f"{page_title} {layout}")
    unknown = []
    if management is None:
        unknown.append("management fee")
    initial = _amount_after("Initial Cost Estimate", text)
    if initial is None:
        unknown.append("complete initial cost")
    monthly = "Monthly Apartment" in text
    latitude, longitude, coordinate_basis = _coordinates(ref, document, prefecture)
    return [
        _record(
            ref,
            label,
            document,
            title=page_title,
            property_name=str(apartment.get("name") or "")
            or _first((r"\n([^\n]+)\nRent/Initial cost",), text),
            address=address,
            prefecture=prefecture,
            city=city,
            latitude=latitude,
            longitude=longitude,
            coordinate_basis=coordinate_basis,
            housing_type=housing_type,
            private_space=(
                True if housing_type in {"private_room", "1R", "1K"} or bool(apartment) else None
            ),
            furnished=bool(re.search(r"Furnished|With Appliances", text, re.I)) or None,
            rent_yen=rent,
            management_fee_yen=management,
            recurring_total_yen=recurring,
            initial_cost_yen=initial,
            unknown_costs=tuple(unknown),
            available_from=_date(available_text),
            available_text=available_text,
            availability_confidence=_availability_confidence(available_text),
            contract_type="monthly" if monthly else "standard",
            minimum_stay_days=30 if monthly else None,
            foreigner_evidence=("Best-Estate foreign-resident inventory",),
            overseas_application_evidence=("Provider supports applications from overseas",),
            registration_evidence=("Residential listing; confirm registration with GTN",),
            registration_confidence="probable",
            guarantor_evidence=("GTN guarantee support available",),
            occupancy_restrictions=_restrictions(text),
            source_evidence=tuple(filter(None, (layout, available_text))),
        )
    ]


def _wagaya(ref: ListingRef, label: str, document: str) -> list[HousingRecord]:
    text = visible_text(document)
    if "property details" not in text.casefold() and "apartment infomation" not in text.casefold():
        return []
    page_title = title(document).split(" | wagaya", 1)[0]
    rent = _amount_after("Rent", text)
    management = _amount_after("Maintenance Fee", text)
    recurring = rent + management if rent is not None and management is not None else None
    available_text = _first((r"(?:Available from|Current Status)\s*([^\n]+)",), text)
    layout = _first((r"Layout\s*([^\n]+)",), text)
    address = _first((r"Location\s*([^\n]+)", r"^([^\n]+?-(?:ku|shi)[^\n]*)"), text)
    prefecture = _prefecture(page_title + " " + address + " " + text[:2500])
    guarantee = _first((r"(?:Guarantor Fee description|Guarantor)\s*([^\n]+)",), text)
    housing_type = _housing_type(layout + " " + text[:2000])
    initial = _amount_after("Initial Cost Estimate", text)
    unknown = []
    if management is None:
        unknown.append("management fee")
    if initial is None:
        unknown.append("complete initial cost")
    monthly = "Monthly Rental" in text
    latitude, longitude, coordinate_basis = _coordinates(ref, document, prefecture)
    return [
        _record(
            ref,
            label,
            document,
            title=page_title,
            property_name=page_title.split(" - ", 1)[0],
            address=address,
            prefecture=prefecture,
            city=_first((r"Location\s*([^\n,]+)",), text),
            latitude=latitude,
            longitude=longitude,
            coordinate_basis=coordinate_basis,
            nearest_stations=tuple(dict.fromkeys(re.findall(r"([^\n]{1,60}?Sta\.)", text, re.I)))[
                :3
            ],
            walk_minutes_min=(
                int(match.group(1))
                if (match := re.search(r"(\d+)\s*(?:min|minutes?)\s*walk", text, re.I))
                else None
            ),
            housing_type=housing_type,
            private_space=True if housing_type != "unknown" else None,
            furnished=bool(re.search(r"Fully furnished|Furniture", text, re.I)) or None,
            rent_yen=rent,
            management_fee_yen=management,
            recurring_total_yen=recurring,
            initial_cost_yen=initial,
            unknown_costs=tuple(unknown),
            available_from=_date(available_text),
            available_text=available_text,
            availability_confidence=_availability_confidence(available_text),
            contract_type="monthly" if monthly else "standard",
            minimum_stay_days=30 if monthly else None,
            foreigner_evidence=("wagaya foreign-resident listing",),
            overseas_application_evidence=(
                ("Online consultation and overseas application may be available",)
                if "currently residing in japan" in text.casefold()
                else ()
            ),
            registration_evidence=("Confirm address registration with the listed agent",),
            registration_confidence="unknown",
            guarantor_evidence=(guarantee,) if guarantee else (),
            occupancy_restrictions=_restrictions(text),
            source_evidence=tuple(filter(None, (layout, available_text, guarantee))),
        )
    ]


def _oakhouse(ref: ListingRef, label: str, document: str) -> list[HousingRecord]:
    text = visible_text(document)
    full_title = title(document)
    page_title = full_title.split(" | ", 1)[0]
    prefecture = _prefecture(full_title) or _prefecture(text[:2500])
    city = _first((rf"{prefecture}\s+([^|\n]+)",), full_title)
    stations = tuple(dict.fromkeys(re.findall(r"([^\n]{1,60}?(?:Station|Sta\.))", text, re.I)))[:3]
    latitude, longitude, coordinate_basis = _coordinates(ref, document, prefecture)
    records: list[HousingRecord] = []
    row_pattern = r'<tr\s+([^>]*class="p-room__caset__row"[^>]*)>(.*?)</tr>'
    for match in re.finditer(row_pattern, document, re.I | re.S):
        attrs, body = match.groups()
        attr = {key.lower(): value for key, value in re.findall(r'([\w-]+)="([^"]*)"', attrs)}
        source_status = attr.get("data-status", "")
        if source_status not in {"vacancy", "soon"}:
            continue
        room_id = attr.get("id") or f"room-{len(records) + 1}"
        room_text = visible_text(body)
        available_text = _first((r"(20\d{2}/\d{1,2}/\d{1,2}\s*~)", r"(Available[^\n]+)"), room_text)
        rent, management, monthly, price_validation, unknown_costs = _oakhouse_room_price(
            room_text, attr
        )
        advertised = _oakhouse_advertised_monthly_from(document, match.start())
        initial = (
            int(attr["data-sort_contract"])
            if attr.get("data-sort_contract", "").isdigit()
            else None
        )
        restrictions = []
        if "Woman Only" in room_text:
            restrictions.append("female_only")
        if "Man Only" in room_text:
            restrictions.append("male_only")
        room_type = attr.get("data-type")
        housing_type = _housing_type(room_text)
        if housing_type == "unknown" and room_type == "single":
            housing_type = "private_room"
        availability_confidence = (
            "explicit"
            if available_text
            else "available_now"
            if source_status == "vacancy"
            else "unknown"
        )
        availability_label = available_text or (
            "Available now" if source_status == "vacancy" else "Vacancy soon; date unverified"
        )
        records.append(
            _record(
                ref,
                label,
                document,
                external_id=f"{ref.external_id}:{room_id}",
                title=f"{page_title} · Room {room_id}",
                property_name=page_title,
                unit_name=room_id,
                prefecture=prefecture,
                city=city,
                latitude=latitude,
                longitude=longitude,
                coordinate_basis=coordinate_basis,
                nearest_stations=stations,
                walk_minutes_min=(
                    int(walk.group(1))
                    if (walk := re.search(r"(\d+)\s*(?:min|minutes?)", text, re.I))
                    else None
                ),
                housing_type=housing_type,
                private_space=True if room_type in {"single", "apartment"} else None,
                furnished="Furnished" in room_text or attr.get("data-furniture") == "0",
                rent_yen=rent,
                management_fee_yen=management,
                mandatory_monthly_fees_yen=0,
                recurring_total_yen=monthly,
                advertised_monthly_from_yen=advertised,
                price_basis="available_room",
                price_validation=price_validation,
                initial_cost_yen=initial,
                exit_cost_yen=0,
                unknown_costs=unknown_costs,
                available_from=_date(available_text),
                available_text=availability_label,
                availability_confidence=availability_confidence,
                source_availability_status=source_status,
                minimum_stay_days=30,
                contract_type="monthly",
                foreigner_evidence=("Foreign Residents Allowed",),
                visa_evidence=("Oakhouse accepts any valid visa type",),
                overseas_application_evidence=("Online move-in application available",),
                registration_evidence=("Oakhouse share-house address registration supported",),
                registration_confidence="confirmed",
                guarantor_evidence=("No guarantor or guarantor company required",),
                occupancy_restrictions=tuple(restrictions),
                source_evidence=tuple(
                    filter(
                        None,
                        (
                            room_text[:400],
                            f"room status: {source_status}",
                            availability_label,
                            (f"available-room total ¥{monthly:,}" if monthly is not None else ""),
                            (f"advertised from ¥{advertised:,}" if advertised is not None else ""),
                        ),
                    )
                ),
            )
        )
    return records


def normalize_records(ref: ListingRef, label: str, document: str) -> list[HousingRecord]:
    if ref.source_id == "best_estate":
        return _best_estate(ref, label, document)
    if ref.source_id == "wagaya":
        return _wagaya(ref, label, document)
    if ref.source_id == "oakhouse":
        return _oakhouse(ref, label, document)
    raise ValueError(f"unsupported source parser: {ref.source_id}")
