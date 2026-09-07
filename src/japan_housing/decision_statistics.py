"""Descriptive statistics for the actual filtered listings, with explicit denominators."""

from __future__ import annotations

import math
from collections import Counter
from statistics import median
from typing import Any

from .decision import tradeoff_candidate


def distribution(values: list) -> dict[str, Any]:
    observed = sorted(
        v
        for v in values
        if isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)
    )
    return {
        "n": len(observed),
        "missing": len(values) - len(observed),
        "min": observed[0] if observed else None,
        "median": median(observed) if observed else None,
        "max": observed[-1] if observed else None,
    }


def filtered_statistics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    potential = [r for r in rows if tradeoff_candidate(r)]
    complete = [r for r in potential if r["scenario"]["complete"]]
    incomplete = [r for r in potential if not r["scenario"]["complete"]]
    issues: Counter[str] = Counter()
    for r in potential:
        issues.update(set(r["scenario"]["unknown_costs"]))
    return {
        "filtered": len(rows),
        "potential": len(potential),
        "status_counts": dict(Counter(r["scenario_status"] for r in rows)),
        "source_count": len({r["source_id"] for r in potential}),
        "complete_monthly": distribution([r["ranking_price_yen"] for r in complete]),
        "known_minimum_monthly": distribution([r["ranking_price_yen"] for r in incomplete]),
        "area_m2": distribution([r.get("room_area_m2") for r in potential]),
        "cash_yen": distribution(
            [
                r["scenario"]["cash_before_keys_yen"] if r["scenario"]["cash_complete"] else None
                for r in potential
            ]
        ),
        "cost_coverage": {"known": len(complete), "total": len(potential)},
        "route_coverage": {
            "known": sum(r["route_metrics"]["complete"] for r in potential),
            "total": len(potential),
        },
        "common_cost_gaps": [
            {"field": field, "listings": count}
            for field, count in sorted(issues.items(), key=lambda item: (-item[1], item[0]))[:5]
        ],
        "note": "Descriptive listing counts, not unique leases or a representative market sample. "
        "Complete quotes and known minimums are summarized separately.",
    }
