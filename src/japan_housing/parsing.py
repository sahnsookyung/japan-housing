"""Source-aware normalization into conservative housing records."""

from __future__ import annotations

import hashlib
import html as html_module
import json
import re
from calendar import monthrange
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .geography import validate_coordinates
from .html import clean_text, evidence_hash, meta, money, title, visible_text
from .models import HousingRecord, ListingRef, expected_housing_record_schema_version, iso_now

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
PREFECTURE_BY_CASEFOLD = {prefecture.casefold(): prefecture for prefecture in PREFECTURES}
PREFECTURE_PATTERN = re.compile(
    rf"(?<![A-Za-z])({'|'.join(map(re.escape, PREFECTURES))})(?![A-Za-z])",
    re.I,
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


def _definition_value(label: str, document: str) -> str | None:
    """Read one HTML definition-list value while preserving an explicitly empty value."""

    match = re.search(
        rf"<dt[^>]*>\s*{re.escape(label)}\s*</dt>\s*<dd[^>]*>(.*?)</dd>",
        document,
        re.I | re.S,
    )
    if not match:
        return None
    return clean_text(re.sub(r"<[^>]+>", " ", match.group(1)))


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
    if re.fullmatch(r"(?:-|—|none|not required|no)(?:\s*(?:Yen|円))?", value, re.I):
        return 0
    if re.search(r"(?:¥|￥|\bYen\b|円)", value, re.I):
        return money(value)
    return _rent_multiple(value, rent_yen)


def _section_after(label: str, text: str) -> str:
    index = text.casefold().find(label.casefold())
    return text[index:] if index >= 0 else ""


def _explicit_fee(text: str, patterns: tuple[str, ...]) -> int | None:
    amount = r"((?:(?:¥|￥)\s*[\d,]+|[\d,]+\s*(?:Yen|円)))"
    for pattern in patterns:
        match = re.search(
            rf"(?:{pattern})\s*[:：]?\s*(?:of\s+)?(?:[（(]\s*)?{amount}",
            text,
            re.I,
        )
        if match and (value := money(match.group(1))) is not None:
            return value
    return None


def _plain_labeled_amount(text: str, patterns: tuple[str, ...]) -> int | None:
    """Parse a provider-labeled integer even when the page omits the currency suffix."""

    for pattern in patterns:
        match = re.search(rf"(?:{pattern})\s*[:：]?\s*([\d,]+)(?!\s*[%％])", text, re.I)
        if match and (value := money(match.group(1))) is not None:
            return value
    return None


def _monthly_detail_fees(text: str) -> int:
    """Sum distinct, explicitly monthly fees not already represented by rent/maintenance."""

    fees: dict[str, int] = {}
    pattern = re.compile(
        r"([A-Za-z][A-Za-z /-]{1,48}?fee)\s*[:：]?\s*"
        r"(?:¥|￥)?\s*([\d,]+)\s*(?:Yen|円)?\s*(?:\(monthly\)|per month)",
        re.I,
    )
    for label, raw_amount in pattern.findall(text):
        normalized = " ".join(label.casefold().split())
        tokens = set(normalized.replace("/", " ").split())
        if normalized in {"rent fee", "maintenance fee", "management fee"} or (
            "guarantee" in tokens
        ):
            continue
        amount = money(raw_amount)
        if amount is not None:
            fees[normalized] = amount
    return sum(fees.values())


def _date(value: str) -> str | None:
    normalized = re.sub(r"^[\s:：]+", "", value)
    for pattern in (
        r"(20\d{2})[./年-](\d{1,2})[./月-](\d{1,2})",
        r"(20\d{2})/(\d{1,2})/(\d{1,2})",
    ):
        match = re.search(pattern, normalized)
        if not match:
            continue
        try:
            return date(*(int(part) for part in match.groups())).isoformat()
        except ValueError:
            continue
    approximate = re.search(
        r"(20\d{2})[./-](\d{1,2})\s*-?\s*(Early|Mid(?:-term|dle)?|Late)",
        normalized,
        re.I,
    )
    if approximate:
        year, month = (int(part) for part in approximate.groups()[:2])
        period = approximate.group(3).casefold()
        day = (
            10
            if period == "early"
            else 20
            if period.startswith("mid")
            else monthrange(year, month)[1]
        )
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None
    month_only = re.fullmatch(r"\s*(20\d{2})[./-](\d{1,2})\s*", normalized)
    if month_only:
        year, month = (int(part) for part in month_only.groups())
        try:
            return date(year, month, monthrange(year, month)[1]).isoformat()
        except ValueError:
            return None
    return None


def _prefecture(value: str) -> str:
    match = PREFECTURE_PATTERN.search(value)
    return PREFECTURE_BY_CASEFOLD.get(match.group(1).casefold(), "") if match else ""


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
    if re.search(r"\b(?:1\s*)?studio\b", folded):
        return "1R"
    layout = re.search(r"(?<![a-z0-9])(\d+(?:ldk|dk|k|r))(?![a-z0-9])", folded)
    if layout:
        return layout.group(1).upper()
    return "unknown"


def _wagaya_japanese_layout(document: str) -> str:
    match = re.search(
        r"<dt[^>]*>\s*間取り\s*</dt>\s*<dd[^>]*>(.*?)</dd>",
        document,
        re.I | re.S,
    )
    if match:
        return clean_text(re.sub(r"<[^>]+>", " ", match.group(1)))
    return _label_value("間取り", visible_text(document))


def _city_from_address(address: str, prefecture: str) -> str:
    without_prefecture = re.sub(
        rf"\b{re.escape(prefecture)}\s+Prefecture\b", "", address, flags=re.I
    )
    if without_prefecture == address:
        without_prefecture = re.sub(
            rf"^\s*{re.escape(prefecture)}\b", "", address, count=1, flags=re.I
        )
    for pattern in (
        r"\b([A-Za-z][A-Za-z -]+? City(?: [A-Za-z-]+ Ward)?)\b",
        r"\b([A-Za-z][A-Za-z -]+?-(?:ku|shi|cho|machi))\b",
    ):
        match = re.search(pattern, without_prefecture, re.I)
        if match:
            city = clean_text(match.group(1))
            if city.casefold().endswith(" city"):
                prior = without_prefecture[: match.start()]
                ward = re.search(
                    r"([A-Za-z][A-Za-z -]+ Ward|[A-Za-z][A-Za-z -]+-ku)\s*,?\s*$", prior, re.I
                )
                if ward:
                    city = f"{city} {clean_text(ward.group(1))}"
            return city
    segments = [
        clean_text(segment) for segment in without_prefecture.split(",") if clean_text(segment)
    ]
    if not segments:
        return ""
    return next((segment for segment in segments if not re.match(r"^\d", segment)), segments[0])


def _rent_percentage(text: str, rent_yen: int | None, patterns: tuple[str, ...]) -> int | None:
    if rent_yen is None:
        return None
    for pattern in patterns:
        match = re.search(rf"(?:{pattern})[^\n。]{{0,50}}?([\d.]+)\s*[%％]", text, re.I)
        if not match:
            continue
        try:
            ratio = Decimal(match.group(1)) / 100
        except InvalidOperation:
            continue
        if 0 <= ratio <= 5:
            return int((ratio * rent_yen).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return None


def _rent_months(text: str, rent_yen: int | None, patterns: tuple[str, ...]) -> int | None:
    if rent_yen is None:
        return None
    for pattern in patterns:
        match = re.search(
            rf"(?:{pattern})[^\n。]{{0,30}}?([\d.]+)\s*(?:ヶ|ヵ|か|ケ|カ)?月(?:分)?",
            text,
            re.I,
        )
        if not match:
            continue
        try:
            months = Decimal(match.group(1))
        except InvalidOperation:
            continue
        if 0 <= months <= 24:
            return int((months * rent_yen).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return None


def _best_estate_monthly_fees(text: str, recurring_base_yen: int | None) -> int:
    """Return the explicit monthly lower bound for mandatory Best-Estate add-ons."""

    exact_guarantee = _rent_percentage(
        text,
        recurring_base_yen,
        (
            r"保証委託料[^\n。]{0,80}?月額",
            r"保証会社加入要[^\n。]{0,80}?月次(?:月額総額)?",
        ),
    )
    guarantee = exact_guarantee
    if guarantee is None:
        guarantee = _explicit_fee(
            text,
            (r"monthly guarantee fee", r"月額保証(?:委託)?料"),
        )
    smart_lock = _explicit_fee(text, (r"スマートロック利用料",))
    return (guarantee or 0) + (smart_lock or 0)


def _best_estate_other_initial_fees(text: str) -> int:
    """Sum distinct mandatory entry services with exact provider labels."""

    labels = (
        (r"エアコン(?:分解)?(?:清掃|洗浄)(?:費用?|代)?",),
        (r"(?:24|２４)\s*[HＨ](?:時間)?緊急サポート(?:費用?|代)?",),
        (r"抗菌(?:施工|処理)(?:費用?|代)?",),
    )
    return sum(_plain_labeled_amount(text, patterns) or 0 for patterns in labels)


def _cluster(address: str, property_name: str, unit: str, rent: int | None) -> str:
    # Rent changes do not create a new physical room. Do not collapse buildings
    # without a published unit number into a guessed shared identity.
    if not unit or not (address or property_name):
        return ""
    value = "|".join(re.sub(r"\W+", "", part.casefold()) for part in (address, property_name, unit))
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
        schema_version=expected_housing_record_schema_version(ref.source_id),
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


def source_verified_empty(source_id: str, document: str) -> bool:
    """Recognize only provider-explicit closure evidence for a detail page."""

    if source_id == "oakhouse":
        return oakhouse_verified_empty(document)
    if source_id == "wagaya":
        return bool(re.search(r"\bthe\s+post\s+has\s+been\s+removed\b", document, re.I))
    return False


def _best_estate_costs(
    text: str,
    rent_yen: int | None,
    recurring_base_yen: int | None,
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
    guarantee_amount = _rent_percentage(
        text,
        recurring_base_yen,
        (
            r"保証委託料[^\n。]{0,80}?初回額",
            r"保証会社加入要[^\n。]{0,80}?初回(?:月額総額)?",
        ),
    )
    if guarantee_amount is None:
        guarantee_amount = _explicit_fee(
            text,
            (
                r"initial guarantee fee",
                r"initial guarantor fee",
                r"minimum guarantee fee",
            ),
        )
    brokerage_fee = next(
        (
            amount
            for label in ("Brokerage Fee", "Broker Fee", "Agent Fee")
            if (amount := _label_amount(label, text, rent_yen=rent_yen)) is not None
        ),
        None,
    )
    if brokerage_fee is None:
        brokerage_fee = _rent_percentage(
            text,
            rent_yen,
            (r"仲介手数料(?:家賃(?:の)?)?", r"brokerage fee", r"broker fee", r"agent fee"),
        )
    if brokerage_fee is None:
        brokerage_fee = _rent_months(
            text,
            rent_yen,
            (r"仲介手数料(?:家賃(?:の)?)?", r"brokerage fee", r"broker fee", r"agent fee"),
        )
    other_initial = _best_estate_other_initial_fees(text)
    exit_cost = _explicit_fee(
        text,
        (
            r"退去時(?:清掃|クリーニング)費用?",
            r"move[- ]?out (?:cleaning )?fee",
            r"cleaning fee (?:upon|at) move[- ]?out",
            r"exit cleaning fee",
        ),
    )

    known_components = [
        deposit if deposit is not None else security,
        key_money,
        move_in_admin,
        insurance_amount,
        guarantee_amount,
        brokerage_fee,
        other_initial or None,
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
        brokerage_text = bool(
            re.search(r"仲介手数料|\b(?:brokerage|broker|agent) fee\b", text, re.I)
        )
        if brokerage_text and brokerage_fee is None:
            unknown.append("brokerage fee")
        if re.search(r"スマモル賃貸プラン未加入の場合", text):
            unknown.append("variable smart-lock service fees")
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
    key_replacement_fee = _explicit_fee(text, (r"key replacement fee",))

    breakdown_initial = sum(value for value in (deposit, key_money, agent_fee) if value is not None)
    estimate_initial = None
    if estimate_total is not None and estimate_rent is not None and estimate_management is not None:
        estimate_initial = max(0, estimate_total - rent - management)
    elif estimate_total is not None:
        estimate_initial = estimate_total
    included_initial = max(breakdown_initial, estimate_initial or 0)
    additional_initial = sum(
        value for value in (standalone_initial, key_replacement_fee) if value is not None
    )
    if additional_initial:
        expected_with_additional = (
            (rent or 0) + (management or 0) + breakdown_initial + additional_initial
        )
        if estimate_total is None or estimate_total < expected_with_additional:
            included_initial += additional_initial

    recurring_before_guarantee = (
        rent + management + monthly_fees if rent is not None and management is not None else None
    )
    monthly_guarantee = _rent_percentage(
        text,
        recurring_before_guarantee,
        (r"monthly\s+guarantee\s+(?:commission\s+)?fee",),
    )
    if monthly_guarantee is None:
        monthly_guarantee = _explicit_fee(text, (r"monthly\s+guarantee\s+fee",))
    monthly_fees += monthly_guarantee or 0

    insurance_value = _label_value("Renter's Insurance", text)
    insurance_amount = (
        money(insurance_value)
        if insurance_value and re.search(r"(?:¥|￥|\bYen\b|円)", insurance_value, re.I)
        else None
    )
    guarantee_value = _label_value("Initial Guarantee Fee", text)
    guarantee_amount = (
        money(guarantee_value)
        if guarantee_value and re.search(r"(?:¥|￥|\bYen\b|円)", guarantee_value, re.I)
        else _rent_percentage(
            f"Initial Guarantee Fee {guarantee_value}",
            recurring_before_guarantee,
            (r"initial\s+guarantee\s+fee",),
        )
    )
    if guarantee_amount is None:
        guarantee_amount = _explicit_fee(text, (r"initial\s+guarantee\s+fee",))
    tenant_support_fee = _label_amount("Tenant support fee", text)
    room_cleaning_fee = _label_amount("Room cleaning fee", text)
    key_set_fee = _explicit_fee(text, (r"key set fee",))
    extra_contract_fees = tuple(
        value
        for value in (
            insurance_amount,
            guarantee_amount,
            tenant_support_fee,
            room_cleaning_fee,
            key_set_fee,
        )
        if value is not None
    )
    if extra_contract_fees:
        expected_with_contract_fees = (
            (rent or 0)
            + (management or 0)
            + breakdown_initial
            + additional_initial
            + sum(extra_contract_fees)
        )
        if estimate_total is None or estimate_total < expected_with_contract_fees:
            included_initial += sum(extra_contract_fees)
    has_initial_evidence = any(
        value is not None
        for value in (
            estimate_total,
            deposit,
            key_money,
            agent_fee,
            standalone_initial,
            key_replacement_fee,
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
            r"cleaning fee (?:upon|at) move[- ]?out",
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
    guarantee_description = _label_value("Guarantor Fee description", text) or _label_value(
        "Guarantor", text
    )
    guarantee_required = bool(
        re.search(
            r"(?:required|condition of the contract|obligation to join)",
            guarantee_description,
            re.I,
        )
    )
    if guarantee_amount is None and (
        (guarantee_value and not re.search(r"^(?:-|none|not required)$", guarantee_value, re.I))
        or guarantee_required
    ):
        unknown.extend(("initial guarantee fee", "complete initial cost"))
    if re.search(r"\bguarantee deposit\s*:\s*required\b", text, re.I):
        unknown.extend(("guarantee deposit amount", "complete initial cost"))
    if re.search(r"\binternet usage fee\s*:\s*(?:required|mandatory)\b", text, re.I):
        unknown.append("internet usage fee amount")
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
    management = _label_amount("Maintenance Fee", text)
    recurring_base = rent + management if rent is not None and management is not None else None
    mandatory_monthly = _best_estate_monthly_fees(text, recurring_base)
    recurring = recurring_base + mandatory_monthly if recurring_base is not None else None
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
    initial, exit_cost, cost_unknown, price_validation = _best_estate_costs(
        text, rent, recurring_base
    )
    unknown.extend(cost_unknown)
    monthly = bool(re.search(r"\bMonthly Apartment\b", text, re.I))
    provider_updated_at = _date(_label_value("Last updated", text)) or ""
    provider_next_update_at = _date(_label_value("Next update date", text)) or ""
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
            mandatory_monthly_fees_yen=mandatory_monthly,
            recurring_total_yen=recurring,
            price_basis="detail_components",
            price_validation=price_validation,
            initial_cost_yen=initial,
            exit_cost_yen=exit_cost,
            deposit_yen=_label_amount("Deposit", text, rent_yen=rent),
            unknown_costs=tuple(unknown),
            available_from=_date(available_text),
            available_text=available_text,
            availability_confidence=_availability_confidence(available_text),
            contract_type="monthly" if monthly else "standard",
            minimum_stay_days=30 if monthly else None,
            foreigner_evidence=("Best-Estate foreign-resident inventory",),
            overseas_application_evidence=("Provider supports applications from overseas",),
            registration_evidence=(
                "Best-Estate foreign-renter guidance requires residence registration",
            ),
            registration_confidence="confirmed" if not monthly else "unknown",
            guarantor_evidence=("GTN guarantee support available",),
            occupancy_restrictions=_restrictions(text),
            source_evidence=tuple(filter(None, (layout, available_text))),
            provider_updated_at=provider_updated_at,
            provider_next_update_at=provider_next_update_at,
        )
    ]


def _wagaya(ref: ListingRef, label: str, document: str) -> list[HousingRecord]:
    text = visible_text(document)
    if "property details" not in text.casefold() and "apartment infomation" not in text.casefold():
        return []
    page_title = title(document).split(" | wagaya", 1)[0]
    detail_text = _section_after("Property Details", text) or _section_after(
        "Apartment Infomation", text
    )

    def detail_value(field: str) -> str:
        structured = _definition_value(field, document)
        return structured if structured is not None else _label_value(field, detail_text)

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
    available_date_text = detail_value("Available from")
    current_status = detail_value("Current Status")
    available_text = (
        available_date_text
        if available_date_text
        and not re.fullmatch(r"(?:-|none|please ask|need to confirm)", available_date_text, re.I)
        else current_status
    )
    source_availability_status = {
        "vacant": "vacancy",
        "moving in": "occupied",
        "will be vacated": "soon",
    }.get(current_status.casefold(), "unknown")
    layout = detail_value("Layout")
    title_address = page_title.split(" - ", 1)[1] if " - " in page_title else ""
    title_address = re.sub(r"\s*\[[^\]]+\]\s*$", "", title_address)
    address = _label_value("Location", detail_text) or title_address
    prefecture = _prefecture(page_title + " " + address + " " + text[:2500])
    city = _city_from_address(address, prefecture)
    guarantee = detail_value("Guarantor Fee description") or detail_value("Guarantor")
    property_type = detail_value("Type")
    explicit_share_house = bool(
        re.search(r"\b(?:share house|shared apartment)\b", f"{page_title} {property_type}", re.I)
    )
    housing_type = "private_room" if explicit_share_house else _housing_type(layout)
    unknown: list[str] = []
    if management is None:
        unknown.append("management fee")
    unknown.extend(cost_unknown)
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
            city=city,
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
            private_space=(
                True
                if housing_type != "unknown"
                or re.search(r"\b(?:apartment|mansion|detached house)\b", property_type, re.I)
                else None
            ),
            furnished=bool(re.search(r"Fully furnished|Furniture", text, re.I)) or None,
            rent_yen=rent,
            management_fee_yen=management,
            mandatory_monthly_fees_yen=mandatory_monthly,
            recurring_total_yen=recurring,
            price_basis="detail_components",
            price_validation=price_validation,
            initial_cost_yen=initial,
            deposit_yen=_label_amount(
                "Deposit", _section_after("Initial Cost Estimate", text), rent_yen=rent
            ),
            room_area_m2=(
                float(area.group(1))
                if (area := re.search(r"([\d.]+)\s*(?:m²|m2|㎡)", detail_value("Area")))
                else None
            ),
            exit_cost_yen=exit_cost,
            unknown_costs=tuple(unknown),
            available_from=_date(available_text),
            available_text=available_text,
            availability_confidence=_availability_confidence(available_text),
            source_availability_status=source_availability_status,
            contract_type="monthly" if monthly else "standard",
            minimum_stay_days=30 if monthly else None,
            foreigner_evidence=("wagaya foreign-resident listing",),
            overseas_application_evidence=(
                ("Online consultation and overseas application may be available",)
                if "currently residing in japan" in text.casefold()
                else ()
            ),
            registration_evidence=(
                ("Standard residential rental rather than monthly lodging",)
                if not monthly
                else ("Confirm address registration with the listed agent",)
            ),
            registration_confidence="standard_residential" if not monthly else "unknown",
            guarantor_evidence=(guarantee,) if guarantee else (),
            occupancy_restrictions=_restrictions(text),
            source_evidence=tuple(filter(None, (layout, available_text, guarantee))),
        )
    ]


def _oakhouse_access(text: str) -> tuple[tuple[str, ...], int | None]:
    """Extract station names only from explicit property-access evidence."""

    matches = re.findall(
        r"\b(\d+)\s*(?:min|minutes?)\s+walk\s+(?:to|from)\s+"
        r"([^\n]{1,80}?(?:Station|Sta\.))\b",
        text,
        re.I,
    )
    stations: list[str] = []
    walks: list[int] = []
    for walk_text, station_text in matches:
        station = re.sub(r"(?<!\s)(Station|Sta\.)$", r" \1", station_text.strip(), flags=re.I)
        station = re.sub(r"\s+", " ", station)
        if station not in stations:
            stations.append(station)
        walks.append(int(walk_text))
    return tuple(stations[:3]), min(walks) if walks else None


def _oakhouse(ref: ListingRef, label: str, document: str) -> list[HousingRecord]:
    text = visible_text(document)
    full_title = title(document)
    page_title = full_title.split(" | ", 1)[0]
    prefecture = _prefecture(full_title) or _prefecture(text[:2500])
    city = _first((rf"{prefecture}\s+([^|\n]+)",), full_title)
    stations, walk_minutes = _oakhouse_access(text)
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
                walk_minutes_min=walk_minutes,
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
                deposit_yen=0,
                refundable_deposit_yen=0,
                room_area_m2=(
                    float(area.group(1))
                    if (area := re.search(r"(\d+(?:\.\d+)?)\s*(?:m²|m2|㎡)", room_text))
                    else None
                ),
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


def normalize_records(
    ref: ListingRef,
    label: str,
    document: str,
    *,
    supplemental_document: str | None = None,
) -> list[HousingRecord]:
    # Wagaya's explicit removal marker is otherwise indistinguishable from a
    # valid detail-page shell. Oakhouse verifies empty inventory against its
    # room-table invariants inside the runner, so avoid parsing that table twice.
    if ref.source_id == "wagaya" and source_verified_empty(ref.source_id, document):
        return []
    if ref.source_id == "best_estate":
        return _best_estate(ref, label, document)
    if ref.source_id == "wagaya":
        records = _wagaya(ref, label, document)
        if supplemental_document:
            japanese_layout = _wagaya_japanese_layout(supplemental_document)
            housing_type = _housing_type(japanese_layout)
            if housing_type != "unknown":
                for record in records:
                    if record.housing_type != "unknown":
                        continue
                    record.housing_type = housing_type
                    record.private_space = True
                    record.source_evidence = (
                        *record.source_evidence,
                        f"Japanese layout: {japanese_layout}",
                    )
        return records
    if ref.source_id == "oakhouse":
        return _oakhouse(ref, label, document)
    if ref.source_id in {"modern_living", "gg_house", "tokyo_sharehouse"}:
        from .share_sources import parse_share_source

        return parse_share_source(ref, label, document, supplemental_document)
    raise ValueError(f"unsupported source parser: {ref.source_id}")
