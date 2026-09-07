"""Local, auditable room evidence and manually reviewed source imports."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .decision import RECORD_FIELDS
from .models import HousingRecord, iso_now

EDITABLE = RECORD_FIELDS - {
    "schema_version",
    "source_id",
    "source_label",
    "external_id",
    "url",
    "cluster_key",
    "raw_evidence_hash",
    "list_fingerprint",
    "observed_at",
    "detail_verified_at",
}
MONEY_FIELDS = {k for k in EDITABLE if k.endswith("_yen")}
BOOL_FIELDS = {
    "air_conditioning",
    "natural_light",
    "private_space",
    "furnished",
    "deposit_in_initial_cost",
    "desk",
    "extension_available",
}
INT_FIELDS = {
    "building_year",
    "minimum_stay_days",
    "maximum_stay_days",
    "minimum_stay_months",
    "notice_days",
    "upfront_rent_months",
    "early_exit_before_months",
    "residents",
    "showers",
    "toilets",
    "washers",
    "floor",
    "screening_days",
    "walk_minutes_min",
}
NUMERIC_FIELDS = {
    "internet_latency_ms",
    "night_noise_db",
    "flood_depth_m",
    "room_area_m2",
    "usable_area_m2",
    "internet_download_mbps",
    "internet_upload_mbps",
    "early_exit_penalty_months",
    "latitude",
    "longitude",
}


def _safe_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 2000:
        raise ValueError("source URL must be a short HTTPS URL")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("source URL must use HTTPS without credentials")
    return value


def validate_fields(values: object) -> dict[str, Any]:
    if not isinstance(values, dict) or set(values) - EDITABLE:
        raise ValueError("unsupported evidence fields")
    for key, value in values.items():
        if value is None:
            if key == "deposit_in_initial_cost":
                raise ValueError("deposit_in_initial_cost must be boolean")
            if key not in MONEY_FIELDS | INT_FIELDS | NUMERIC_FIELDS | BOOL_FIELDS | {
                "available_from"
            }:
                raise ValueError(f"{key} cannot be null")
            continue
        if key in MONEY_FIELDS | INT_FIELDS:
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100000000:
                raise ValueError(f"{key} must be a nonnegative integer")
        elif key in NUMERIC_FIELDS:
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
            ):
                raise ValueError(f"{key} must be finite")
            low, high = (
                (-85, 85)
                if key == "latitude"
                else (-180, 180)
                if key == "longitude"
                else (0, 100000)
            )
            if not low <= value <= high:
                raise ValueError(f"{key} is out of range")
        elif key in BOOL_FIELDS:
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be boolean")
        elif (
            key.endswith("_evidence")
            and key != "metrics_evidence"
            or key in {"unknown_costs", "nearest_stations", "occupancy_restrictions"}
        ):
            if (
                not isinstance(value, list)
                or len(value) > 100
                or any(not isinstance(v, str) or len(v) > 2000 for v in value)
            ):
                raise ValueError(f"{key} must be a list of short strings")
        elif key == "campaign":
            validate_campaign(value)
        elif key == "metrics_evidence":
            if not isinstance(value, dict) or len(json.dumps(value, allow_nan=False)) > 20000:
                raise ValueError("metric evidence must be a bounded object")
            validate_routes(value.get("routes", []))
        elif not isinstance(value, str) or len(value) > 4000:
            raise ValueError(f"{key} must be short text")
    if (
        values.get("refundable_deposit_yen") is not None
        and values.get("deposit_yen") is not None
        and values["refundable_deposit_yen"] > values["deposit_yen"]
    ):
        raise ValueError("deposit refund cannot exceed the deposit paid")
    bounds = {
        "minimum_stay_months": 1200,
        "early_exit_before_months": 1200,
        "upfront_rent_months": 36,
        "notice_days": 3650,
        "minimum_stay_days": 36500,
        "maximum_stay_days": 36500,
    }
    for key, high in bounds.items():
        if values.get(key) is not None and values[key] > high:
            raise ValueError(f"{key} exceeds the supported range")
    if values.get("billing_basis", "unknown") not in {"unknown", "calendar_daily", "anniversary"}:
        raise ValueError("billing basis is unsupported")
    return values


def validate_campaign(campaign: object) -> None:
    if not isinstance(campaign, dict):
        raise ValueError("campaign must be an object")
    if not campaign:
        return
    try:
        start = date.fromisoformat(campaign["move_in_from"])
        end = date.fromisoformat(campaign["move_in_until"])
        minimum = campaign["minimum_months"]
        if (
            end < start
            or isinstance(minimum, bool)
            or not isinstance(minimum, int)
            or not 1 <= minimum <= 36
        ):
            raise ValueError("campaign dates or minimum stay are invalid")
        if not isinstance(campaign.get("verified", False), bool):
            raise ValueError("campaign verified must be boolean")
        for key in (
            "waived_initial_yen",
            "free_rent_months",
            "early_exit_fixed_yen",
            "early_exit_recurring_months",
            "upfront_discount_yen",
        ):
            number = campaign.get(key, 0)
            if (
                isinstance(number, bool)
                or not isinstance(number, int | float)
                or not math.isfinite(number)
                or number < 0
                or number > 100000000
            ):
                raise ValueError("campaign amounts must be finite and nonnegative")
    except (KeyError, TypeError) as exc:
        raise ValueError("campaign requires move-in dates and minimum_months") from exc


def merge_route_history(existing: list, incoming: list) -> list:
    """Retain up to 60 endpoint/schedule observations so reverting pins can reuse them."""
    fields = (
        "destination_id",
        "destination_latitude",
        "destination_longitude",
        "origin_latitude",
        "origin_longitude",
        "schedule_key",
    )
    result, seen = [], set()
    for route in [*incoming, *existing]:
        key = json.dumps([route.get(field) for field in fields], sort_keys=True)
        if key not in seen:
            seen.add(key)
            result.append(route)
        if len(result) == 60:
            break
    return result


def validate_routes(routes: object) -> None:
    if not isinstance(routes, list) or len(routes) > 60:
        raise ValueError("routes must contain at most 60 entries")
    for route in routes:
        if not isinstance(route, dict):
            raise ValueError("route must be an object")
        required = (
            "destination_id",
            "destination_latitude",
            "destination_longitude",
            "origin_latitude",
            "origin_longitude",
            "minutes",
            "verified_at",
            "source_url",
        )
        if any(key not in route for key in required):
            raise ValueError("route requires exact endpoints, minutes, date and source URL")
        if "schedule_key" in route and (
            not isinstance(route["schedule_key"], str) or len(route["schedule_key"]) > 100
        ):
            raise ValueError("route schedule key is invalid")
        _safe_url(route["source_url"])
        if (
            not isinstance(route["destination_id"], str)
            or not 0 < len(route["destination_id"]) <= 80
        ):
            raise ValueError("route destination ID is invalid")
        minutes = route["minutes"]
        if (
            isinstance(minutes, bool)
            or not isinstance(minutes, int | float)
            or not 0 < minutes <= 1440
        ):
            raise ValueError("route minutes must be positive and at most 1440")
        for key in ("minutes", "fare_yen", "transfers", "walking_minutes"):
            value = route.get(key)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or not 0 <= value <= 1000000
            ):
                raise ValueError("route metric is invalid")
        for key in (
            "destination_latitude",
            "origin_latitude",
            "destination_longitude",
            "origin_longitude",
        ):
            bound = 85 if key.endswith("latitude") else 180
            value = route[key]
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or not -bound <= value <= bound
            ):
                raise ValueError("route coordinates are invalid")
        try:
            observed = date.fromisoformat(route["verified_at"][:10])
            if observed > date.today():
                raise ValueError("route verification cannot be in the future")
        except (TypeError, ValueError) as exc:
            raise ValueError("route verification date is invalid") from exc


class EvidenceStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=10)
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS decision_evidence (
                id INTEGER PRIMARY KEY, stable_key TEXT NOT NULL, payload TEXT NOT NULL,
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS decision_imports (
                stable_key TEXT PRIMARY KEY, record_json TEXT NOT NULL);
        """)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.connection.close()

    def revision(self) -> int:
        return self.connection.execute(
            "SELECT COALESCE(MAX(id),0) FROM decision_evidence"
        ).fetchone()[0]

    @staticmethod
    def prepare(payload: object) -> dict[str, Any]:
        if not isinstance(payload, dict) or len(json.dumps(payload)) > 50000:
            raise ValueError("evidence payload is invalid or too large")
        values = validate_fields(payload.get("values"))
        source = _safe_url(payload.get("source_url"))
        kind = payload.get("kind", "confirmed")
        if kind not in {"confirmed", "published", "estimate"}:
            raise ValueError("evidence kind must be confirmed, published or estimate")
        note = str(payload.get("note", ""))
        if len(note) > 4000:
            raise ValueError("evidence note is too long")
        item = {
            "values": values,
            "source_url": source,
            "kind": kind,
            "note": note,
            "observed_at": iso_now(),
            "scope": "room",
        }
        return item

    def add(self, key: str, payload: object) -> None:
        item = self.prepare(payload)
        with self.connection:
            self.connection.execute(
                "INSERT INTO decision_evidence(stable_key,payload,created_at) VALUES (?,?,?)",
                (key, json.dumps(item), iso_now()),
            )

    def import_record(self, payload: object) -> str:
        if not isinstance(payload, dict) or not isinstance(payload.get("record"), dict):
            raise ValueError("import requires a record object and source evidence")
        raw = payload["record"]
        if set(raw) - RECORD_FIELDS:
            raise ValueError("import has unknown record fields")
        url = _safe_url(raw.get("url"))
        identity = {
            k: str(raw.get(k, "")) for k in ("source_id", "source_label", "external_id", "title")
        }
        if any(not v or len(v) > 300 for v in identity.values()):
            raise ValueError("import requires source_id, source_label, external_id and title")
        if (
            not re.fullmatch(r"[a-z0-9_]{1,80}", identity["source_id"])
            or ":" in identity["external_id"]
        ):
            raise ValueError("use a lowercase source ID and an external ID without colons")
        values = validate_fields({k: v for k, v in raw.items() if k in EDITABLE})
        if values.get("refundable_deposit_yen") is not None and values.get("deposit_yen") is None:
            raise ValueError("an imported refund requires the deposit amount")
        evidence = self.prepare(
            {
                "values": values,
                "source_url": payload.get("source_url", url),
                "kind": payload.get("kind", "published"),
                "note": payload.get("note", "Reviewed import"),
            }
        )
        # Plain imported narrative starts unverified. Structured, reviewed values carry provenance.
        record = HousingRecord(schema_version=1, url=url, **identity)
        record.observed_at = record.detail_verified_at = iso_now()
        key = record.stable_key
        with self.connection:
            self.connection.execute(
                "INSERT INTO decision_imports VALUES (?,?) ON CONFLICT(stable_key) "
                "DO UPDATE SET record_json=excluded.record_json",
                (key, json.dumps(record.to_dict())),
            )
            self.connection.execute(
                "INSERT INTO decision_evidence(stable_key,payload,created_at) VALUES (?,?,?)",
                (key, json.dumps(evidence), iso_now()),
            )
        return key

    def merge(self, originals: list[dict[str, Any]]) -> list[dict[str, Any]]:
        records = {
            item["stable_key"]: {
                **item,
                "field_evidence": dict(item.get("field_evidence", {})),
                "estimated_fields": dict(item.get("estimated_fields", {})),
            }
            for item in originals
        }
        for key, raw in self.connection.execute(
            "SELECT stable_key,record_json FROM decision_imports"
        ):
            record = json.loads(raw)
            age = datetime.now().astimezone() - datetime.fromisoformat(record["detail_verified_at"])
            records.setdefault(
                key,
                {
                    **record,
                    "stable_key": key,
                    "freshness": "stale" if age.total_seconds() > 72 * 3600 else "current",
                    "decision_status": "new",
                },
            )
        for key, raw in self.connection.execute(
            "SELECT stable_key,payload FROM decision_evidence ORDER BY id"
        ):
            if key not in records:
                continue
            evidence = json.loads(raw)
            record = records[key]
            record.setdefault("field_evidence", {})
            for field, value in evidence["values"].items():
                if evidence["kind"] == "estimate":
                    record.setdefault("estimated_fields", {})[field] = value
                else:
                    record["field_evidence"][field] = {
                        k: v for k, v in evidence.items() if k != "values"
                    }
                    if field == "metrics_evidence":
                        previous = record.get(field, {})
                        value = {**previous, **value}
                        value["routes"] = merge_route_history(
                            previous.get("routes", []), value.get("routes", [])
                        )
                    record[field] = value
            if (
                evidence["kind"] != "estimate"
                and "source_availability_status" in evidence["values"]
            ):
                record["detail_verified_at"] = evidence["observed_at"]
                record["freshness"] = (
                    "current"
                    if (
                        datetime.now().astimezone()
                        - datetime.fromisoformat(evidence["observed_at"])
                    ).total_seconds()
                    <= 72 * 3600
                    else "stale"
                )
        if self.connection.execute("SELECT 1 FROM sqlite_master WHERE name='decisions'").fetchone():
            for key, status, note, updated in self.connection.execute(
                "SELECT stable_key,status,note,updated_at FROM decisions"
            ):
                if key in records:
                    records[key].update(
                        decision_status=status, decision_note=note, decision_updated_at=updated
                    )
        return list(records.values())
