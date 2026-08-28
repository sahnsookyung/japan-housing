"use strict";

import {
  housingParetoFrontierIds,
  housingParetoLocation,
  housingParetoPoints,
} from "/housing-pareto.mjs";

const SVG_NS = "http://www.w3.org/2000/svg";
const controls = {
  query: document.querySelector("#query"),
  classification: document.querySelector("#classification"),
  track: document.querySelector("#track"),
  decision: document.querySelector("#decision"),
  maxCost: document.querySelector("#max-cost"),
  rankingView: document.querySelector("#ranking-view"),
  paretoPerspective: document.querySelector("#pareto-perspective"),
};
const state = {
  report: null,
  selected: new Set(),
  focusedKey: "",
};

function node(tag, options = {}, children = []) {
  const element = document.createElement(tag);
  if (options.className) element.className = options.className;
  if (options.text !== undefined) element.textContent = String(options.text);
  if (options.type) element.type = options.type;
  if (options.value !== undefined) element.value = options.value;
  if (options.title) element.title = options.title;
  if (options.ariaLabel) element.setAttribute("aria-label", options.ariaLabel);
  for (const child of children) element.append(child);
  return element;
}

function svgNode(tag, attributes = {}, text = "") {
  const element = document.createElementNS(SVG_NS, tag);
  for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, String(value));
  if (text) element.textContent = text;
  return element;
}

function safeLink(url, label) {
  const link = node("a", { text: label });
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:") throw new Error("unsupported URL");
    link.href = parsed.href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
  } catch {
    link.removeAttribute("href");
  }
  return link;
}

function yen(value) {
  return Number.isFinite(value) ? `¥${Math.round(value).toLocaleString()}` : "Unknown";
}

function listingText(item) {
  return [
    item.title,
    item.property_name,
    item.prefecture,
    item.city,
    ...(item.nearest_stations || []),
  ].join(" ").toLocaleLowerCase();
}

function matchesBase(item) {
  const query = controls.query.value.trim().toLocaleLowerCase();
  const classification = controls.classification.value;
  const maxCost = Number(controls.maxCost.value || 0);
  if (query && !listingText(item).includes(query)) return false;
  if (classification === "actionable" && item.classification === "excluded") return false;
  if (!["actionable", "all"].includes(classification) && item.classification !== classification) return false;
  if (controls.track.value !== "all" && item.track !== controls.track.value) return false;
  if (controls.decision.value !== "all" && item.decision_status !== controls.decision.value) return false;
  if (maxCost && (!Number.isInteger(item.recurring_total_yen) || item.recurring_total_yen > maxCost)) return false;
  return true;
}

function priorityFor(item) {
  if (controls.rankingView.value === "closer") return item.priority_closer_score;
  if (controls.rankingView.value === "cheaper") return item.priority_cheaper_score;
  return item.priority_score;
}

function anchorLabel() {
  return state.report?.ranking?.anchor_label || "configured midpoint";
}

function paretoPerspectiveId() {
  return controls.paretoPerspective?.value || "balanced";
}

function paretoPerspective() {
  const perspectiveId = paretoPerspectiveId();
  return (state.report?.ranking?.pareto?.perspectives || []).find(
    (perspective) => perspective.id === perspectiveId,
  ) || {
    id: "balanced",
    label: "All anchors (balanced)",
    kind: "aggregate",
    distance_target: anchorLabel(),
  };
}

function perspectiveLocation(item) {
  return housingParetoLocation(item, paretoPerspectiveId());
}

function perspectiveDistance(item) {
  const perspectiveId = paretoPerspectiveId();
  const distance = item?.pareto_distances_km?.[perspectiveId];
  if (Number.isFinite(distance)) return distance;
  return perspectiveId === "balanced" ? item?.distance_to_anchor_km : Number.NaN;
}

function perspectiveRoute(item) {
  if (paretoPerspectiveId() === "balanced") return null;
  return (item.commute_routes || []).find(
    (route) => route.anchor_id === paretoPerspectiveId(),
  ) || null;
}

