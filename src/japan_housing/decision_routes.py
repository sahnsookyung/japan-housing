"""Small, explicit transit requests for saved user destinations; no bicycle dependency."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from .config import AppConfig, RouteAnchor
from .routing import (
    GoogleRoutesClient,
    RouteOrigin,
    coordinate_key,
    representative_departure,
    resolve_routes_api_key,
    transit_schedule_key,
)


def fetch_room_routes(
    item: dict[str, Any], preferences: dict[str, Any], config: AppConfig
) -> list[dict[str, Any]]:
    lat, lon = item.get("latitude"), item.get("longitude")
    if lat is None or lon is None:
        raise ValueError("Add verified room coordinates before fetching routes.")
    targets = [
        d
        for d in preferences["destinations"]
        if d["enabled"]
        and d["kind"] == "office"
        and d["latitude"] is not None
        and d.get("office_days_per_week") != 0
    ]
    if not targets:
        raise ValueError("Enable an office destination with a pin first.")
    schedule = transit_schedule_key(config.routing)
    cutoff = (date.today() - timedelta(days=config.routing.cache_days)).isoformat()
    existing = item.get("metrics_evidence", {}).get("routes", [])
    routes, missing = [], []
    for destination in targets:
        found = next(
            (
                r
                for r in existing
                if r.get("destination_id") == destination["id"]
                and r.get("destination_latitude") == destination["latitude"]
                and r.get("destination_longitude") == destination["longitude"]
                and r.get("origin_latitude") == lat
                and r.get("origin_longitude") == lon
                and cutoff <= r.get("verified_at", "")[:10] <= date.today().isoformat()
                and r.get("schedule_key", schedule) == schedule
            ),
            None,
        )
        if found:
            routes.append(found)
        else:
            missing.append(destination)
    if not missing:
        return routes
    secret = resolve_routes_api_key(Path(".env"), required=True)
    anchors = tuple(
        RouteAnchor(d["id"], d["label"], d["latitude"], d["longitude"]) for d in missing
    )
    with GoogleRoutesClient(secret, config) as client:
        values = client.compute_matrix(
            "TRANSIT",
            [RouteOrigin(coordinate_key(lat, lon), lat, lon)],
            anchors,
            representative_departure(config.routing),
            schedule,
        )
    by_id = {d["id"]: d for d in missing}
    for value in values:
        if value.duration_seconds is None:
            continue
        destination = by_id[value.anchor_id]
        url = "https://www.google.com/maps/dir/?" + urlencode(
            {
                "api": 1,
                "origin": f"{lat},{lon}",
                "destination": f"{destination['latitude']},{destination['longitude']}",
                "travelmode": "transit",
            }
        )
        routes.append(
            {
                "destination_id": destination["id"],
                "destination_latitude": destination["latitude"],
                "destination_longitude": destination["longitude"],
                "origin_latitude": lat,
                "origin_longitude": lon,
                "minutes": round(value.duration_seconds / 60, 1),
                "verified_at": value.fetched_at,
                "source_url": url,
                "provider": "google_routes",
                "schedule_key": schedule,
                "departure_at": value.departure_at,
            }
        )
    return routes
