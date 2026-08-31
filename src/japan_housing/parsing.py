"""Source-aware normalization into conservative housing records."""

from __future__ import annotations

import hashlib
import html as html_module
import json
import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .geography import validate_coordinates
from .html import clean_text, evidence_hash, meta, money, title, visible_text
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

OAKHOUSE_ROOM_ROW_PATTERN = re.compile(
    r'<tr\s+([^>]*class=["\'][^"\']*\bp-room__caset__row\b[^"\']*["\'][^>]*)>(.*?)</tr>',
    re.I | re.S,
)


def _first(patterns: tuple[str, ...], value: str) -> str:
    for pattern in patterns:
        match = re.search(pattern, value, re.I | re.S)
        if match:
            return clean_text(match.group(1))
    return ""


def _amount_after(label: str, text: str) -> int | None:
    found = _first(
        (
            rf"{re.escape(label)}\s*[:：]?\s*(?:Total\s*)?"
            r"((?:¥|￥)?\s*[\d,]+(?:\s*(?:Yen|円))?)",
        ),
        text,
    )
    return money(found) if found else None


def _label_value(label: str, text: str) -> str:
    """Return the first meaningful same-line or following-line value for a label."""

    folded_label = label.casefold()
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = clean_text(line)
        folded = stripped.casefold()
        if folded == folded_label:
            if index + 1 < len(lines):
                return clean_text(lines[index + 1])
            continue
        for separator in (":", "："):
            prefix = f"{folded_label}{separator}"
            if folded.startswith(prefix):
                return clean_text(stripped[len(label) + 1 :])
    return ""


