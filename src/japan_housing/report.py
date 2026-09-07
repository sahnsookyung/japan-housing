"""Deterministic report construction and atomic multi-format publication."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .config import AppConfig
from .decision import RECORD_FIELDS, decision_summary
from .decision_service import DecisionService
from .models import SourceRunResult, iso_now
from .pareto import annotate_pareto_frontier
from .sources import source_specs
from .state import StateStore


@dataclass(frozen=True, slots=True)
class PublishedReport:
    run_id: str
    status: str
    directory: Path
    json_path: Path
    markdown_path: Path
    csv_path: Path
    job_count: int


def _source_status_map(health: list[dict[str, Any]]) -> dict[str, str]:
    return {str(item["source_id"]): str(item.get("last_status") or "not_run") for item in health}


def build_report_payload(
    store: StateStore,
    run_id: str,
    source_results: list[SourceRunResult],
    performance: dict[str, Any],
    config: AppConfig,
) -> dict[str, Any]:
    store.apply_listing_freshness(source_specs())
    reclassified = store.reclassify_active(config.profile, config.ranking, config.routing)
    health = store.source_health()
    statuses = _source_status_map(health)
    records = store.active_records()
    for item in records:
        item["source_status"] = statuses.get(str(item.get("source_id")), "unknown")
        if item.get("freshness") == "stale":
            stale_evidence = "listing is stale and requires detail-page revalidation"
            cautions = list(item.get("cautions") or ())
            cautions.append(stale_evidence)
            item["cautions"] = list(dict.fromkeys(cautions))
            if item.get("classification") != "excluded":
                item["classification"] = (
                    "unverified" if config.profile.strict_evidence else "review"
                )
                rank_key = list(item.get("rank_key") or ())
                if rank_key:
                    rank_key[0] = 2 if config.profile.strict_evidence else 1
                    item["rank_key"] = rank_key
    records.sort(key=lambda item: tuple(item.get("rank_key") or (999, str(item.get("stable_key")))))
    pareto = annotate_pareto_frontier(records, config.ranking, config.routing)
    classifications = ("eligible", "review", "unverified", "excluded")
    counts = {name: 0 for name in classifications}
    current_counts = {name: 0 for name in classifications}
    tracks = {name: 0 for name in ("landing", "long_term", "both")}
    current_tracks = {name: 0 for name in ("landing", "long_term", "both")}
    freshness_counts = {name: 0 for name in ("current", "stale")}
    for item in records:
        classification = str(item.get("classification"))
        track = str(item.get("track"))
        freshness = str(item.get("freshness") or "current")
        counts[classification] = counts.get(classification, 0) + 1
        tracks[track] = tracks.get(track, 0) + 1
        freshness_counts[freshness] = freshness_counts.get(freshness, 0) + 1
        if freshness == "current":
            current_counts[classification] = current_counts.get(classification, 0) + 1
            current_tracks[track] = current_tracks.get(track, 0) + 1
    persisted_statuses = [str(item.get("last_status") or "not_run") for item in health]
    commute_coverage = store.commute_coverage(config.routing)
    routing_complete = (
        not config.routing.enabled
        or commute_coverage["fresh_elements"] == commute_coverage["required_elements"]
    )
    classification_invariant_ok = not config.profile.strict_evidence or counts.get("review", 0) == 0
    status = (
        "healthy"
        if persisted_statuses
        and all(value == "healthy" for value in persisted_statuses)
        and all(result.status == "healthy" for result in source_results)
        and routing_complete
        and classification_invariant_ok
        else "partial"
    )
    payload = {
        "schema_version": 10,
        "run_id": run_id,
        "generated_at": iso_now(),
        "status": status,
        "housing_count": len(records),
        "classification_counts": counts,
        "current_classification_counts": current_counts,
        "track_counts": tracks,
        "current_track_counts": current_tracks,
        "freshness_counts": freshness_counts,
        "source_health": health,
        "source_coverage": store.source_coverage(),
        "commute_coverage": commute_coverage,
        "performance": performance,
        "evidence_policy": {
            "mode": "separate_unverified" if config.profile.strict_evidence else "manual_review",
            "strict_evidence": config.profile.strict_evidence,
            "classification_invariant_ok": classification_invariant_ok,
            "manual_review_count": counts.get("review", 0),
            "current_manual_review_count": current_counts.get("review", 0),
            "unverified_count": counts.get("unverified", 0),
            "current_unverified_count": current_counts.get("unverified", 0),
        },
        "ranking": {
            "method": "weighted_normalized_price_distance_transit_and_bicycle",
            "direction": "lower_is_better",
            "anchor_label": config.ranking.anchor_label,
            "anchor_latitude": config.ranking.anchor_latitude,
            "anchor_longitude": config.ranking.anchor_longitude,
            "anchor_members": [anchor.label for anchor in config.routing.anchors],
            "price_weight": config.ranking.price_weight,
            "distance_weight": config.ranking.distance_weight,
            "transit_weight": config.ranking.transit_weight,
            "bicycle_weight": config.ranking.bicycle_weight,
            "price_reference_yen": config.ranking.price_reference_yen,
            "distance_reference_km": config.ranking.distance_reference_km,
            "transit_reference_minutes": config.ranking.transit_reference_minutes,
            "bicycle_reference_minutes": config.ranking.bicycle_reference_minutes,
            "missing_distance_penalty_km": config.ranking.missing_distance_penalty_km,
            "missing_transit_penalty_minutes": config.ranking.missing_transit_penalty_minutes,
            "missing_bicycle_penalty_minutes": config.ranking.missing_bicycle_penalty_minutes,
            "maximum_distance_km": config.ranking.maximum_distance_km,
            "uses_train_time": True,
            "uses_bus_time": True,
            "uses_bicycle_time": True,
            "commute_aggregation": "penalized_arithmetic_mean_across_all_route_anchors",
            "variants": {
                "closer": {"price_weight": 0.40, "location_weight": 0.60},
                "balanced": {
                    "price_weight": config.ranking.price_weight,
                    "distance_weight": config.ranking.distance_weight,
                    "transit_weight": config.ranking.transit_weight,
                    "bicycle_weight": config.ranking.bicycle_weight,
                },
                "cheaper": {"price_weight": 0.80, "location_weight": 0.20},
            },
            "pareto_frontier_count": pareto.frontier_count,
            "pareto": {
                "direction": "minimize_effective_monthly_price_and_location_burden",
                "price_metric": "ranking_price_yen",
                "location_metric": "pareto_location_scores[selected_perspective]",
                "location_inputs": [
                    "midpoint_distance",
                    "mean_public_transit_across_anchors",
                    "mean_bicycle_time_across_anchors",
                ],
                "perspectives": [
                    {
                        "id": "balanced",
                        "label": "All anchors (balanced)",
                        "kind": "aggregate",
                        "distance_target": config.ranking.anchor_label,
                        "candidate_count": pareto.candidate_counts["balanced"],
                        "frontier_count": pareto.frontier_counts["balanced"],
                    },
                    *(
                        {
                            "id": anchor.anchor_id,
                            "label": anchor.label,
                            "kind": "anchor",
                            "distance_target": anchor.label,
                            "candidate_count": pareto.candidate_counts[anchor.anchor_id],
                            "frontier_count": pareto.frontier_counts[anchor.anchor_id],
                        }
                        for anchor in config.routing.anchors
                    ),
                ],
                "cohort": "housing_track",
                "candidate_classification": "eligible",
                "excluded_decisions": ["reserved", "not_interested"],
                "candidate_count": pareto.candidate_count,
                "frontier_count": pareto.frontier_count,
                "cohort_counts": pareto.cohort_counts,
                "candidate_counts": pareto.candidate_counts,
                "frontier_counts": pareto.frontier_counts,
                "perspective_cohort_counts": pareto.perspective_cohort_counts,
                "complexity": "O(p * n log n), p <= 4",
            },
            "reclassified_records": reclassified,
            "long_term_comparison_months": config.profile.long_term_comparison_months,
        },
        "unknown_notifications": store.unknown_notifications(),
        "manual_fallbacks": [
            {
                "label": "Sakura House",
                "url": "https://www.sakura-house.com/",
                "reason": (
                    "Address registration explicitly supported; manual browser check required."
                ),
            },
            {
                "label": "Leopalace21 Monthly",
                "url": "https://www.leopalace21.com/en/properties/monthly/",
                "reason": "Furnished 30–365 day fallback; verify prepaid total against budget.",
            },
            {
                "label": "Borderless House",
                "url": "https://www.borderless-house.com/jp/",
                "reason": (
                    "Working-holiday share houses; confirm address registration per property."
                ),
            },
            {
                "label": "GaijinPot Apartments",
                "url": "https://apartments.gaijinpot.com/en/",
                "reason": "Useful English-speaking agents; automated access is not used.",
            },
            {
                "label": "UR Rental Housing",
                "url": "https://www.ur-net.go.jp/chintai/sp/nonjapanese/",
                "reason": "No guarantor/key money; best evaluated after resident registration.",
            },
        ],
        "housing": records,
    }
    scenario_config = replace(config, runtime=replace(config.runtime, state_path=store.path))
    service = DecisionService(scenario_config, SimpleNamespace(load=lambda: payload))
    service.load()
    projected = service._filtered({"status": ["all"]})
    payload["decision_scenario"] = {
        "preferences": service.preferences,
        "summary": decision_summary(projected),
    }
    # Keep collector diagnostics for existing consumers; scenario fields are explicit.
    additions = (
        *RECORD_FIELDS,
        "scenario",
        "scenario_status",
        "scenario_reasons",
        "route_metrics",
        "metrics",
        "field_evidence",
        "estimated_fields",
    )
    originals = {row["stable_key"]: row for row in records}
    payload["housing"] = [
        {
            **originals.get(row["stable_key"], row),
            **{name: row[name] for name in additions if name in row},
            "scenario_priority_score": row["priority_score"],
        }
        for row in projected
    ]
    payload["housing_count"] = len(projected)
    return payload


def _decision_markdown(payload: dict[str, Any]) -> str:
    decision = payload["decision_scenario"]
    p, summary = decision["preferences"], decision["summary"]
    lines = [
        "# Japan Housing · Saved decision scenario",
        "",
        f"Stay: {p['move_in']} → {p['move_out']} (move-out exclusive)",
        f"Effective ceiling: ¥{p['effective_budget_yen']:,}/month · "
        f"price weight {p['price_weight']:.0%}",
        f"Housing area: {p.get('housing_region', 'greater_tokyo')}",
        f"Generated: {payload['generated_at']} · source status: {payload['status']}",
        "",
        f"{summary['current']} current · {summary['eligible']} meets requirements · "
        f"{summary['needs_confirmation']} needs confirmation · "
        f"{summary['route_complete']} complete office routes",
        "",
        "Expense excludes refundable security cash. "
        "Incomplete amounts are known floors, not quotes.",
        "Only observed routes to confirmed office pins receive a weighted score.",
        "",
        "| Room | Stay expense | Effective / month | Cash before keys | Status |",
        "|---|---:|---:|---:|---|",
    ]
    candidates = [
        r
        for r in payload["housing"]
        if r["scenario_status"] in {"eligible", "needs_confirmation"}
        and r.get("decision_status") not in {"reserved", "not_interested"}
    ]
    for row in candidates[:50]:
        s = row["scenario"]

        def money(value):
            return f"¥{value:,}" if value is not None else "Unknown"

        floor = "" if s["complete"] else "≥ "
        cash_floor = "" if s["cash_complete"] else "≥ "
        title = str(row["title"]).replace("|", r"\|").replace("\n", " ")
        lines.append(
            f"| [{title}]({row['url']}) | {floor}{money(s['known_total_yen'])} | "
            f"{floor}{money(s['effective_monthly_yen'])} | "
            f"{cash_floor}{money(s['cash_before_keys_yen'])} | {row['scenario_status']} |"
        )
    if not candidates:
        lines.append("No current potential matches for the selected dates.")
    lines.extend(["", "## Saved employment destinations", ""])
    for d in p["destinations"]:
        lines.append(
            f"- {d['label']}: {d['kind']} · {'enabled' if d['enabled'] else 'disabled'} · "
            f"{'confirmed entrance' if d['verified'] else 'location unconfirmed'}"
        )
    lines.extend(["", "## Source coverage", ""])
    for source in payload["source_health"]:
        lines.append(
            f"- {source['source_id']}: {source.get('last_status') or 'not run'} · "
            f"{source.get('last_error') or 'no error recorded'}"
        )
    lines.extend(
        [
            "",
            "The dashboard and its filtered exports use the same saved scenario. "
            "Room details retain individual cost terms and provenance.",
            "",
        ]
    )
    return "\n".join(lines)


def _markdown(payload: dict[str, Any]) -> str:
    if "decision_scenario" in payload:
        return _decision_markdown(payload)
    counts = payload.get("current_classification_counts") or payload["classification_counts"]
    freshness = payload.get("freshness_counts") or {}
    current_total = sum(int(value) for value in counts.values())
    stale_total = int(freshness.get("stale") or 0)
    ranking = payload["ranking"]
    anchor_frontiers = " · ".join(
        f"{perspective['label']} {perspective['frontier_count']}/{perspective['candidate_count']}"
        for perspective in ranking["pareto"]["perspectives"]
        if perspective["kind"] == "anchor"
    )
    lines = [
        "# Japan Housing Report",
        "",
        f"Generated: {payload['generated_at']}",
        f"Status: **{payload['status']}**",
        (
            f"Listings: **{current_total} current** · "
            f"eligible {counts.get('eligible', 0)} · "
            f"unverified {counts.get('unverified', 0)} · "
            f"review {counts.get('review', 0)} · "
            f"excluded {counts.get('excluded', 0)} · {stale_total} quarantined/stale · "
            f"{payload['housing_count']} stored"
        ),
        "",
        (
            "> Address registration, final fees, availability, and contract eligibility "
            "must be confirmed before payment."
        ),
        (
            "> Priority is lower-is-better: "
            f"{ranking['price_weight']:.0%} ranking monthly cost plus "
            f"{ranking['distance_weight']:.0%} straight-line midpoint distance, "
            f"{ranking['transit_weight']:.0%} mean public-transit time, and "
            f"{ranking['bicycle_weight']:.0%} mean bicycle time across the three stations."
        ),
        (
            f"> References: ¥{ranking['price_reference_yen']:,} and "
            f"{ranking['distance_reference_km']:g} km, "
            f"{ranking['transit_reference_minutes']:g} transit minutes, and "
            f"{ranking['bicycle_reference_minutes']:g} bicycle minutes normalize their "
            "respective components to 100%."
        ),
        (
            "> Missing routes are scored conservatively as "
            f"{ranking['missing_transit_penalty_minutes']:g} transit minutes or "
            f"{ranking['missing_bicycle_penalty_minutes']:g} bicycle minutes. "
            "Google bicycle routes are beta and may omit or misidentify suitable cycling paths."
        ),
        (
            f"> Route coverage: {payload['commute_coverage']['fresh_elements']}/"
            f"{payload['commute_coverage']['required_elements']} fresh matrix elements "
            f"({payload['commute_coverage']['coverage_ratio']:.0%})."
        ),
        (
            f"> Complete landing listings use three-month effective monthly cost; complete "
            f"long-term listings use the {ranking['long_term_comparison_months']}-month "
            "effective monthly cost. Incomplete totals remain explicitly separated."
        ),
        (
            f"> Pareto frontier: {ranking['pareto']['frontier_count']} of "
            f"{ranking['pareto']['candidate_count']} eligible, still-actionable candidates. "
            "Effective monthly price and normalized location burden are both minimized within "
            "each housing track."
        ),
        (
            f"> Anchor-specific Pareto frontiers: {anchor_frontiers}. Each station view uses "
            "its own straight-line distance, public-transit/bus time, and bicycle time."
        ),
        "",
    ]
    for heading, classification in (
        ("Actionable", "eligible"),
        ("Manual review", "review"),
        ("Unverified / blocked", "unverified"),
        ("Excluded", "excluded"),
    ):
        lines.extend((f"## {heading}", ""))
        selected = [item for item in payload["housing"] if item["classification"] == classification]
        if not selected:
            lines.extend(("None.", ""))
            continue
        if classification in {"unverified", "excluded"}:
            label = "unverified" if classification == "unverified" else "excluded"
            lines.extend(
                (
                    f"{len(selected):,} {label} listings are retained in the JSON and CSV files. ",
                    "They are omitted here to keep the Discord attachment bounded.",
                    "",
                )
            )
            continue
        for item in selected:
            title = str(item.get("title") or "Untitled").replace("[", "(").replace("]", ")")
            recurring = item.get("recurring_total_yen")
            cost = (
                f"¥{recurring:,}/month" if isinstance(recurring, int) else "monthly total unknown"
            )
            place = " ".join(
                filter(None, (str(item.get("prefecture") or ""), str(item.get("city") or "")))
            )
            decision = item.get("decision_status") or "new"
            lines.append(f"### [{title}]({item.get('url')})")
            summary = (
                f"{cost} · {place or 'location unverified'} · {item.get('track')} · "
                f"priority {item.get('priority_score', 'unknown')} · "
                f"{'Pareto frontier · ' if item.get('pareto_frontier') else ''}"
                f"decision: `{decision}`"
            )
            lines.append(summary)
            ranking_price = item.get("ranking_price_yen")
            if isinstance(ranking_price, int):
                lines.append(
                    f"- Ranking price: ¥{ranking_price:,}/month "
                    f"({item.get('ranking_price_basis', 'unknown')})"
                )
            distance = item.get("distance_to_anchor_km")
            if isinstance(distance, int | float):
                lines.append(
                    f"- Distance: {distance:.2f} km straight-line to {ranking['anchor_label']} "
                    "midpoint"
                )
            else:
                lines.append(
                    f"- Distance: unavailable; {ranking['missing_distance_penalty_km']:g} km "
                    "ranking penalty applied"
                )
            transit = item.get("transit_minutes_mean")
            bicycle = item.get("bicycle_minutes_mean")
            transit_scored = item.get("transit_minutes_scored")
            bicycle_scored = item.get("bicycle_minutes_scored")
            anchors = int(item.get("commute_anchor_count") or 0)
            transit_coverage = int(item.get("transit_route_coverage") or 0)
            bicycle_coverage = int(item.get("bicycle_route_coverage") or 0)
            transit_observed = (
                f"{transit:.1f} min observed mean"
                if isinstance(transit, int | float)
                else "no observed route"
            )
            bicycle_observed = (
                f"{bicycle:.1f} min observed mean"
                if isinstance(bicycle, int | float)
                else "no observed route"
            )
            lines.append(
                f"- Public transit: {transit_observed} ({transit_coverage}/{anchors}); "
                f"ranking mean {float(transit_scored):.1f} min including missing-route penalties"
            )
            lines.append(
                f"- Bicycle: {bicycle_observed} ({bicycle_coverage}/{anchors}); "
                f"ranking mean {float(bicycle_scored):.1f} min including missing-route penalties"
            )
            if item.get("available_text"):
                lines.append(f"- Availability: {item['available_text']}")
            if item.get("source_id") == "oakhouse":
                price_fields = (
                    item.get("recurring_total_yen"),
                    item.get("rent_yen"),
                    item.get("management_fee_yen"),
                )
                if all(isinstance(value, int) for value in price_fields):
                    total, rent, management = price_fields
                    lines.append(
                        f"- Available-room monthly price: ¥{total:,} "
                        f"(rent ¥{rent:,} + maintenance ¥{management:,})"
                    )
                else:
                    lines.append("- Available-room monthly price: incomplete")
                advertised = item.get("advertised_monthly_from_yen")
                if isinstance(advertised, int):
                    lines.append(f"- Property advertised monthly from: ¥{advertised:,}")
                lines.append(
                    f"- Price validation: {item.get('price_validation', 'unknown')} · "
                    f"room status: {item.get('source_availability_status', 'unknown')}"
                )
            lines.append(
                f"- Address registration: {item.get('registration_confidence', 'unknown')}"
            )
            for caution in item.get("cautions") or ():
                lines.append(f"- Check: {caution}")
            lines.append("")
    lines.extend(("## Manual fallbacks", ""))
    for fallback in payload["manual_fallbacks"]:
        lines.append(f"- [{fallback['label']}]({fallback['url']}): {fallback['reason']}")
    lines.append("")
    return "\n".join(lines)


def _safe_csv(value: object) -> object:
    if isinstance(value, dict | list | tuple):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if isinstance(value, str) and value.lstrip()[:1] in {"=", "+", "-", "@"}:
        return "'" + value
    return value


def _write_scenario_csv(path: Path, records: list[dict[str, Any]]) -> None:
    columns = (
        "stable_key",
        "title",
        "scenario_status",
        "move_in",
        "move_out",
        "known_total_yen",
        "effective_monthly_yen",
        "complete",
        "cash_before_keys_yen",
        "cash_complete",
        "refundable_cash_yen",
        "room_area_m2",
        "mean_minutes",
        "url",
    )
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in records:
            value = {**row, **row["scenario"], "mean_minutes": row["route_metrics"]["mean_minutes"]}
            writer.writerow({key: _safe_csv(value.get(key, "")) for key in columns})
        handle.flush()
        os.fsync(handle.fileno())


def _write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    if records and "scenario" in records[0]:
        _write_scenario_csv(path, records)
        return
    columns = (
        "classification",
        "track",
        "decision_status",
        "source_label",
        "title",
        "prefecture",
        "city",
        "housing_type",
        "priority_score",
        "priority_price_component",
        "priority_distance_component",
        "priority_transit_component",
        "priority_bicycle_component",
        "priority_closer_score",
        "priority_cheaper_score",
        "pareto_candidate",
        "pareto_frontier",
        "pareto_location_score",
        "pareto_candidates",
        "pareto_frontiers",
        "pareto_location_scores",
        "pareto_distances_km",
        "distance_to_anchor_km",
        "transit_minutes_mean",
        "bicycle_minutes_mean",
        "transit_route_coverage",
        "bicycle_route_coverage",
        "commute_anchor_count",
        "latitude",
        "longitude",
        "coordinate_basis",
        "recurring_total_yen",
        "ranking_price_yen",
        "ranking_price_basis",
        "advertised_monthly_from_yen",
        "price_basis",
        "price_validation",
        "source_availability_status",
        "initial_cost_yen",
        "available_from",
        "available_text",
        "registration_confidence",
        "url",
        "stable_key",
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            writer.writerow({key: _safe_csv(record.get(key, "")) for key in columns})
        handle.flush()
        os.fsync(handle.fileno())


def _write_text(path: Path, value: str) -> None:
    with path.open("w", encoding="utf-8") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def publish_report(root: Path, payload: dict[str, Any]) -> PublishedReport:
    run_id = str(payload["run_id"])
    if run_id != Path(run_id).name or not run_id:
        raise ValueError("invalid report run ID")
    runs = root / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    staging = runs / f".staging-{run_id}"
    final = runs / run_id
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    json_path = staging / "housing.json"
    markdown_path = staging / "housing.md"
    csv_path = staging / "housing.csv"
    _write_text(
        json_path,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
    )
    _write_text(markdown_path, _markdown(payload))
    _write_csv(csv_path, payload["housing"])
    manifest = {
        "run_id": run_id,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "files": {path.name: _sha256(path) for path in (json_path, markdown_path, csv_path)},
    }
    _write_text(staging / "manifest.json", json.dumps(manifest, indent=2) + "\n")
    decoded = json.loads(json_path.read_text(encoding="utf-8"))
    if decoded.get("run_id") != run_id or decoded.get("housing_count") != len(payload["housing"]):
        raise RuntimeError("staged report validation failed")
    if final.exists():
        raise RuntimeError("report run directory already exists")
    os.replace(staging, final)
    _fsync_directory(runs)
    pointer_tmp = root / ".latest.json.tmp"
    pointer = {
        "run_id": run_id,
        "status": payload["status"],
        "path": f"runs/{run_id}/housing.json",
    }
    _write_text(pointer_tmp, json.dumps(pointer, indent=2) + "\n")
    os.replace(pointer_tmp, root / "latest.json")
    _fsync_directory(root)
    return PublishedReport(
        run_id,
        str(payload["status"]),
        final,
        final / "housing.json",
        final / "housing.md",
        final / "housing.csv",
        len(payload["housing"]),
    )


def _directory_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.iterdir() if item.is_file())


def prune_reports(
    root: Path,
    *,
    days: int,
    keep_run_id: str,
    recent_runs: int | None = None,
    max_bytes: int | None = None,
) -> int:
    runs = root / "runs"
    if not runs.is_dir():
        return 0
    cutoff = datetime.now().astimezone() - timedelta(days=days)
    removed = 0
    retained: list[tuple[Path, datetime, int]] = []
    for child in runs.iterdir():
        if not child.is_dir():
            continue
        if child.name.startswith(".staging-"):
            modified = datetime.fromtimestamp(child.stat().st_mtime).astimezone()
            if modified < cutoff:
                shutil.rmtree(child)
                removed += 1
            continue
        manifest = child / "manifest.json"
        if not manifest.is_file():
            continue
        modified = datetime.fromtimestamp(manifest.stat().st_mtime).astimezone()
        if modified < cutoff and child.name != keep_run_id:
            shutil.rmtree(child)
            removed += 1
            continue
        retained.append((child, modified, _directory_size(child)))

    retained.sort(key=lambda item: item[1], reverse=True)
    if recent_runs is not None and len(retained) > recent_runs:
        daily_seen = {
            modified.date().isoformat() for _child, modified, _size in retained[:recent_runs]
        }
        for child, modified, _size in retained[recent_runs:]:
            day = modified.date().isoformat()
            if child.name == keep_run_id or day not in daily_seen:
                daily_seen.add(day)
                continue
            shutil.rmtree(child)
            removed += 1
        retained = [item for item in retained if item[0].is_dir()]

    if max_bytes is not None:
        total = sum(size for _child, _modified, size in retained)
        for child, _modified, size in reversed(retained):
            if total <= max_bytes:
                break
            if child.name == keep_run_id:
                continue
            shutil.rmtree(child)
            total -= size
            removed += 1
    return removed


def load_latest(root: Path) -> tuple[dict[str, Any], Path]:
    pointer = json.loads((root / "latest.json").read_text(encoding="utf-8"))
    run_id = str(pointer["run_id"])
    if not run_id or run_id != Path(run_id).name:
        raise RuntimeError("latest report pointer has an invalid run ID")
    path = root / "runs" / run_id / "housing.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("run_id") != run_id:
        raise RuntimeError("latest report identity mismatch")
    return payload, path