function commuteText(item, mode) {
  const perspective = paretoPerspective();
  if (perspective.kind === "anchor") {
    const route = perspectiveRoute(item);
    const observed = route?.[`${mode}_minutes`];
    const penalty = state.report?.ranking?.[
      mode === "transit"
        ? "missing_transit_penalty_minutes"
        : "missing_bicycle_penalty_minutes"
    ];
    const label = mode === "transit" ? "Transit / bus" : "Bicycle";
    if (Number.isFinite(observed)) {
      return `${label}: ${observed.toFixed(1)} min to ${perspective.label}`;
    }
    const penaltyText = Number.isFinite(penalty) ? `${penalty.toFixed(1)} min penalty` : "penalty";
    return `${label}: pending to ${perspective.label}; ${penaltyText} ranked`;
  }
  const observed = item[`${mode}_minutes_mean`];
  const scored = item[`${mode}_minutes_scored`];
  const coverage = Number(item[`${mode}_route_coverage`] || 0);
  const anchorCount = Number(item.commute_anchor_count || 0);
  const label = mode === "transit" ? "Transit / bus" : "Bicycle";
  const scoredText = Number.isFinite(scored) ? `${scored.toFixed(1)} min ranked` : "ranking unavailable";
  if (Number.isFinite(observed)) {
    return `${label}: ${observed.toFixed(1)} min observed (${coverage}/${anchorCount}); ${scoredText}`;
  }
  return `${label}: pending (${coverage}/${anchorCount}); ${scoredText}`;
}

function compareRank(left, right) {
  if (controls.rankingView.value === "pareto") {
    const leftLocation = perspectiveLocation(left);
    const rightLocation = perspectiveLocation(right);
    return (
      (Number.isFinite(leftLocation) ? leftLocation : Number.POSITIVE_INFINITY) -
        (Number.isFinite(rightLocation) ? rightLocation : Number.POSITIVE_INFINITY) ||
      (left.ranking_price_yen ?? Number.POSITIVE_INFINITY) -
        (right.ranking_price_yen ?? Number.POSITIVE_INFINITY) ||
      String(left.stable_key).localeCompare(String(right.stable_key))
    );
  }
  const leftKey = [...(left.rank_key || [999])];
  const rightKey = [...(right.rank_key || [999])];
  leftKey[3] = priorityFor(left);
  rightKey[3] = priorityFor(right);
  for (let index = 0; index < Math.max(leftKey.length, rightKey.length); index += 1) {
    const a = leftKey[index] ?? "";
    const b = rightKey[index] ?? "";
    if (a < b) return -1;
    if (a > b) return 1;
  }
  return 0;
}

function detailBlock(item, frontier) {
  const wrap = node("div", { className: "property-cell" });
  const badges = node("div", { className: "badge-row" });
  badges.append(node("span", {
    className: `badge badge-${item.classification}`,
    text: `${item.classification} · ${item.track}`,
  }));
  if (frontier) badges.append(node("span", { className: "badge badge-frontier", text: "Pareto" }));
  wrap.append(badges);
  wrap.append(safeLink(item.url, item.title || "Untitled housing"));
  const location = [item.prefecture, item.city].filter(Boolean).join(" ") || "Location unverified";
  wrap.append(node("small", { text: location }));
  if (item.nearest_stations?.length) wrap.append(node("small", { text: item.nearest_stations.join(" · ") }));
  const checks = (item.cautions || []).slice(0, 2);
  if (checks.length) wrap.append(node("small", { className: "caution", text: checks.join(" · ") }));
  return wrap;
}

