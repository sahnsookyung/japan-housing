"""One scenario projection shared by the dashboard, comparisons and exports."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from dataclasses import asdict, fields, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import AppConfig
from .costs import add_months, cost_scenario
from .models import HousingRecord
from .policy import classify, straight_line_distance_km
from .routing import transit_schedule_key

RECORD_FIELDS = frozenset(field.name for field in fields(HousingRecord))
DESTINATION_KINDS = {"office", "remote", "unknown", "relocation"}


def default_preferences(config: AppConfig) -> dict[str, Any]:
    start = max(date(2026, 9, 10), date.today())
    return {
        "version": 1,
        "housing_region": "greater_tokyo",
        "move_in": start.isoformat(),
        "move_out": add_months(start, 2).isoformat(),
        "effective_budget_yen": 85000,
        "cash_budget_yen": 0,
        "minimum_room_m2": 0,
        "commute_limit_minutes": 60,
        "price_weight": 0.85,
        "transport_days_per_month": 0,
        "destinations": [],
    }


def _number(value: object, label: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    if not low <= value <= high:
        raise ValueError(f"{label} must be between {low:g} and {high:g}")
    return value


def validate_preferences(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("scenario must be an object")
    allowed = {
        "version",
        "housing_region",
        "move_in",
        "move_out",
        "effective_budget_yen",
        "cash_budget_yen",
        "minimum_room_m2",
        "commute_limit_minutes",
        "price_weight",
        "transport_days_per_month",
        "destinations",
    }
    if set(payload) - allowed:
        raise ValueError("scenario contains unsupported fields")
    try:
        start = date.fromisoformat(payload["move_in"])
        end = date.fromisoformat(payload["move_out"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("move-in and move-out must be ISO dates") from exc
    if not date(2000, 1, 1) <= start <= date(2100, 1, 1) or not start < end <= add_months(
        start, 36
    ):
        raise ValueError("stay must have ordered dates and last at most 36 months")
    result = {"version": 1, "move_in": start.isoformat(), "move_out": end.isoformat()}
    region = payload.get("housing_region", "greater_tokyo")
    if region not in {"greater_tokyo", "kansai", "japan"}:
        raise ValueError("housing region is unsupported")
    result["housing_region"] = region
    limits = {
        "effective_budget_yen": (1, 10000000, 85000),
        "cash_budget_yen": (0, 100000000, 0),
        "minimum_room_m2": (0, 500, 0),
        "commute_limit_minutes": (1, 240, 60),
        "price_weight": (0, 1, 0.85),
        "transport_days_per_month": (0, 31, 0),
    }
    for key, (low, high, default) in limits.items():
        result[key] = _number(payload.get(key, default), key, low, high)
    destinations = payload.get("destinations", [])
    if not isinstance(destinations, list) or len(destinations) > 20:
        raise ValueError("at most 20 destinations are supported")
    result["destinations"] = []
    seen = set()
    for item in destinations:
        if not isinstance(item, dict):
            raise ValueError("each destination must be an object")
        key, label = str(item.get("id", "")), str(item.get("label", "")).strip()
        if not key or len(key) > 80 or key in seen or not label or len(label) > 160:
            raise ValueError("destinations require unique IDs and a short label")
        seen.add(key)
        kind = item.get("kind", "office")
        if kind not in DESTINATION_KINDS:
            raise ValueError("destination kind is unsupported")
        lat, lon = item.get("latitude"), item.get("longitude")
        if (lat is None) != (lon is None):
            raise ValueError("both latitude and longitude are required")
        if lat is not None:
            _number(lat, "latitude", -85, 85)
            _number(lon, "longitude", -180, 180)
        weight = _number(item.get("weight", 1), "destination weight", 0.01, 100)
        days = item.get("office_days_per_week")
        if days is not None:
            _number(days, "office days per week", 0, 7)
        for flag in ("enabled", "verified"):
            if not isinstance(item.get(flag, flag == "enabled"), bool):
                raise ValueError(f"{flag} must be boolean")
        source = str(item.get("source_url") or "")
        if source and (len(source) > 2000 or urlparse(source).scheme != "https"):
            raise ValueError("destination evidence URL must use HTTPS")
        result["destinations"].append(
            {
                "id": key,
                "label": label,
                "kind": kind,
                "latitude": lat,
                "longitude": lon,
                "enabled": item.get("enabled", True),
                "verified": item.get("verified", False),
                "weight": weight,
                "office_days_per_week": days,
                "location_basis": str(item.get("location_basis", "user pin"))[:200],
                "source_url": source,
            }
        )
    return result


def preferences_path(config: AppConfig) -> Path:
    return config.runtime.state_path.parent / "decision-preferences.json"


def read_preferences(config: AppConfig) -> dict[str, Any]:
    path = preferences_path(config)
    if not path.exists():
        return default_preferences(config)
    if path.stat().st_size > 32768:
        raise ValueError("saved scenario is too large")
    return validate_preferences(json.loads(path.read_text()))


def atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".decision-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_preferences(config: AppConfig, payload: object) -> dict[str, Any]:
    result = validate_preferences(payload)
    atomic_json(preferences_path(config), result)
    return result


def scenario_key(preferences: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(preferences, sort_keys=True).encode()).hexdigest()[:20]


def commute_destinations(preferences: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        d
        for d in preferences["destinations"]
        if d["enabled"] and d["kind"] == "office" and d.get("office_days_per_week") != 0
    ]


def _route_metrics(
    item: dict[str, Any], preferences: dict[str, Any], config: AppConfig
) -> dict[str, Any]:
    targets = commute_destinations(preferences)
    routes = []
    supplied = item.get("metrics_evidence", {}).get("routes", [])
    # Only matching endpoints, schedule and fresh observations count; history is retained.
    schedule = transit_schedule_key(config.routing)
    for destination in targets:
        route = next(
            (
                r
                for r in supplied
                if r.get("destination_id") == destination["id"]
                and r.get("destination_latitude") == destination["latitude"]
                and r.get("destination_longitude") == destination["longitude"]
                and r.get("origin_latitude") == item.get("latitude")
                and r.get("origin_longitude") == item.get("longitude")
                and r.get("schedule_key", schedule) == schedule
                and (date.today() - timedelta(days=config.routing.cache_days)).isoformat()
                <= r.get("verified_at", "")[:10]
                <= date.today().isoformat()
            ),
            None,
        )
        minutes = route.get("minutes") if route else None
        if (
            not isinstance(minutes, int | float)
            or isinstance(minutes, bool)
            or not math.isfinite(minutes)
            or minutes <= 0
        ):
            minutes = None
        routes.append(
            {
                "destination_id": destination["id"],
                "label": destination["label"],
                "minutes": minutes,
                "weight": destination["weight"],
                "office_days_per_week": destination["office_days_per_week"],
                "endpoint_confirmed": destination["verified"],
                "fare_yen": route.get("fare_yen") if route else None,
                "transfers": route.get("transfers") if route else None,
                "walking_minutes": route.get("walking_minutes") if route else None,
                "last_return": route.get("last_return") if route else None,
                "source_url": route.get("source_url") if route else None,
            }
        )
    observed = [r for r in routes if r["minutes"] is not None]
    origin_basis = item.get("coordinate_basis", "unknown")
    origin_confirmed = origin_basis != "unknown" and "approximation" not in origin_basis
    complete = (
        origin_confirmed
        and bool(routes)
        and len(routes) == len(observed)
        and all(r["endpoint_confirmed"] for r in routes)
    )
    weighted = (
        sum(r["minutes"] * r["weight"] for r in observed) / sum(r["weight"] for r in observed)
        if observed
        else None
    )
    fares_complete = complete and all(isinstance(r["fare_yen"], int) for r in observed)
    return {
        "endpoints_confirmed": origin_confirmed and all(r["endpoint_confirmed"] for r in routes),
        "origin_confirmed": origin_confirmed,
        "weekly_commute_hours": round(
            sum(r["minutes"] * 2 * r["office_days_per_week"] * r["weight"] for r in observed)
            / sum(r["weight"] for r in observed)
            / 60,
            1,
        )
        if complete and all(r["office_days_per_week"] is not None for r in observed)
        else None,
        "destinations": routes,
        "observed": len(observed),
        "required": len(targets),
        "complete": complete,
        "mean_minutes": round(weighted, 1) if weighted is not None else None,
        "worst_minutes": max((r["minutes"] for r in observed), default=None),
        "jobs_within_limit": sum(
            r["minutes"] <= preferences["commute_limit_minutes"] for r in observed
        ),
        "monthly_transport_yen": round(
            2
            * preferences["transport_days_per_month"]
            * sum(r["fare_yen"] * r["weight"] for r in observed)
            / sum(r["weight"] for r in observed)
        )
        if fares_complete
        else None,
    }


def geographic_metrics(item: dict[str, Any], preferences: dict[str, Any]) -> dict[str, Any]:
    targets = commute_destinations(preferences)
    distances = []
    if item.get("latitude") is not None and item.get("longitude") is not None:
        for destination in targets:
            if destination["latitude"] is not None and destination["longitude"] is not None:
                distances.append(
                    (
                        straight_line_distance_km(
                            item["latitude"],
                            item["longitude"],
                            destination["latitude"],
                            destination["longitude"],
                        ),
                        destination["weight"],
                    )
                )
    return {
        "complete": bool(targets) and len(distances) == len(targets),
        "mean_km": sum(km * weight for km, weight in distances)
        / sum(weight for _, weight in distances)
        if distances
        else None,
        "worst_km": max((km for km, _ in distances), default=None),
        "observed": len(distances),
        "required": len(targets),
    }


def project_listing(
    item: dict[str, Any],
    preferences: dict[str, Any],
    config: AppConfig,
    *,
    include_exits: bool = True,
) -> dict[str, Any]:
    record = HousingRecord.from_dict(
        {key: value for key, value in item.items() if key in RECORD_FIELDS}
    )
    # Some providers publish zero as a missing-area placeholder, including cached rows.
    if record.room_area_m2 is not None and record.room_area_m2 <= 0:
        record.room_area_m2 = None
    start, end = (
        date.fromisoformat(preferences["move_in"]),
        date.fromisoformat(preferences["move_out"]),
    )
    months = min(36, max(1, (end.year - start.year) * 12 + end.month - start.month))
    stay = cost_scenario(record, months, move_in=start, move_out=end)
    # Remove the old budget/window exclusion before applying this scenario's requirements.
    profile = replace(
        config.profile,
        move_in_earliest=start,
        move_in_latest=start,
        monthly_budget_yen=100000000,
        target_prefectures=tuple(set(config.profile.target_prefectures) | {record.prefecture}),
    )
    outcome = classify(record, profile, replace(config.ranking, maximum_distance_km=40000))
    reasons = [
        reason for reason in outcome.cautions if not reason.startswith("distance to ")
    ] + list(stay.contract_reasons)
    status = "eligible" if outcome.name == "eligible" else "needs_confirmation"
    if outcome.name == "excluded" or stay.contract_eligible is False:
        status = "excluded"
        reasons.extend(outcome.reasons)
    if item.get("freshness") == "stale":
        status = "stale"
        reasons.append("vacancy evidence is stale")
    if (
        stay.effective_monthly_yen is not None
        and stay.effective_monthly_yen > preferences["effective_budget_yen"]
    ):
        status = "excluded"
        reasons.append("known effective expense exceeds the selected budget")
    if not stay.complete or stay.contract_eligible is None:
        status = "needs_confirmation" if status == "eligible" else status
    if preferences["cash_budget_yen"]:
        if (
            stay.cash_before_keys_yen is not None
            and stay.cash_before_keys_yen > preferences["cash_budget_yen"]
        ):
            status = "excluded"
            reasons.append("known cash due exceeds the available cash")
        elif not stay.cash_complete:
            status = "needs_confirmation" if status == "eligible" else status
            reasons.append("cash due before keys is incomplete")
    if preferences["minimum_room_m2"]:
        if record.room_area_m2 is None:
            status = "needs_confirmation" if status == "eligible" else status
            reasons.append("room area requires confirmation")
        elif record.room_area_m2 < preferences["minimum_room_m2"]:
            status = "excluded"
            reasons.append("room is smaller than the selected minimum")
    regions = {
        "greater_tokyo": {"Tokyo", "Kanagawa", "Saitama", "Chiba"},
        "kansai": {"Osaka", "Kyoto", "Hyogo", "Nara", "Shiga", "Wakayama"},
    }
    region = preferences.get("housing_region", "greater_tokyo")
    if region in regions and record.prefecture and record.prefecture not in regions[region]:
        status = "excluded"
        reasons.append("outside the selected housing region")
    if "age_18_39" in record.occupancy_restrictions:
        reasons.append("operator age restriction: 18–39; confirm applicant eligibility")
        status = "needs_confirmation" if status == "eligible" else status
    if item.get("freshness") == "stale":
        status = "stale"
    routes = _route_metrics(item, preferences, config)
    price = stay.effective_monthly_yen
    weight = preferences["price_weight"]
    score = (
        round(
            100
            * (
                weight * price / preferences["effective_budget_yen"]
                + (1 - weight) * routes["mean_minutes"] / preferences["commute_limit_minutes"]
            ),
            2,
        )
        if price is not None and stay.complete and routes["complete"]
        else None
    )
    if weight == 1 and price is not None and stay.complete:
        score = round(100 * price / preferences["effective_budget_yen"], 2)
    recurring = record.recurring_total_yen
    nonref_entry = max(0, (record.initial_cost_yen or 0) - (record.refundable_deposit_yen or 0))
    early = (
        [asdict(cost_scenario(record, n, move_in=start)) for n in (1, 2, 3)]
        if include_exits
        else []
    )
    facilities = {
        name: round(record.residents / count, 1) if record.residents and count else None
        for name, count in (
            ("residents_per_shower", record.showers),
            ("residents_per_toilet", record.toilets),
            ("residents_per_washer", record.washers),
        )
    }
    try:
        age = round(
            (
                datetime.now().astimezone() - datetime.fromisoformat(record.detail_verified_at)
            ).total_seconds()
            / 3600,
            1,
        )
    except (TypeError, ValueError):
        age = None
    return {
        **item,
        "room_area_m2": record.room_area_m2,
        "scenario": asdict(stay),
        "scenario_status": status,
        "scenario_reasons": list(dict.fromkeys(reasons)),
        "ranking_price_yen": price,
        "priority_score": score,
        "route_metrics": routes,
        "geographic_metrics": geographic_metrics(item, preferences),
        "exit_scenarios": early,
        "metrics": {
            **facilities,
            "verification_age_hours": age,
            "housing_plus_transport_yen": price + routes["monthly_transport_yen"]
            if price is not None and routes["monthly_transport_yen"] is not None
            else None,
            "entry_fee_equivalent_months": round(nonref_entry / recurring, 2)
            if recurring and stay.complete
            else None,
            "unresolved_cost_count": len(stay.unknown_costs),
            "cash_buffer_yen": preferences["cash_budget_yen"] - stay.cash_before_keys_yen
            if preferences["cash_budget_yen"] and stay.cash_complete
            else None,
        },
    }


def tradeoff_candidate(item: dict[str, Any]) -> bool:
    return item["scenario_status"] in {"eligible", "needs_confirmation"} and item.get(
        "decision_status"
    ) not in {"reserved", "not_interested"}


def tradeoff_value(item: dict[str, Any], metric: str) -> float | None:
    """Plot potential matches without upgrading uncertain prices or eligibility."""
    if not tradeoff_candidate(item):
        return None
    price = item["ranking_price_yen"]
    if price is None or not math.isfinite(price):
        return None
    if metric == "distance":
        values = item["geographic_metrics"]
        return values["mean_km"] if values["complete"] else None
    routes = item["route_metrics"]
    # A partial destination average is not comparable with the full-office average.
    return (
        routes["mean_minutes"]
        if routes["required"] > 0 and routes["observed"] == routes["required"]
        else None
    )


def frontier_value(item: dict[str, Any], metric: str) -> float | None:
    if (
        item["scenario_status"] != "eligible"
        or not item["scenario"]["complete"]
        or item.get("decision_status") in {"reserved", "not_interested"}
    ):
        return None
    values = item["geographic_metrics"] if metric == "distance" else item["route_metrics"]
    return (
        values["mean_km" if metric == "distance" else "mean_minutes"]
        if values["complete"]
        else None
    )


def scenario_frontier(records: list[dict[str, Any]], metric: str = "transit") -> set[str]:
    candidates = [r for r in records if frontier_value(r, metric) is not None]
    candidates.sort(key=lambda r: (r["ranking_price_yen"], frontier_value(r, metric)))
    best = math.inf
    result = set()
    previous = None
    for item in candidates:
        pair = (item["ranking_price_yen"], frontier_value(item, metric))
        if pair[1] < best or pair == previous:
            result.add(item["stable_key"])
            best = pair[1]
            previous = pair
    return result


def decision_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    current = [r for r in records if r.get("freshness") != "stale"]
    feasible = [r for r in current if tradeoff_candidate(r)]
    return {
        "stored": len(records),
        "potential": len(feasible),
        "current": len(current),
        "eligible": sum(r["scenario_status"] == "eligible" for r in current),
        "needs_confirmation": sum(r["scenario_status"] == "needs_confirmation" for r in current),
        "cost_complete": sum(r["scenario"]["complete"] for r in feasible),
        "route_complete": sum(r["route_metrics"]["complete"] for r in feasible),
        "excluded": sum(r["scenario_status"] == "excluded" for r in current),
    }
