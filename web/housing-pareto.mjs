const NON_ACTIONABLE_DECISIONS = new Set(["reserved", "not_interested"]);

function finiteNumber(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : Number.NaN;
}

export function housingParetoCandidate(item) {
  if (item?.classification !== "eligible") return null;
  if (NON_ACTIONABLE_DECISIONS.has(item?.decision_status || "new")) return null;
  const stableKey = String(item?.stable_key || "");
  const price = finiteNumber(item?.ranking_price_yen);
  const location = finiteNumber(item?.pareto_location_score);
  if (!stableKey || price <= 0 || location < 0 || !Number.isFinite(price + location)) return null;
  return {
    stableKey,
    track: String(item?.track || "both"),
    price,
    location,
    item,
  };
}

export function housingParetoFrontierIds(records) {
  const grouped = new Map();
  for (const item of records || []) {
    const candidate = housingParetoCandidate(item);
    if (!candidate) continue;
    if (!grouped.has(candidate.track)) grouped.set(candidate.track, []);
    grouped.get(candidate.track).push(candidate);
  }

  const frontier = new Set();
  for (const candidates of grouped.values()) {
    candidates.sort((left, right) =>
      left.price - right.price ||
      left.location - right.location ||
      left.stableKey.localeCompare(right.stableKey)
    );
    let bestLocationAtLowerPrice = Number.POSITIVE_INFINITY;
    let index = 0;
    while (index < candidates.length) {
      const price = candidates[index].price;
      let end = index + 1;
      while (end < candidates.length && candidates[end].price === price) end += 1;
      const group = candidates.slice(index, end);
      const groupBestLocation = group[0].location;
      if (groupBestLocation < bestLocationAtLowerPrice) {
        for (const candidate of group) {
          if (candidate.location === groupBestLocation) frontier.add(candidate.stableKey);
        }
      }
      bestLocationAtLowerPrice = Math.min(bestLocationAtLowerPrice, groupBestLocation);
      index = end;
    }
  }
  return frontier;
}

export function housingParetoPoints(records, frontierIds = housingParetoFrontierIds(records)) {
  const points = [];
  for (const item of records || []) {
    const candidate = housingParetoCandidate(item);
    if (!candidate) continue;
    points.push({
      ...candidate,
      frontier: frontierIds.has(candidate.stableKey),
    });
  }
  return points.sort((left, right) =>
    left.track.localeCompare(right.track) ||
    left.location - right.location ||
    left.price - right.price ||
    left.stableKey.localeCompare(right.stableKey)
  );
}