function decisionControl(item) {
  const select = node("select", { ariaLabel: `Decision for ${item.title}` });
  for (const value of ["new", "shortlisted", "contacted", "application_started", "reserved", "not_interested"]) {
    const option = node("option", { value, text: value.replaceAll("_", " ") });
    option.selected = item.decision_status === value;
    select.append(option);
  }
  select.addEventListener("change", async () => {
    select.disabled = true;
    try {
      const response = await fetch("/api/decisions", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Japan-Housing": "1" },
        body: JSON.stringify({ stable_key: item.stable_key, status: select.value }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "Decision update failed");
      item.decision_status = select.value;
      render();
    } catch (error) {
      showError(error.message);
    } finally {
      select.disabled = false;
    }
  });
  return select;
}

function compareButton(item) {
  const selected = state.selected.has(item.stable_key);
  const button = node("button", {
    type: "button",
    className: "compare-toggle",
    text: selected ? "✓" : "+",
    ariaLabel: `${selected ? "Remove" : "Add"} ${item.title} ${selected ? "from" : "to"} comparison`,
    title: selected ? "Remove from comparison" : "Add to comparison",
  });
  button.setAttribute("aria-pressed", String(selected));
  button.addEventListener("click", () => {
    if (selected) state.selected.delete(item.stable_key);
    else if (state.selected.size < 3) state.selected.add(item.stable_key);
    else showError("Compare up to three homes at a time.");
    render();
  });
  return button;
}

function renderRows(records, frontierIds) {
  const body = document.querySelector("#results");
  body.replaceChildren();
  document.querySelector("#empty-state").hidden = records.length !== 0;
  for (const item of records) {
    const frontier = frontierIds.has(item.stable_key);
    const row = node("tr", { className: frontier ? "is-frontier" : "" });
    row.dataset.stableKey = item.stable_key;
    row.append(node("td", { className: "compare-cell" }, [compareButton(item)]));
    row.append(node("td", {}, [detailBlock(item, frontier)]));

    const monthly = node("td", { className: "cost-cell" });
    monthly.append(node("strong", { text: `${yen(item.ranking_price_yen)} effective` }));
    monthly.append(node("small", { text: `${yen(item.recurring_total_yen)} recurring` }));
    monthly.append(node("small", { text: `Initial: ${yen(item.initial_cost_yen)}` }));
    if (item.source_id === "oakhouse") {
      monthly.append(node("small", {
        className: Number.isInteger(item.advertised_monthly_from_yen) && item.advertised_monthly_from_yen !== item.recurring_total_yen ? "caution" : "",
        text: `Advertised from ${yen(item.advertised_monthly_from_yen)} · ${item.price_validation || "unvalidated"}`,
      }));
    }
    row.append(monthly);

    const location = node("td", { className: "location-cell" });
    const locationScore = perspectiveLocation(item);
    const distance = perspectiveDistance(item);
    const perspective = paretoPerspective();
    location.append(node("strong", {
      text: Number.isFinite(locationScore)
        ? `${locationScore.toFixed(2)} burden`
        : "Location unavailable",
    }));
    location.append(node("small", {
      text: Number.isFinite(distance)
        ? `${distance.toFixed(2)} km to ${perspective.distance_target}`
        : "Distance penalty applied",
    }));
    location.append(node("small", { text: commuteText(item, "transit") }));
    location.append(node("small", { text: commuteText(item, "bicycle") }));
    row.append(location);

    row.append(node("td", { className: "availability-cell" }, [
      node("strong", { text: item.available_text || item.available_from || "Confirm" }),
      node("span", {
        className: `registration registration-${item.registration_confidence || "unknown"}`,
        text: `Registration ${item.registration_confidence || "unknown"}`,
      }),
    ]));
    row.append(node("td", { className: "decision-cell" }, [decisionControl(item)]));
    body.append(row);
  }
}

function scenarioText(item) {
  const scenario = item.track === "landing" ? item.landing_90 : item.long_term;
  if (!scenario) return "Scenario unavailable";
  const complete = scenario.complete ? "complete" : `check ${(scenario.unknown_costs || []).join(", ")}`;
  return `${scenario.months} months: ${yen(scenario.known_total_yen)} · ${complete}`;
}

function renderCompare() {
  const section = document.querySelector("#compare-section");
  const container = document.querySelector("#compare");
  const selected = (state.report?.housing || []).filter((item) => state.selected.has(item.stable_key));
  section.hidden = selected.length === 0;
  container.replaceChildren();
  for (const item of selected) {
    const locationScore = perspectiveLocation(item);
    const distance = perspectiveDistance(item);
    const perspective = paretoPerspective();
    const card = node("article", { className: "compare-card" });
    card.append(safeLink(item.url, item.title));
    card.append(node("strong", { text: `${yen(item.ranking_price_yen)} effective / month` }));
    card.append(node("span", { text: `${yen(item.recurring_total_yen)} recurring · ${scenarioText(item)}` }));
    card.append(node("span", { text: commuteText(item, "transit") }));
    card.append(node("span", { text: commuteText(item, "bicycle") }));
    card.append(node("span", {
      text: `${Number.isFinite(distance) ? `${distance.toFixed(2)} km to ${perspective.distance_target}` : "distance penalty"} · location burden ${Number.isFinite(locationScore) ? locationScore.toFixed(2) : "unknown"}`,
    }));
    card.append(node("span", { text: `Available: ${item.available_text || "confirm"}` }));
    card.append(node("span", { text: `Registration: ${item.registration_confidence || "unknown"}` }));
    container.append(card);
  }
}

function chartDetail(item, frontier) {
  const container = document.querySelector("#chart-detail");
  container.replaceChildren();
  if (!item) {
    container.textContent = "Select a chart point to inspect its housing trade-off.";
    return;
  }
  const locationScore = perspectiveLocation(item);
  container.append(node("span", { className: frontier ? "badge badge-frontier" : "badge", text: frontier ? "Pareto frontier" : "Comparable" }));
  container.append(safeLink(item.url, item.title || "Untitled housing"));
  container.append(node("strong", {
    text: `${yen(item.ranking_price_yen)} effective · ${Number.isFinite(locationScore) ? locationScore.toFixed(2) : "unknown"} location burden`,
  }));
  container.append(node("small", {
    text: `${paretoPerspective().label} · ${item.track} · ${item.city || item.prefecture || "location unverified"}`,
  }));
}

function scale(value, minimum, maximum, start, end) {
  if (maximum === minimum) return (start + end) / 2;
  return start + ((value - minimum) / (maximum - minimum)) * (end - start);
}

function renderPareto(records, frontierIds) {
  const container = document.querySelector("#pareto-chart");
  const perspective = paretoPerspective();
  const points = housingParetoPoints(records, frontierIds, perspective.id);
  container.replaceChildren();
  document.querySelector("#pareto-total").textContent = `${frontierIds.size} efficient`;
  const focused = points.find((point) => point.stableKey === state.focusedKey);
  if (!points.length) {
    container.append(node("div", { className: "chart-empty", text: "No eligible homes with comparable price and location metrics." }));
    chartDetail(null, false);
    return;
  }

  const width = 680;
  const height = 430;
  const padding = { top: 28, right: 28, bottom: 58, left: 78 };
  const prices = points.map((point) => point.price);
  const locations = points.map((point) => point.location);
  const rawPriceMin = Math.min(...prices);
  const rawPriceMax = Math.max(...prices);
  const rawLocationMin = Math.min(...locations);
  const rawLocationMax = Math.max(...locations);
  const priceMargin = Math.max(5_000, (rawPriceMax - rawPriceMin) * 0.08);
  const locationMargin = Math.max(5, (rawLocationMax - rawLocationMin) * 0.08);
  const priceMin = Math.max(0, rawPriceMin - priceMargin);
  const priceMax = rawPriceMax + priceMargin;
  const locationMin = Math.max(0, rawLocationMin - locationMargin);
  const locationMax = rawLocationMax + locationMargin;
  const x = (value) => scale(value, locationMin, locationMax, padding.left, width - padding.right);
  const y = (value) => scale(value, priceMin, priceMax, height - padding.bottom, padding.top);

  const svg = svgNode("svg", {
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-label": `${perspective.label}: ${points.length} comparable homes; ${frontierIds.size} are on the Pareto frontier`,
  });
  for (let index = 0; index <= 4; index += 1) {
    const ratio = index / 4;
    const xPosition = padding.left + ratio * (width - padding.left - padding.right);
    const yPosition = padding.top + ratio * (height - padding.top - padding.bottom);
    svg.append(svgNode("line", { x1: xPosition, y1: padding.top, x2: xPosition, y2: height - padding.bottom, class: "chart-grid" }));
    svg.append(svgNode("line", { x1: padding.left, y1: yPosition, x2: width - padding.right, y2: yPosition, class: "chart-grid" }));
    svg.append(svgNode("text", { x: xPosition, y: height - 31, class: "chart-tick", "text-anchor": "middle" }, (locationMin + ratio * (locationMax - locationMin)).toFixed(0)));
    svg.append(svgNode("text", { x: padding.left - 10, y: yPosition + 4, class: "chart-tick", "text-anchor": "end" }, yen(priceMax - ratio * (priceMax - priceMin))));
  }
  svg.append(svgNode("text", { x: (padding.left + width - padding.right) / 2, y: height - 8, class: "chart-axis", "text-anchor": "middle" }, `${perspective.label} location burden (lower is better)`));
  const yLabel = svgNode("text", { x: 18, y: height / 2, class: "chart-axis", "text-anchor": "middle", transform: `rotate(-90 18 ${height / 2})` }, "Effective monthly price (lower is better)");
  svg.append(yLabel);

  const tracks = new Map();
  for (const point of points.filter((entry) => entry.frontier)) {
    if (!tracks.has(point.track)) tracks.set(point.track, []);
    tracks.get(point.track).push(point);
  }
  for (const frontier of tracks.values()) {
    frontier.sort((left, right) => left.location - right.location || left.price - right.price);
    if (frontier.length > 1) {
      svg.append(svgNode("polyline", {
        points: frontier.map((point) => `${x(point.location)},${y(point.price)}`).join(" "),
        class: "frontier-line",
      }));
    }
  }

  for (const point of points) {
    const circle = svgNode("circle", {
      cx: x(point.location),
      cy: y(point.price),
      r: point.frontier ? 7 : 4.5,
      class: `${point.frontier ? "chart-point chart-point-frontier" : "chart-point"}${point.stableKey === state.focusedKey ? " is-focused" : ""}`,
      tabindex: "0",
      role: "button",
      "data-stable-key": point.stableKey,
      "data-frontier": String(point.frontier),
      "aria-label": `${point.item.title}; ${yen(point.price)} effective monthly; ${point.location.toFixed(2)} ${perspective.label} location burden; ${point.frontier ? "Pareto frontier" : "comparable"}`,
    });
    circle.append(svgNode("title", {}, `${point.item.title} · ${yen(point.price)} · ${perspective.label} burden ${point.location.toFixed(2)}`));
    const selectPoint = () => {
      state.focusedKey = point.stableKey;
      renderPareto(records, frontierIds);
      document.querySelector(`tr[data-stable-key="${CSS.escape(point.stableKey)}"]`)?.scrollIntoView({ block: "nearest" });
    };
    circle.addEventListener("click", selectPoint);
    circle.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        selectPoint();
      }
    });
    svg.append(circle);
  }
  container.append(svg);
  chartDetail(focused?.item || null, focused?.frontier || false);
}