def _rent_multiple(value: str, rent_yen: int | None) -> int | None:
    match = re.search(r"([\d.]+)\s*months?\b", value, re.I)
    if not match or rent_yen is None:
        return None
    try:
        months = Decimal(match.group(1))
    except InvalidOperation:
        return None
    if months < 0:
        return None
    return int((months * rent_yen).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _label_amount(label: str, text: str, *, rent_yen: int | None = None) -> int | None:
    """Parse a label expressed as yen or as a multiple of base rent."""

    value = _label_value(label, text)
    if not value:
        return None
    if re.search(r"(?:¥|￥|\bYen\b|円)", value, re.I):
        return money(value)
    return _rent_multiple(value, rent_yen)


def _section_after(label: str, text: str) -> str:
    index = text.casefold().find(label.casefold())
    return text[index:] if index >= 0 else ""


def _explicit_fee(text: str, patterns: tuple[str, ...]) -> int | None:
    amount = r"((?:(?:¥|￥)\s*[\d,]+|[\d,]+\s*(?:Yen|円)))"
    for pattern in patterns:
        match = re.search(rf"(?:{pattern})\s*[:：]?\s*{amount}", text, re.I)
        if match and (value := money(match.group(1))) is not None:
            return value
    return None


def _monthly_detail_fees(text: str) -> int:
    """Sum distinct, explicitly monthly fees not already represented by rent/maintenance."""

    fees: dict[str, int] = {}
    pattern = re.compile(
        r"([A-Za-z][A-Za-z /-]{1,48}?fee)\s*[:：]?\s*"
        r"(?:¥|￥)?\s*([\d,]+)\s*(?:Yen|円)?\s*\(monthly\)",
        re.I,
    )
    for label, raw_amount in pattern.findall(text):
        normalized = " ".join(label.casefold().split())
        if any(word in normalized for word in ("rent", "maintenance", "guarantee")):
            continue
        amount = money(raw_amount)
        if amount is not None:
            fees[normalized] = amount
    return sum(fees.values())


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


def _oakhouse_room_rows(document: str) -> list[tuple[dict[str, str], str, int]]:
    rows: list[tuple[dict[str, str], str, int]] = []
    for match in OAKHOUSE_ROOM_ROW_PATTERN.finditer(document):
        raw_attributes, body = match.groups()
        attributes = {
            key.lower(): value
            for key, _, value in re.findall(r"([\w-]+)\s*=\s*([\"'])(.*?)\2", raw_attributes, re.S)
        }
        rows.append((attributes, body, match.start()))
    return rows


def oakhouse_snapshot_verified(document: str, parsed_rooms: int) -> bool:
    """Confirm that every advertised available-room row was understood by the parser."""

    rows = _oakhouse_room_rows(document)
    if rows:
        statuses = [attributes.get("data-status", "") for attributes, _, _ in rows]
        known_statuses = {"vacancy", "soon", "novacancy"}
        if not all(status in known_statuses for status in statuses):
            return False
        available_rows = sum(status in {"vacancy", "soon"} for status in statuses)
        available_markers = len(
            re.findall(
                r"\bdata-status\s*=\s*([\"'])(?:vacancy|soon)\1",
                document,
                re.I,
            )
        )
        return parsed_rooms == available_rows == available_markers
    if parsed_rooms:
        return False
    text = visible_text(document)
    return bool(
        re.search(
            r"\b(?:no (?:vacant|available) rooms?|no vacancies|currently fully occupied)\b"
            r"|空室.{0,8}(?:なし|ございません)",
            text,
            re.I,
        )
    )


def oakhouse_verified_empty(document: str) -> bool:
    """Return true only when the page explicitly proves that no room is available."""

    return oakhouse_snapshot_verified(document, 0)


def _best_estate_costs(
    text: str,
    rent_yen: int | None,
) -> tuple[int | None, int | None, tuple[str, ...], str]:
    """Return known one-time entry/exit costs without guessing variable charges."""

    explicit_initial = _label_amount("Initial Cost Estimate", text)
    if explicit_initial is None:
        explicit_initial = _label_amount("Initial Cost", text)
    deposit = _label_amount("Deposit", text, rent_yen=rent_yen)
    key_money = _label_amount("Key Money", text, rent_yen=rent_yen)
    security = _label_amount("Security Deposit", text, rent_yen=rent_yen)
    move_in_admin = _explicit_fee(
        text,
        (
            r"入居時事務手数料",
            r"(?<!再)契約事務手数料",
            r"move[- ]?in (?:administrative|admin) fee",
            r"contract (?:administrative|admin) fee",
        ),
    )
    home_insurance = _label_value("Home Insurance", text)
    insurance_amount = money(home_insurance) if home_insurance else None
    guarantee_amount = _explicit_fee(
        text,
        (r"initial guarantee fee", r"initial guarantor fee"),
    )
    exit_cost = _explicit_fee(
        text,
        (
            r"退去時(?:清掃|クリーニング)費用?",
            r"move[- ]?out (?:cleaning )?fee",
            r"exit cleaning fee",
        ),
    )

    known_components = [
        deposit if deposit is not None else security,
        key_money,
        move_in_admin,
        insurance_amount,
        guarantee_amount,
    ]
    has_initial_evidence = explicit_initial is not None or any(
        value is not None for value in known_components
    )
    known_initial = sum(value for value in known_components if value is not None)
    initial = (
        explicit_initial
        if explicit_initial is not None
        else known_initial
        if has_initial_evidence
        else None
    )
    unknown: list[str] = []
    if explicit_initial is None:
        if deposit is None and security is None:
            unknown.append("deposit")
        if key_money is None:
            unknown.append("key money")
        guarantee_text = _section_after("Guarantor Company", text)[:700]
        if (
            guarantee_text
            and not re.search(r"(?:not required|不要)", guarantee_text, re.I)
            and (guarantee_amount is None or re.search(r"%|~|〜", guarantee_text))
        ):
            unknown.append("variable guarantor costs")
        if (
            home_insurance
            and not re.search(r"(?:not required|不要|none|-)$", home_insurance, re.I)
            and insurance_amount is None
        ):
            unknown.append("home insurance amount")
        if unknown:
            unknown.append("complete initial cost")
    if exit_cost is None:
        unknown.append("exit cost")
    validation = "source_total" if explicit_initial is not None else "known_components"
    return initial, exit_cost, tuple(dict.fromkeys(unknown)), validation


def _wagaya_costs(
    text: str,
) -> tuple[
    int | None,
    int | None,
    int,
    int | None,
    int | None,
    tuple[str, ...],
    str,
]:
    """Parse wagaya's calculator while excluding first-month rent from one-time costs."""

    estimate = _section_after("Initial Cost Estimate", text)
    estimate_rent = _label_amount("Rent", estimate)
    estimate_management = _label_amount("Maintenance Fee", estimate)
    rent = estimate_rent if estimate_rent is not None else _amount_after("Rent", text)
    management = (
        estimate_management
        if estimate_management is not None
        else _amount_after("Maintenance Fee", text)
    )
    monthly_fees = _monthly_detail_fees(text)
    estimate_total = _amount_after("Initial Cost Estimate", text)
    deposit = _label_amount("Deposit", estimate, rent_yen=rent)
    key_money = _label_amount("Key Money", estimate, rent_yen=rent)
    agent_fee = _label_amount("Agent Fee", estimate, rent_yen=rent)
    if agent_fee is None:
        agent_fee = _label_amount("Agent Fee", text, rent_yen=rent)
    standalone_initial = _label_amount("Initial costs", text, rent_yen=rent)

    breakdown_initial = sum(value for value in (deposit, key_money, agent_fee) if value is not None)
    estimate_initial = None
    if estimate_total is not None and estimate_rent is not None and estimate_management is not None:
        estimate_initial = max(0, estimate_total - rent - management)
    elif estimate_total is not None:
        estimate_initial = estimate_total
    included_initial = max(breakdown_initial, estimate_initial or 0)
    if standalone_initial is not None:
        expected_with_standalone = (
            (rent or 0) + (management or 0) + breakdown_initial + standalone_initial
        )
        if estimate_total is None or estimate_total < expected_with_standalone:
            included_initial += standalone_initial

    insurance_value = _label_value("Renter's Insurance", text)
    insurance_amount = money(insurance_value) if insurance_value else None
    guarantee_value = _label_value("Initial Guarantee Fee", text)
    guarantee_amount = money(guarantee_value) if guarantee_value else None
    included_initial += sum(
        value for value in (insurance_amount, guarantee_amount) if value is not None
    )
    has_initial_evidence = any(
        value is not None
        for value in (
            estimate_total,
            deposit,
            key_money,
            agent_fee,
            standalone_initial,
            insurance_amount,
            guarantee_amount,
        )
    )
    initial = included_initial if has_initial_evidence else None

    exit_cost = _explicit_fee(
        text,
        (
            r"退去時(?:清掃|クリーニング)費用?",
            r"move[- ]?out (?:cleaning )?fee",
            r"exit cleaning fee",
        ),
    )
    unknown: list[str] = []
    if estimate_total is None:
        unknown.append("complete initial cost")
    if "approximate estimate" in estimate.casefold():
        unknown.append("complete initial cost")
    if (
        insurance_value
        and re.search(r"required", insurance_value, re.I)
        and insurance_amount is None
    ):
        unknown.extend(("renter's insurance amount", "complete initial cost"))
    if (
        guarantee_value
        and not re.search(r"^(?:-|none|not required)$", guarantee_value, re.I)
        and guarantee_amount is None
    ):
        unknown.extend(("initial guarantee fee", "complete initial cost"))
    if exit_cost is None:
        unknown.append("exit cost")

    validation = "estimate_components_known_floor"
    if (
        estimate_total is not None
        and rent is not None
        and management is not None
        and estimate_total == rent + management + breakdown_initial
    ):
        validation = "estimate_components_matched_known_floor"
    return (
        rent,
        management,
        monthly_fees,
        initial,
        exit_cost,
        tuple(dict.fromkeys(unknown)),
        validation,
    )


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
    unknown: list[str] = []
    if management is None:
        unknown.append("management fee")
    initial, exit_cost, cost_unknown, price_validation = _best_estate_costs(text, rent)
    unknown.extend(cost_unknown)
    monthly = bool(re.search(r"\bMonthly Apartment\b", text, re.I))
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
            mandatory_monthly_fees_yen=0,
            recurring_total_yen=recurring,
            price_basis="detail_components",
            price_validation=price_validation,
            initial_cost_yen=initial,
            exit_cost_yen=exit_cost,
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
    (
        rent,
        management,
        mandatory_monthly,
        initial,
        exit_cost,
        cost_unknown,
        price_validation,
    ) = _wagaya_costs(text)
    recurring = (
        rent + management + mandatory_monthly
        if rent is not None and management is not None
        else None
    )
    available_text = _first((r"(?:Available from|Current Status)\s*([^\n]+)",), text)
    layout = _first((r"Layout\s*([^\n]+)",), text)
    address = _first((r"Location\s*([^\n]+)", r"^([^\n]+?-(?:ku|shi)[^\n]*)"), text)
    prefecture = _prefecture(page_title + " " + address + " " + text[:2500])
    guarantee = _first((r"(?:Guarantor Fee description|Guarantor)\s*([^\n]+)",), text)
    housing_type = _housing_type(layout + " " + text[:2000])
    unknown: list[str] = []
    if management is None:
        unknown.append("management fee")
    unknown.extend(cost_unknown)
    detail_text = _section_after("Property Details", text) or _section_after(
        "Apartment Infomation", text
    )
    monthly = bool(
        re.search(r"\bMonthly Rental\b", page_title, re.I)
        or re.search(r"\bMonthly Rental\b(?!\s+Property)", detail_text, re.I)
    )
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
            mandatory_monthly_fees_yen=mandatory_monthly,
            recurring_total_yen=recurring,
            price_basis="detail_components",
            price_validation=price_validation,
            initial_cost_yen=initial,
            exit_cost_yen=exit_cost,
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
    for attr, body, row_start in _oakhouse_room_rows(document):
        source_status = attr.get("data-status", "")
        if source_status not in {"vacancy", "soon"}:
            continue
        room_id = attr.get("id") or f"room-{len(records) + 1}"
        room_text = visible_text(body)
        available_text = _first((r"(20\d{2}/\d{1,2}/\d{1,2}\s*~)", r"(Available[^\n]+)"), room_text)
        rent, management, monthly, price_validation, unknown_costs = _oakhouse_room_price(
            room_text, attr
        )
        advertised = _oakhouse_advertised_monthly_from(document, row_start)
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
