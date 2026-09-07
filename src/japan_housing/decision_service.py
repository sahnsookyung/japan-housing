"""Bounded cached scenario queries; no scraping in filter requests."""

from __future__ import annotations

import csv
import io
import json
import math
import threading
from datetime import date
from typing import Any

from .config import AppConfig
from .costs import cost_scenario
from .decision import (
    RECORD_FIELDS,
    decision_summary,
    project_listing,
    read_preferences,
    save_preferences,
    scenario_frontier,
    scenario_key,
)
from .evidence import EvidenceStore
from .models import HousingRecord
from .share_sources import SOURCE_CATALOG


class DecisionService:
    def __init__(self, config: AppConfig, snapshots: Any):
        self.config, self.snapshots = config, snapshots
        self.lock = threading.RLock()
        self.key = None
        self.records: list[dict[str, Any]] = []
        self.by_id = {}
        self.preferences = {}
        self.report = {}

    def invalidate(self) -> None:
        with self.lock:
            self.key = None

    def load(self) -> None:
        report = self.snapshots.load()
        preferences = read_preferences(self.config)
        with EvidenceStore(self.config.runtime.state_path) as evidence:
            key = (report["run_id"], scenario_key(preferences), evidence.revision())
            if key == self.key:
                return
            originals = evidence.merge(report.get("housing", []))
        self.preferences = preferences
        self.report = report
        self.records = [project_listing(item, preferences, self.config) for item in originals]
        # Precompute search text once per immutable view, not per keystroke.
        for item in self.records:
            item["search_text"] = " ".join(
                str(item.get(k, ""))
                for k in (
                    "title",
                    "address",
                    "city",
                    "prefecture",
                    "source_label",
                    "nearest_stations",
                )
            ).casefold()
        self.by_id = {item["stable_key"]: item for item in self.records}
        self.key = key

    def save(self, payload: object) -> dict[str, Any]:
        with self.lock:
            result = save_preferences(self.config, payload)
            self.key = None
            return result

    def _filtered(self, params: dict[str, list[str]]) -> list[dict[str, Any]]:
        query = params.get("query", [""])[0].strip().casefold()
        if len(query) > 200:
            raise ValueError("query is too long")
        status = params.get("status", ["feasible"])[0]
        if status not in {"feasible", "eligible", "needs_confirmation", "excluded", "stale", "all"}:
            raise ValueError("unsupported status")
        decision = params.get("decision", ["all"])[0]
        rows = [
            r
            for r in self.records
            if (not query or query in r["search_text"])
            and (
                status == "all"
                or r["scenario_status"] == status
                or status == "feasible"
                and r["scenario_status"] in {"eligible", "needs_confirmation"}
            )
            and (decision == "all" or r.get("decision_status", "new") == decision)
        ]
        rank = {"eligible": 0, "needs_confirmation": 1, "excluded": 2, "stale": 3}
        sort = params.get("sort", ["recommended"])[0]
        if sort not in {"recommended", "price", "cash", "space", "commute", "verification"}:
            raise ValueError("unsupported sort")

        def order(item):
            price = item["ranking_price_yen"]
            value = {
                "price": price,
                "cash": item["scenario"]["cash_before_keys_yen"],
                "space": -item["room_area_m2"] if item.get("room_area_m2") is not None else None,
                "commute": item["route_metrics"]["mean_minutes"],
                "verification": len(item["scenario"]["unknown_costs"]),
            }.get(sort, item["priority_score"])
            return (
                rank[item["scenario_status"]],
                not item["scenario"]["complete"],
                value if value is not None else math.inf,
                price if price is not None else math.inf,
                item["stable_key"],
            )

        rows.sort(key=order)
        return rows

    @staticmethod
    def compact(item: dict[str, Any]) -> dict[str, Any]:
        keys = {
            "stable_key",
            "title",
            "source_label",
            "source_id",
            "url",
            "city",
            "prefecture",
            "latitude",
            "longitude",
            "room_area_m2",
            "desk",
            "scenario_status",
            "ranking_price_yen",
            "priority_score",
            "decision_status",
            "scenario",
            "route_metrics",
            "metrics",
            "available_text",
            "registration_confidence",
            "cluster_key",
        }
        result = {key: item.get(key) for key in keys}
        result["scenario"] = {
            key: value for key, value in item["scenario"].items() if key != "ledger"
        }
        return result

    def query(self, params: dict[str, list[str]]) -> dict[str, Any]:
        with self.lock:
            self.load()
            rows = self._filtered(params)
            try:
                offset = int(params.get("offset", ["0"])[0])
                limit = int(params.get("limit", ["50"])[0])
            except ValueError as exc:
                raise ValueError("pagination must be integers") from exc
            if not 0 <= offset <= 100000 or not 1 <= limit <= 200:
                raise ValueError("pagination out of bounds")
            frontier = scenario_frontier(rows)
            points = []
            groups: dict[tuple[float, float], list[dict[str, Any]]] = {}
            for item in rows:
                lat, lon = item.get("latitude"), item.get("longitude")
                if isinstance(lat, int | float) and isinstance(lon, int | float):
                    group = groups.setdefault((lat, lon), [])
                    group.append(
                        {
                            "id": item["stable_key"],
                            "title": item["title"],
                            "price": item["ranking_price_yen"],
                            "status": item["scenario_status"],
                        }
                    )
                if (
                    item["route_metrics"]["complete"]
                    and item["scenario"]["complete"]
                    and item["scenario_status"] == "eligible"
                ):
                    points.append(
                        {
                            "id": item["stable_key"],
                            "title": item["title"],
                            "price": item["ranking_price_yen"],
                            "minutes": item["route_metrics"]["mean_minutes"],
                            "frontier": item["stable_key"] in frontier,
                        }
                    )
            map_points = [
                {"latitude": lat, "longitude": lon, "rooms": members[:30], "count": len(members)}
                for (lat, lon), members in list(groups.items())[:500]
            ]
            return {
                "run_id": self.report["run_id"],
                "scenario_key": scenario_key(self.preferences),
                "preferences": self.preferences,
                "generated_at": self.report["generated_at"],
                "summary": decision_summary(self.records),
                "total": len(rows),
                "offset": offset,
                "limit": limit,
                "records": [self.compact(r) for r in rows[offset : offset + limit]],
                "map_points": map_points,
                "map_total": len(groups),
                "points": points[:1000],
                "chart_total": len(points),
                "frontier_ids": sorted(frontier),
                "source_health": self.report.get("source_health", []),
                "source_coverage": self.report.get("source_coverage", []),
                "source_catalog": list(SOURCE_CATALOG),
                "manual_fallbacks": self.report.get("manual_fallbacks", []),
                "routing_status": self.report.get("performance", {})
                .get("commute", {})
                .get("status", "not_configured"),
                "ranking_note": (
                    "Only complete observed commutes receive a weighted score. Other rows "
                    "fall back to known expense within their evidence tier."
                ),
            }

    def detail(self, key: str) -> dict[str, Any]:
        with self.lock:
            self.load()
            if key not in self.by_id:
                raise ValueError("listing is no longer present")
            return {k: v for k, v in self.by_id[key].items() if k != "search_text"}

    def compare(self, keys: list[str]) -> dict[str, Any]:
        if not 1 <= len(keys) <= 3 or len(set(keys)) != len(keys):
            raise ValueError("compare one to three distinct rooms")
        with self.lock:
            self.load()
            items = [self.detail(key) for key in keys]
            start = date.fromisoformat(self.preferences["move_in"])
            series = []
            for item in items:
                record = HousingRecord.from_dict(
                    {key: val for key, val in item.items() if key in RECORD_FIELDS}
                )
                values = [cost_scenario(record, n, move_in=start) for n in range(1, 13)]
                series.append(
                    {
                        "id": item["stable_key"],
                        "title": item["title"],
                        "values": [
                            {
                                "months": s.months,
                                "total_yen": s.known_total_yen,
                                "complete": s.complete,
                                "contract_eligible": s.contract_eligible,
                                "end": s.move_out,
                            }
                            for s in values
                        ],
                    }
                )
            crossings = []
            for i, left in enumerate(series):
                for right in series[i + 1 :]:
                    pairs = [
                        (a, b)
                        for a, b in zip(left["values"], right["values"], strict=True)
                        if a["complete"]
                        and b["complete"]
                        and a["contract_eligible"] is True
                        and b["contract_eligible"] is True
                    ]
                    initial = next((a["total_yen"] - b["total_yen"] for a, b in pairs), None)
                    changed = next(
                        (
                            a["end"]
                            for a, b in pairs
                            if initial is not None
                            and (a["total_yen"] - b["total_yen"]) * initial < 0
                        ),
                        None,
                    )
                    crossings.append(
                        {
                            "left": left["id"],
                            "right": right["id"],
                            "first_monthly_crossover": changed,
                            "comparable_months": len(pairs),
                        }
                    )
            return {
                "records": items,
                "cost_series": series,
                "crossovers": crossings,
                "note": (
                    "Totals compare equal stay periods; crossovers are checked at monthly "
                    "anniversaries, not claimed as exact daily break-even dates."
                ),
            }

    def export(self, params: dict[str, list[str]], format: str) -> tuple[bytes, str]:
        with self.lock:
            self.load()
            rows = self._filtered(params)
            if format == "json":
                return json.dumps(
                    {
                        "run_id": self.report["run_id"],
                        "preferences": self.preferences,
                        "records": [
                            {k: v for k, v in r.items() if k != "search_text"} for r in rows
                        ],
                    },
                    ensure_ascii=False,
                ).encode(), "application/json"
            if format != "csv":
                raise ValueError("export must be csv or json")
            out = io.StringIO()
            writer = csv.writer(out)
            writer.writerow(
                [
                    "Room",
                    "Status",
                    "Move in",
                    "Move out (exclusive)",
                    "Known expense JPY",
                    "Effective monthly JPY",
                    "Expense complete",
                    "Cash before keys JPY",
                    "Cash complete",
                    "Room m2",
                    "Mean commute minutes",
                    "Source",
                ]
            )
            for item in rows:
                s = item["scenario"]
                title = item["title"]
                if title.lstrip().startswith(("=", "+", "-", "@")):
                    title = "'" + title
                writer.writerow(
                    [
                        title,
                        item["scenario_status"],
                        s["move_in"],
                        s["move_out"],
                        s["known_total_yen"],
                        s["effective_monthly_yen"],
                        s["complete"],
                        s["cash_before_keys_yen"],
                        s["cash_complete"],
                        item.get("room_area_m2"),
                        item["route_metrics"]["mean_minutes"],
                        item["url"],
                    ]
                )
            return out.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8"