function renderFallbacks() {
  const container = document.querySelector("#fallbacks");
  container.replaceChildren();
  for (const item of state.report?.manual_fallbacks || []) {
    const card = node("article", { className: "fallback-card" });
    card.append(safeLink(item.url, item.label));
    card.append(node("p", { text: item.reason }));
    container.append(card);
  }
}

function renderSourceHealth() {
  const container = document.querySelector("#source-health");
  container.replaceChildren();
  for (const item of state.report?.source_health || []) {
    const status = item.last_status || "not run";
    const card = node("article", { className: "source-card" });
    card.append(node("span", { className: `status-dot status-${status}`, ariaLabel: status }));
    card.append(node("strong", { text: item.label || item.source_id }));
    card.append(node("small", { text: status.replaceAll("_", " ") }));
    container.append(card);
  }
}

function renderSummary() {
  const counts = state.report?.classification_counts || {};
  const coverage = Math.round((state.report?.commute_coverage?.coverage_ratio || 0) * 100);
  document.querySelector("#summary-eligible").textContent = counts.eligible || 0;
  document.querySelector("#summary-review").textContent = counts.review || 0;
  const pareto = state.report?.ranking?.pareto || {};
  document.querySelector("#summary-pareto").textContent =
    pareto.frontier_counts?.[paretoPerspectiveId()] ?? pareto.frontier_count ?? 0;
  document.querySelector("#summary-coverage").textContent = `${coverage}%`;
  document.querySelector("#anchor-label").textContent = anchorLabel();
  document.querySelector("#summary").textContent =
    `${state.report.housing_count} listings · ${state.report.status} snapshot · updated ${state.report.generated_at}`;
  document.querySelector("#refresh-state").textContent =
    `${state.report.status} · ${coverage}% routes`;
}

