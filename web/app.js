"use strict";

const controls = {
  query: document.querySelector("#query"),
  classification: document.querySelector("#classification"),
  track: document.querySelector("#track"),
  decision: document.querySelector("#decision"),
  maxCost: document.querySelector("#max-cost"),
};
const state = { report: null, selected: new Set() };

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
  return Number.isInteger(value) ? `¥${value.toLocaleString()}` : "Unknown";
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

function matches(item) {
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

function detailBlock(item) {
  const wrap = node("div", { className: "property-cell" });
  wrap.append(safeLink(item.url, item.title || "Untitled housing"));
  wrap.append(node("span", {
    className: `badge ${item.classification}`,
    text: `${item.classification} · ${item.track}`,
  }));
  const location = [item.prefecture, item.city].filter(Boolean).join(" ") || "Location unverified";
  wrap.append(node("small", { text: location }));
  if (item.nearest_stations?.length) {
    wrap.append(node("small", { text: item.nearest_stations.join(" · ") }));
  }
  const checks = (item.cautions || []).slice(0, 3);
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
      if (!response.ok) throw new Error((await response.json()).error || "Decision update failed");
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
    className: selected ? "compare active" : "compare",
    text: selected ? "Added" : "Add",
    ariaLabel: `${selected ? "Remove" : "Add"} ${item.title} ${selected ? "from" : "to"} comparison`,
  });
  button.addEventListener("click", () => {
    if (selected) state.selected.delete(item.stable_key);
    else if (state.selected.size < 3) state.selected.add(item.stable_key);
    else showError("Compare up to three listings at a time.");
    render();
  });
  return button;
}

function renderRows(records) {
  const body = document.querySelector("#results");
  body.replaceChildren();
  for (const item of records) {
    const row = node("tr");
    row.append(node("td", {}, [compareButton(item)]));
    row.append(node("td", {}, [detailBlock(item)]));
    const distance = Number.isFinite(item.distance_to_anchor_km)
      ? `${item.distance_to_anchor_km.toFixed(2)} km to Shibuya`
      : "Distance penalty applied";
    row.append(node("td", {}, [
      node("strong", { text: Number.isFinite(item.priority_score) ? item.priority_score.toFixed(2) : "Unknown" }),
      node("small", { text: distance }),
      node("small", { text: "60% price · 40% straight-line distance" }),
    ]));
    const monthly = node("td");
    monthly.append(node("strong", { text: yen(item.recurring_total_yen) }));
    if (item.source_id === "oakhouse") {
      monthly.append(node("small", {
        text: `Available room: rent ${yen(item.rent_yen)} + maintenance ${yen(item.management_fee_yen)}`,
      }));
      if (Number.isInteger(item.advertised_monthly_from_yen)) {
        monthly.append(node("small", {
          className: item.advertised_monthly_from_yen === item.recurring_total_yen ? "" : "caution",
          text: `Property advertised from ${yen(item.advertised_monthly_from_yen)}`,
        }));
      }
      monthly.append(node("small", {
        text: `Validation: ${item.price_validation || "unknown"}`,
      }));
    }
    monthly.append(node("small", { text: `Initial: ${yen(item.initial_cost_yen)}` }));
    row.append(monthly);
    row.append(node("td", { text: item.available_text || item.available_from || "Confirm" }));
    row.append(node("td", {}, [
      node("span", {
        className: `registration ${item.registration_confidence || "unknown"}`,
        text: item.registration_confidence || "unknown",
      }),
    ]));
    row.append(node("td", {}, [decisionControl(item)]));
    body.append(row);
  }
}

function scenarioText(item) {
  const scenario = item.track === "landing" ? item.landing_90 : item.long_term;
  if (!scenario) return "Scenario unavailable";
  const complete = scenario.complete ? "complete" : `check: ${(scenario.unknown_costs || []).join(", ")}`;
  return `${scenario.months} months: ${yen(scenario.known_total_yen)} · ${complete}`;
}

function renderCompare(records) {
  const section = document.querySelector("#compare-section");
  const container = document.querySelector("#compare");
  const selected = records.filter((item) => state.selected.has(item.stable_key));
  section.hidden = selected.length === 0;
  container.replaceChildren();
  for (const item of selected) {
    const card = node("article", { className: "compare-card" });
    card.append(safeLink(item.url, item.title));
    card.append(node("strong", { text: `${yen(item.recurring_total_yen)}/month` }));
    card.append(node("span", {
      text: `Priority ${Number.isFinite(item.priority_score) ? item.priority_score.toFixed(2) : "unknown"} · ${
        Number.isFinite(item.distance_to_anchor_km)
          ? `${item.distance_to_anchor_km.toFixed(2)} km straight-line to Shibuya`
          : "distance penalty applied"
      }`,
    }));
    if (item.source_id === "oakhouse") {
      card.append(node("span", {
        text: `Available room: ${yen(item.rent_yen)} rent + ${yen(item.management_fee_yen)} maintenance`,
      }));
      card.append(node("span", {
        text: `Advertised from: ${yen(item.advertised_monthly_from_yen)} · validation: ${item.price_validation}`,
      }));
    }
    card.append(node("span", { text: scenarioText(item) }));
    card.append(node("span", { text: `Available: ${item.available_text || "confirm"}` }));
    card.append(node("span", { text: `Registration: ${item.registration_confidence}` }));
    card.append(node("span", { text: `Source: ${item.source_label}` }));
    container.append(card);
  }
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

function showError(message) {
  const box = document.querySelector("#error");
  box.textContent = message;
  box.hidden = !message;
}

function render() {
  if (!state.report) return;
  showError("");
  const records = (state.report.housing || []).filter(matches);
  document.querySelector("#result-count").textContent = `${records.length} shown`;
  document.querySelector("#summary").textContent =
    `${state.report.housing_count} listings · ${state.report.status} snapshot · updated ${state.report.generated_at}`;
  renderRows(records);
  renderCompare(state.report.housing || []);
  renderFallbacks();
}

async function load() {
  showError("");
  try {
    const response = await fetch("/api/report", { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not load the report");
    state.report = payload;
    const keys = new Set((payload.housing || []).map((item) => item.stable_key));
    state.selected = new Set([...state.selected].filter((key) => keys.has(key)));
    render();
  } catch (error) {
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
  render();
});
document.querySelector("#clear-compare").addEventListener("click", () => {
  state.selected.clear();
  render();
});

load();