function showError(message) {
  const box = document.querySelector("#error");
  box.textContent = message;
  box.hidden = !message;
}

function render() {
  if (!state.report) return;
  showError("");
  const baseRecords = (state.report.housing || []).filter(matchesBase);
  const perspective = paretoPerspective();
  const frontierIds = housingParetoFrontierIds(baseRecords, perspective.id);
  const records = (controls.rankingView.value === "pareto"
    ? baseRecords.filter((item) => frontierIds.has(item.stable_key))
    : baseRecords
  ).sort(compareRank);

  document.querySelector("#result-count").textContent = records.length;
  document.querySelector("#frontier-feedback").textContent =
    `${records.length} shown · ${frontierIds.size} ${perspective.label} efficient among ${baseRecords.length} filtered`;
  document.querySelector("#pareto-heading").textContent = `${perspective.label} frontier`;
  document.querySelector("#pareto-axis-context").textContent =
    `PRICE ↓ · ${perspective.label.toUpperCase()} BURDEN ↓`;
  const routeCoverage = Math.round((state.report.commute_coverage?.coverage_ratio || 0) * 100);
  const distanceTarget = perspective.distance_target || perspective.label;
  document.querySelector("#pareto-caption").textContent = routeCoverage === 100
    ? `${perspective.label} burden uses complete transit/bus and bicycle evidence plus straight-line distance to ${distanceTarget}. Frontiers are independent by housing track and recomputed after every filter.`
    : `Overall route coverage is ${routeCoverage}%; missing ${perspective.label} route elements retain configured penalties, with straight-line distance measured to ${distanceTarget}. Frontiers are independent by housing track and recomputed after every filter.`;

  renderRows(records, frontierIds);
  renderPareto(baseRecords, frontierIds);
  renderCompare();
  renderSummary();
  renderFallbacks();
  renderSourceHealth();
}

async function load() {
  showError("");
  document.querySelector("#refresh-state").textContent = "Refreshing report…";
  try {
    const response = await fetch("/api/report", { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not load the report");
    state.report = payload;
    const currentPerspective = paretoPerspectiveId();
    const perspectives = payload.ranking?.pareto?.perspectives || [];
    controls.paretoPerspective.replaceChildren(
      ...perspectives.map((perspective) => node("option", {
        value: perspective.id,
        text: perspective.label,
      })),
    );
    if (!perspectives.length) {
      controls.paretoPerspective.append(node("option", {
        value: "balanced",
        text: "All anchors (balanced)",
      }));
    }
    controls.paretoPerspective.value = perspectives.some(
      (perspective) => perspective.id === currentPerspective,
    ) ? currentPerspective : "balanced";
    for (const [name, weights] of Object.entries(payload.ranking?.variants || {})) {
      const option = controls.rankingView.querySelector(`option[value="${name}"]`);
      if (option) option.textContent = `${name[0].toUpperCase()}${name.slice(1)} · ${Math.round(weights.price_weight * 100)}% price`;
    }
    const keys = new Set((payload.housing || []).map((item) => item.stable_key));
    state.selected = new Set([...state.selected].filter((key) => keys.has(key)));
    if (!keys.has(state.focusedKey)) state.focusedKey = "";
    render();
  } catch (error) {
    document.querySelector("#refresh-state").textContent = "Report unavailable";
    showError(error.message);
  }
}

for (const control of Object.values(controls)) control.addEventListener("input", render);
document.querySelector("#refresh").addEventListener("click", load);
document.querySelector("#reset").addEventListener("click", () => {
  controls.query.value = "";
  controls.classification.value = "actionable";
  controls.track.value = "all";
  controls.decision.value = "all";
  controls.maxCost.value = "80000";
  controls.rankingView.value = "balanced";
  controls.paretoPerspective.value = "balanced";
  render();
});
document.querySelector("#clear-compare").addEventListener("click", () => {
  state.selected.clear();
  render();
});
document.querySelector("#summary-eligible-card").addEventListener("click", () => {
  controls.classification.value = "eligible";
  controls.rankingView.value = "balanced";
  render();
});
document.querySelector("#summary-review-card").addEventListener("click", () => {
  controls.classification.value = "review";
  controls.rankingView.value = "balanced";
  render();
});
document.querySelector("#summary-pareto-card").addEventListener("click", () => {
  controls.classification.value = "eligible";
  controls.rankingView.value = "pareto";
  render();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "/" && document.activeElement?.tagName !== "INPUT") {
    event.preventDefault();
    controls.query.focus();
  }
});

load();
