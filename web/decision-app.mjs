import { createHousingMap } from "./decision-map.mjs";
import { createPinHistory } from "./pin-history.mjs";
import { factFields, roomFactUpdate } from "./room-evidence.mjs";
import {
  defaultView,
  readViewState,
  viewSearch,
  readSelection,
  writeSelection,
} from "./view-state.mjs";
import {
  tradeoffChart,
  costChart,
  addCalendarMonths,
} from "./decision-charts.mjs";
const $ = (id) => document.getElementById(id);
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const yen = (value) =>
  value == null ? "Unknown" : `¥${Math.round(value).toLocaleString()}`;
const amount = (value, complete) =>
  value == null ? "Unknown" : `${complete ? "" : "≥ "}${yen(value)}`;
const expense = (s) =>
  s.cost_upper_yen != null && s.cost_upper_yen > s.known_total_yen
    ? `${yen(s.known_total_yen)}–${yen(s.cost_upper_yen)}`
    : amount(s.known_total_yen, s.complete);
const textValue = (value) =>
  value == null
    ? "Unverified"
    : value === true
      ? "Yes"
      : value === false
        ? "No"
        : String(value);
const link = (url, label) => {
  try {
    const u = new URL(url);
    if (u.protocol !== "https:") return esc(label);
    return `<a href="${esc(u.href)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>`;
  } catch {
    return esc(label);
  }
};
const initialView = readViewState(location.search);
const selectionStorage = {
  getItem: (key) => sessionStorage.getItem(key),
  setItem: (key, value) => sessionStorage.setItem(key, value),
};
let activeView = initialView.view,
  destination = initialView.destination,
  visualKeys = {},
  sourceKey,
  comparisonKey,
  comparisonController,
  comparisonSequence = 0;
const selectedTitles = new Map();
let prefsReady = false,
  prefs,
  snapshot,
  offset = initialView.offset,
  controller,
  sequence = 0,
  selected = new Set(readSelection(selectionStorage)),
  openId = null,
  detailSequence = 0,
  busy = false;
const form = $("scenario-form"),
  destForm = $("destination-form");
const notice = (message) => {
  $("notice").textContent = message;
};
const pinHistory = createPinHistory(
  {
    getItem: (key) => localStorage.getItem(key),
    setItem: (key, value) => localStorage.setItem(key, value),
  },
  () =>
    notice(
      "Pin undo is available in this tab, but browser storage is unavailable.",
    ),
);
function renderPinUndo() {
  const change = prefs && pinHistory.latest(prefs.destinations);
  $("undo-pin").disabled = !change || busy;
  $("undo-pin").title = change
    ? `Restore the previous pin for ${change.current.label}`
    : "No saved pin change to undo";
}
async function confirmPinMove(before, after) {
  if (
    before.latitude === after.latitude &&
    before.longitude === after.longitude
  )
    return true;
  const dialog = $("pin-dialog");
  const coordinates = (d) =>
    d.latitude == null
      ? "No pin"
      : `${d.latitude.toFixed(6)}, ${d.longitude.toFixed(6)}`;
  $("pin-location").textContent = before.label;
  $("pin-before").textContent = coordinates(before);
  $("pin-after").textContent = coordinates(after);
  dialog.returnValue = "cancel";
  const closed = new Promise((resolve) =>
    dialog.addEventListener(
      "close",
      () => resolve(dialog.returnValue === "confirm"),
      { once: true },
    ),
  );
  dialog.showModal();
  return closed;
}
const error = (message) => {
  $("error").textContent = message;
  $("error").hidden = !message;
  let alert = $("dialog-error");
  if ($("room-dialog").open) {
    if (!alert) {
      alert = document.createElement("p");
      alert.id = "dialog-error";
      alert.setAttribute("role", "alert");
      $("room-dialog").prepend(alert);
    }
    alert.textContent = message;
    alert.hidden = !message;
    if (message) alert.scrollIntoView({ block: "nearest" });
  }
};
async function api(url, payload, signal) {
  if (!payload)
    signal = signal
      ? AbortSignal.any([signal, AbortSignal.timeout(15000)])
      : AbortSignal.timeout(15000);
  const response = await fetch(url, {
    signal,
    headers: payload
      ? { "Content-Type": "application/json", "X-Japan-Housing": "1" }
      : {},
    method: payload ? "POST" : "GET",
    body: payload ? JSON.stringify(payload) : undefined,
  });
  let result;
  try {
    result = await response.json();
  } catch {
    throw new Error(
      `Server returned ${response.status}. Retry loading the snapshot.`,
    );
  }
  if (!response.ok)
    throw new Error(result.error || `Request failed (${response.status})`);
  return result;
}
function run(fn) {
  return async (event) => {
    try {
      await fn(event);
    } catch (e) {
      if (e.name !== "AbortError") error(e.message);
    }
  };
}
function params() {
  const p = new URLSearchParams();
  for (const key of ["query", "status", "sort", "decision"])
    p.set(key, $(key).value);
  p.set("offset", offset);
  p.set("limit", 50);
  p.set("metric", $("chart-metric").value);
  p.set("destination", destination);
  return p;
}
function persistView() {
  const state = Object.fromEntries(params());
  state.offset = offset;
  state.view = activeView;
  const query = viewSearch(state);
  history.replaceState(
    null,
    "",
    location.pathname + (query ? `?${query}` : "") + location.hash,
  );
}
const map = createHousingMap({
  onEdit(d) {
    showDestinationEditor(true);
    editDestination(d);
  },
  onRoom: (id) => openRoom(id).catch((e) => error(e.message)),
  onError: notice,
  onDrop(lat, lon) {
    destForm.elements.latitude.value = lat.toFixed(6);
    destForm.elements.longitude.value = lon.toFixed(6);
    destForm.elements.verified.checked = false;
    notice("Pin placed. Name and save the location to apply it.");
    destForm.elements.label.focus();
  },
  onMove: async (id, lat, lon) => {
    const before = prefs.destinations.find((d) => d.id === id);
    if (!before || busy) return false;
    if (lat === before.latitude && lon === before.longitude) return false;
    const after = {
      ...before,
      latitude: lat,
      longitude: lon,
      verified: false,
      location_basis: "user moved pin; exact entrance unconfirmed",
    };
    if (!(await confirmPinMove(before, after))) {
      notice("Pin move canceled. Saved location and routes are unchanged.");
      return false;
    }
    await updateDestinations(
      prefs.destinations.map((d) => (d.id === id ? after : d)),
      () => pinHistory.record(before, after),
    );
    notice(
      "Pin move confirmed. Undo last pin change restores its previous position. No scan or route request was started.",
    );
    return true;
  },
});
function showTab(which) {
  activeView = which;
  for (const name of ["map", "chart"]) {
    $(`${name}-tab`).setAttribute("aria-selected", String(which === name));
    $(`${name}-tab`).tabIndex = which === name ? 0 : -1;
    $(`${name}-panel`).hidden = which !== name;
  }
  if (which === "map") map.resize();
  if (snapshot) renderVisual(snapshot);
  persistView();
}
function setReady(ready) {
  for (const control of document.querySelectorAll(
    "main input, main select, main textarea, main button",
  ))
    control.disabled = !ready;
}
function fillPreferences() {
  for (const [key, value] of Object.entries(prefs)) {
    const input = form.elements.namedItem(key);
    if (input) input.value = value;
  }
  $("weight-label").textContent = `${Math.round(prefs.price_weight * 100)}%`;
  renderDestinations();
  updateDraft();
}
function updateDraft() {
  if (!prefs) return;
  const dirty = [...new FormData(form)].some(
    ([key, value]) =>
      String(prefs[key]) !== value && Number(prefs[key]) !== Number(value),
  );
  const end = form.elements.move_out;
  end.setCustomValidity(
    end.value &&
      form.elements.move_in.value &&
      end.value <= form.elements.move_in.value
      ? "Move out must be after move in."
      : "",
  );
  $("scenario-draft").textContent = dirty
    ? "Unapplied edits · results still use the saved scenario. Apply or revert your changes."
    : "Results use the saved scenario.";
  $("scenario-draft").classList.toggle("dirty", dirty);
  $("revert-scenario").hidden = !dirty;
  $("stay-settings-label").textContent = dirty
    ? "Edit stay, budget & preferences · unapplied changes"
    : "Edit stay, budget & preferences";
}
async function load({ fill = false } = {}) {
  controller?.abort();
  controller = new AbortController();
  const request = ++sequence;
  error("");
  $("results").setAttribute("aria-busy", "true");
  try {
    const query = params();
    const data = await api(
      `/api/decision?${query}`,
      undefined,
      controller.signal,
    );
    if (request !== sequence) return;
    if (destination && destination !== data.destination)
      notice(
        "That commute location is no longer enabled. Showing all enabled locations.",
      );
    destination = data.destination;
    query.set("destination", destination);
    query.delete("offset");
    data.view_query = query.toString();
    snapshot = data;
    offset = data.offset;
    prefs = data.preferences;
    const initial = !prefsReady;
    if (!prefsReady) {
      setReady(true);
      prefsReady = true;
    }
    if (fill || initial) fillPreferences();
    else {
      renderDestinations();
      updateDraft();
    }
    render(data);
    persistView();
    if (selected.size) await renderComparison();
  } finally {
    if (request === sequence) $("results").removeAttribute("aria-busy");
  }
}
async function savePreferences(next, onSaved = () => {}, { fill = true } = {}) {
  if (busy)
    throw new Error(
      "A scenario save is in progress. Please retry when it finishes.",
    );
  busy = true;
  $("apply-scenario").disabled = true;
  $("apply-scenario").textContent = "Saving…";
  renderPinUndo();
  try {
    prefs = await api("/api/preferences", next);
    onSaved();
    offset = 0;
    try {
      await load({ fill });
    } catch (e) {
      if (fill) fillPreferences();
      else {
        renderDestinations();
        updateDraft();
      }
      error(
        `Settings were saved, but the view could not refresh: ${e.message}. Reload the snapshot.`,
      );
    }
  } finally {
    busy = false;
    $("apply-scenario").disabled = false;
    $("apply-scenario").textContent = "Apply scenario";
    renderPinUndo();
  }
}
async function updateDestinations(destinations, onSaved) {
  await savePreferences({ ...prefs, destinations }, onSaved, { fill: false });
}
function render(data) {
  renderPinUndo();
  $("destination").innerHTML =
    '<option value="">All enabled locations</option>' +
    data.destination_choices
      .map((d) => `<option value="${esc(d.id)}">${esc(d.label)}</option>`)
      .join("");
  $("destination").value = destination;
  const selectedDestination = data.destination_choices.find(
    (d) => d.id === destination,
  );
  $("location-filter-note").textContent = selectedDestination
    ? `Distance, transit and preference ranking use ${selectedDestination.label}. Your saved locations are unchanged.`
    : "Distance, transit and preference ranking use all enabled commute locations and their relative weights.";
  $("freshness").textContent =
    `Snapshot ${new Date(data.generated_at).toLocaleString()} · ${data.status === "healthy" ? "sources current" : "partial source coverage"}`;
  $("scenario-label").innerHTML =
    `${esc(prefs.move_in)} → ${esc(prefs.move_out)}<br><strong>${yen(prefs.effective_budget_yen)} effective / month</strong>`;
  $("summary").innerHTML = [
    ["stored", "Stored"],
    ["current", "Current in snapshot"],
    ["eligible", "Meets requirements"],
    ["needs_confirmation", "Needs confirmation"],
    ["cost_complete", "Complete expenses¹"],
    ["route_complete", "Complete routes¹"],
  ]
    .map(
      ([key, label]) =>
        `<div><strong>${data.summary[key].toLocaleString()}</strong><span>${label}</span></div>`,
    )
    .join("");
  $("summary").title =
    "¹ Among potential matches for the saved stay and selected commute location, before search filters.";
  $("result-count").textContent = data.total.toLocaleString();
  $("browse-rooms").textContent =
    `Browse ${data.total.toLocaleString()} rooms ↓`;
  $("inventory-summary-label").textContent =
    `Inventory evidence · ${data.summary.stored.toLocaleString()} stored · ${data.summary.potential.toLocaleString()} potential matches`;
  $("result-note").textContent =
    `${data.statistics.potential} potential matches · ${data.statistics.source_count} sources`;
  $("ranking-note").textContent = data.ranking_note;
  renderStatistics(data.statistics);
  for (const r of data.records) selectedTitles.set(r.stable_key, r.title);
  renderSelection();
  const frontierIds = new Set(data.frontier_ids);
  $("room-rows").innerHTML = data.records
    .map((r) => {
      const s = r.scenario,
        route = r.route_metrics;
      return `<tr data-id="${esc(r.stable_key)}"><td><button type="button" class="room-link" data-open="${esc(r.stable_key)}">${esc(r.title)}</button><small>${esc(r.source_label)} · ${esc(r.city || r.prefecture || "Area unverified")}</small><span class="badge ${esc(r.scenario_status)}">${esc(r.scenario_status.replaceAll("_", " "))}</span>${frontierIds.has(r.stable_key) ? `<span class="badge">Pareto · ${data.frontier_metric === "distance" ? "geographic" : "transit"}</span>` : ""}${r.possible_duplicate_count ? `<small>Possible duplicate: ${r.possible_duplicate_count} other listing(s)</small>` : ""}${r.decision_status && r.decision_status !== "new" ? `<small>${esc(r.decision_status.replaceAll("_", " "))}</small>` : ""}</td><td data-label="Stay expense"><strong>${expense(s)}</strong><small>${yen(s.effective_monthly_yen)} / month${s.complete ? "" : " · known floor"}</small>${s.unknown_costs.length ? `<small class="missing">${s.unknown_costs.length} unresolved cost fields</small>` : ""}</td><td data-label="Cash & space">${amount(s.cash_before_keys_yen, s.cash_complete)}<small>${r.room_area_m2 == null ? "Room size unverified" : `${r.room_area_m2} m²`}</small></td><td data-label="Commute">${route.mean_minutes == null ? "Unverified" : `${route.mean_minutes} min${route.complete ? "" : " · partial"}`}<small>${route.observed}/${route.required} offices observed</small></td><td data-label="Compare"><input type="checkbox" data-compare="${esc(r.stable_key)}" aria-label="Compare ${esc(r.title)}" ${selected.has(r.stable_key) ? "checked" : ""}></td></tr>`;
    })
    .join("");
  $("empty").hidden = !!data.total;
  $("previous").disabled = offset === 0;
  $("next").disabled = offset + data.limit >= data.total;
  $("page-label").textContent = data.total
    ? `${offset + 1}–${Math.min(offset + data.limit, data.total)} of ${data.total}`
    : "0 rooms";
  for (const format of ["csv", "json"]) {
    const p = params();
    p.delete("offset");
    p.delete("limit");
    p.set("format", format);
    $(`export-${format}`).href = `/api/export?${p}`;
  }
  renderVisual(data);
  if (sourceKey !== data.run_id) {
    renderSources(data);
    sourceKey = data.run_id;
  }
}
function renderVisual(data) {
  const key = `${data.run_id}:${data.scenario_key}:${data.evidence_revision}:${data.evaluated_at_minute}:${data.view_query}`;
  if (visualKeys[activeView] === key) return;
  visualKeys[activeView] = key;
  if (activeView === "map") {
    map.update(data);
    $("map-note").textContent =
      `${data.map_points.length} of ${data.map_total} matching property locations shown, covering ${data.map_shown_room_count} of ${data.map_room_count} rooms with coordinates. ${data.total - data.map_room_count} matching rooms lack coordinates. Pins can contain several rooms.`;
    return;
  }
  tradeoffChart($("tradeoff-chart"), data, (id) =>
    openRoom(id).catch((e) => error(e.message)),
  );
  const coverage = data.frontier_coverage;
  $("frontier-summary").textContent =
    `${coverage.plottable} of ${coverage.potential} potential matches have chart positions: ${coverage.complete_cases} with complete comparison evidence + ${coverage.provisional} needing confirmation. ${data.frontier_ids.length} on the green frontier. ${coverage.unplotted} lack data for a point.${coverage.outside_chart_scope ? ` ${coverage.outside_chart_scope} excluded, stale, reserved or dismissed rows stay outside the chart.` : ""}`;
  $("chart-room-summary").textContent =
    `Browse plotted rooms (${data.points.length}${data.points.length < data.chart_total ? ` of ${data.chart_total}` : ""}) · includes overlapping points`;
  $("chart-room-browser").hidden = !data.points.length;
  $("chart-room-list").innerHTML = data.points
    .map(
      (point) =>
        `<div><button type="button" data-open="${esc(point.id)}">${esc(point.title)}</button><small>${point.price_is_floor ? "≥ " : ""}${yen(point.price)} / month · ${Number(point.value.toFixed(2))} ${data.frontier_metric === "distance" ? "km" : "min"} · ${point.provisional ? "Needs confirmation" : "Complete comparison evidence"}</small></div>`,
    )
    .join("");
  $("unplotted-rooms").hidden = !coverage.unplotted;
  $("unplotted-summary").textContent =
    `Rooms without a point (${coverage.unplotted})${data.unplotted.length < coverage.unplotted ? ` · first ${data.unplotted.length} listed` : ""}`;
  $("unplotted-list").innerHTML = data.unplotted
    .map(
      (room) =>
        `<div><button type="button" data-open="${esc(room.id)}">${esc(room.title)}</button><small>${esc(room.reasons.join("; "))}</small></div>`,
    )
    .join("");
  const comparedLocations = data.destination
    ? `the selected location (${data.destination_choices.find((d) => d.id === data.destination).label})`
    : "all enabled commute locations";
  $("distance-method").textContent =
    `Comparing against ${comparedLocations}. ` +
    (data.frontier_metric === "distance"
      ? "Geographic estimate: Haversine great-circle distances from each room pin, averaged using destination weights. Area pins reduce accuracy. This is not walking distance, rail distance or travel time, and it does not change the transit-based preference ranking. No routing API or listing scan is used."
      : `Google Routes transit matrix (or reviewed route evidence) between saved room and office pins. Provider departure: ${data.route_schedule}. One-way minutes are averaged using destination weights. Every compared location needs a route no older than ${data.route_cache_days} days to plot a point; unconfirmed endpoints are marked provisional. OpenStreetMap supplies the basemap, not the routes.`);
}
function renderStatistics(stats) {
  $("statistics-title").textContent =
    `Statistics for this view · ${stats.potential} potential matches in ${stats.filtered} results`;
  const card = (label, values, formatter, denominator) =>
    `<article><small>${esc(label)}</small><strong>${values.n ? formatter(values.median) : "Unknown"}</strong><small>${values.n ? `Median · ${formatter(values.min)}–${formatter(values.max)}` : "No observations"}</small><small>${values.n} of ${denominator} listings${values.missing ? ` · ${values.missing} unknown` : ""}</small></article>`;
  $("statistics-cards").innerHTML = [
    card(
      "Complete expense / month",
      stats.complete_monthly,
      yen,
      stats.cost_coverage.known,
    ),
    card(
      "Known minimum / month · fees missing",
      stats.known_minimum_monthly,
      (v) => `≥ ${yen(v)}`,
      stats.potential - stats.cost_coverage.known,
    ),
    card(
      "Published room area · m²",
      stats.area_m2,
      (v) => Number(v.toFixed(2)).toLocaleString(),
      stats.potential,
    ),
    card("Complete cash before keys", stats.cash_yen, yen, stats.potential),
  ].join("");
  $("statistics-coverage").textContent =
    `Among potential matches: ${stats.cost_coverage.known}/${stats.potential} complete expenses · ${stats.route_coverage.known}/${stats.potential} complete office routes. Excluded, stale, reserved and dismissed rooms are outside these distributions.`;
  $("statistics-gaps").textContent = stats.common_cost_gaps.length
    ? "Most common cost gaps: " +
      stats.common_cost_gaps
        .map((g) => `${g.field.replaceAll("_", " ")} (${g.listings})`)
        .join(" · ")
    : "No unresolved cost fields in this view.";
  $("statistics-note").textContent = stats.note;
}
function renderDestinations() {
  renderPinUndo();
  const active = prefs.destinations.filter(
    (d) => d.enabled && d.kind === "office" && d.office_days_per_week !== 0,
  );
  $("destination-summary").textContent = active.length
    ? `${active.length} enabled commute ${active.length === 1 ? "location" : "locations"}. Distance and transit comparisons use your saved pins and weights.`
    : "Add or enable a commute location to compare distance and transit. You can browse rooms without one.";
  $("destinations").innerHTML = prefs.destinations
    .map(
      (d) =>
        `<div class="destination-card"><label class="check"><input type="checkbox" data-enable="${esc(d.id)}" ${d.enabled ? "checked" : ""}>${esc(d.label)}</label><small>${esc(d.kind)} · ${d.latitude == null ? "No pin" : d.verified ? "Confirmed entrance" : "Approximate pin"} · weight ${d.weight}${d.office_days_per_week != null ? ` · ${d.office_days_per_week} days/week` : ""}</small><div class="actions"><button type="button" data-edit="${esc(d.id)}">Edit</button><button type="button" data-remove="${esc(d.id)}">Remove</button></div></div>`,
    )
    .join("");
}
function editDestination(d) {
  map.cancel();
  $("destination-form-title").textContent = d ? "Edit location" : "New location";
  $("save-destination").textContent = d ? "Save changes" : "Add location";
  for (const key of [
    "id",
    "label",
    "kind",
    "latitude",
    "longitude",
    "weight",
    "office_days_per_week",
  ])
    destForm.elements[key].value =
      d?.[key] ?? (key === "kind" ? "office" : key === "weight" ? 1 : "");
  destForm.elements.verified.checked = !!d?.verified;
  destForm.elements.label.focus();
}
function showDestinationEditor(show) {
  $("destination-editor").hidden = !show;
  $("toggle-destinations").setAttribute("aria-expanded", String(show));
  if (!show) map.cancel();
}
function renderSources(data) {
  const catalog = [
    ...data.source_catalog,
    ...data.manual_fallbacks.map((s) => ({
      id: s.label,
      label: s.label,
      url: s.url,
      mode: "reference",
      purpose: s.reason,
    })),
  ];
  for (const health of data.source_health) {
    const id = health.source_id || health.id;
    if (!catalog.some((s) => s.id === id))
      catalog.unshift({
        id,
        label: health.source_label || id,
        mode: "automated",
        url: health.source_url || "",
        purpose: "Existing inventory collector",
      });
  }
  $("sources").innerHTML = catalog
    .map((s) => {
      const health = data.source_health.find(
        (h) => (h.source_id || h.id) === s.id,
      );
      const coverage = data.source_coverage.find((c) => c.source_id === s.id);
      return `<div class="source-card"><strong>${link(s.url, s.label)}</strong><span class="badge">${esc(s.mode.replaceAll("_", " "))}</span><p>${esc(s.purpose)}</p>${coverage ? `<p>${coverage.current_records} current rooms · ${coverage.stale_records} stale · ${coverage.cached_pages} detail pages cached</p>` : ""}<small>${health ? `Snapshot: ${esc(health.last_status || health.status || "not run")}${health.last_error || health.error ? ` · ${esc(health.last_error || health.error)}` : ""}` : s.mode === "automated" ? "Collector installed; no successful run in this snapshot" : s.mode === "routing" ? "Transit connector ready; provider credentials required" : "Review and import exact-room evidence"}</small></div>`;
    })
    .join("");
}
function metricRows(r) {
  return [
    [
      "Usable floor area",
      r.usable_area_m2 == null ? null : `${r.usable_area_m2} m²`,
    ],
    ["Desk in this room", r.desk],
    ["Air conditioning", r.air_conditioning],
    ["Natural light", r.natural_light],
    [
      "Measured latency",
      r.internet_latency_ms == null ? null : `${r.internet_latency_ms} ms`,
    ],
    [
      "Measured night noise",
      r.night_noise_db == null ? null : `${r.night_noise_db} dB`,
    ],
    [
      "Mapped flood depth",
      r.flood_depth_m == null
        ? null
        : `${r.flood_depth_m} m · confirm map date and floor`,
    ],
    ["Building year", r.building_year],
    ["Emergency contact requirements", r.emergency_contact_requirement || null],
    ["Residents per shower", r.metrics.residents_per_shower],
    ["Residents per toilet", r.metrics.residents_per_toilet],
    ["Residents per washer", r.metrics.residents_per_washer],
    [
      "Download / upload Mbps",
      r.internet_download_mbps == null
        ? null
        : `${r.internet_download_mbps} / ${r.internet_upload_mbps ?? "unknown"}`,
    ],
    ["Move-out notice", r.notice_days == null ? null : `${r.notice_days} days`],
    ["Notice deadline", r.scenario.notice_deadline || null],
    ["Penalty boundary", r.scenario.penalty_free_exit_date || null],
    ["Extension available", r.extension_available],
    [
      "Room transfer fee",
      r.transfer_fee_yen == null ? null : yen(r.transfer_fee_yen),
    ],
    [
      "Screening lead time",
      r.screening_days == null ? null : `${r.screening_days} days`,
    ],
    [
      "Verification age",
      r.metrics.verification_age_hours == null
        ? null
        : `${r.metrics.verification_age_hours} hours`,
    ],
    [
      "Offices within commute limit",
      `${r.route_metrics.jobs_within_limit} observed of ${r.route_metrics.required}`,
    ],
    ["Expected weekly commute hours", r.route_metrics.weekly_commute_hours],
    [
      "Worst observed commute",
      r.route_metrics.worst_minutes == null
        ? null
        : `${r.route_metrics.worst_minutes} min`,
    ],
    [
      "Unreimbursed monthly fares",
      r.route_metrics.monthly_transport_yen == null
        ? null
        : yen(r.route_metrics.monthly_transport_yen),
    ],
    [
      "Housing plus transport / month",
      r.metrics.housing_plus_transport_yen == null
        ? null
        : yen(r.metrics.housing_plus_transport_yen),
    ],
    [
      "Cash remaining after keys",
      r.metrics.cash_buffer_yen == null ? null : yen(r.metrics.cash_buffer_yen),
    ],
    [
      "Entry fees in months of recurring cost",
      r.metrics.entry_fee_equivalent_months,
    ],
  ]
    .map(
      ([label, value]) =>
        `<tr><th>${esc(label)}</th><td class="${value == null ? "missing" : ""}">${esc(textValue(value))}</td></tr>`,
    )
    .join("");
}
async function openRoom(id) {
  const request = ++detailSequence;
  $("room-detail").innerHTML = '<h2 id="room-title">Loading room…</h2>';
  $("room-dialog").setAttribute("aria-busy", "true");
  if (!$("room-dialog").open) $("room-dialog").showModal();
  let r;
  try {
    r = await api(`/api/listing?${new URLSearchParams({ id, destination })}`);
  } catch (e) {
    if (request === detailSequence)
      $("room-detail").innerHTML =
        '<h2 id="room-title">Room could not load</h2><p>Close this dialog and try opening the room again.</p>';
    throw e;
  } finally {
    if (request === detailSequence)
      $("room-dialog").removeAttribute("aria-busy");
  }
  if (request !== detailSequence) return;
  openId = id;
  const s = r.scenario;
  $("room-detail").innerHTML =
    `<h2 id="room-title">${esc(r.title)}</h2><p>${link(r.url, "Open exact source listing")} · ${esc(r.address || r.city || "Address unverified")}</p><small>Map position: ${esc((r.coordinate_basis || "unknown").replaceAll("_", " "))}</small><span class="badge ${esc(r.scenario_status)}">${esc(r.scenario_status.replaceAll("_", " "))}</span>
  <div class="detail-stats"><div class="detail-stat"><small>Expense for ${s.nights} nights</small><strong>${expense(s)}</strong><small>${s.complete ? "Complete modeled charges" : "Known floor · unresolved charges below"}</small></div><div class="detail-stat"><small>Cash before keys</small><strong>${amount(s.cash_before_keys_yen, s.cash_complete)}</strong><small>${s.cash_complete ? "Known payment schedule" : "Payment schedule incomplete"}</small></div><div class="detail-stat"><small>Refundable security cash</small><strong>${yen(s.refundable_cash_yen)}</strong><small>Kept separate from permanent expense</small></div></div>
  <p>${esc(r.available_text || "Availability requires confirmation")} · registration: ${esc(r.registration_confidence)}</p><ul>${r.scenario_reasons.map((v) => `<li>${esc(v)}</li>`).join("")}</ul>
  <form id="room-decision" class="actions"><label>Decision<select name="status">${["new", "shortlisted", "contacted", "reserved", "not_interested"].map((v) => `<option value="${v}" ${r.decision_status === v ? "selected" : ""}>${v.replaceAll("_", " ")}</option>`).join("")}</select></label><label>Note<input name="note" maxlength="1000" value="${esc(r.decision_note || "")}"></label><button type="submit">Save decision</button></form>
  <details open><summary>Cost ledger & departure scenarios</summary><table><tbody>${s.ledger.map((row) => `<tr><th>${esc(row.label)}</th><td>${yen(row.amount_yen)}</td><td>${esc(row.kind)}</td></tr>`).join("")}</tbody></table>${s.unknown_costs.length ? `<p class="missing">Unresolved: ${esc(s.unknown_costs.join("; "))}</p>` : ""}<p>${r.exit_scenarios
    .map(
      (v) =>
        `${v.months} month(s): ${v.complete ? "" : "≥ "}${yen(v.known_total_yen)}${v.contract_eligible === false ? " (contract not eligible)" : v.contract_eligible == null ? " (minimum stay unknown)" : ""}`,
    )
    .map(esc)
    .join(
      " · ",
    )}</p>${r.campaign && Object.keys(r.campaign).length ? `<p>Campaign evidence: ${esc(r.campaign.eligibility_note || "Check exact room eligibility")} ${link(r.campaign.source_url, "Terms")}</p>` : ""}</details>
  <details open><summary>Metrics that change the decision</summary><table><tbody>${metricRows(r)}</tbody></table><p class="muted">Noise, mould/ventilation, hazard exposure, address registration and WFH reliability need room-specific evidence. A published facility label does not prove speed or quietness.</p></details>
  <details><summary>Office routes & verification</summary><table><tbody>${r.route_metrics.destinations.map((d) => `<tr><th>${esc(d.label)}</th><td>${d.minutes == null ? "Unverified" : `${d.minutes} min`}</td><td>${d.fare_yen == null ? "Fare unknown" : yen(d.fare_yen)}</td><td>${d.source_url ? link(d.source_url, "Route evidence") : ""}</td></tr>`).join("")}</tbody></table><button id="fetch-routes" type="button">Fetch routes to all enabled locations</button><small>Uses configured provider credentials and all enabled commute pins, regardless of the location filter.</small><form id="route-form" class="evidence-form"><label>Destination<select name="destination_id">${prefs.destinations
    .filter((d) => d.enabled && d.kind === "office" && d.latitude != null)
    .map((d) => `<option value="${esc(d.id)}">${esc(d.label)}</option>`)
    .join(
      "",
    )}</select></label><label>Observed one-way minutes<input name="minutes" type="number" min="1" max="1440" required></label><label>One-way fare · ¥ (optional)<input name="fare_yen" type="number" min="0" step="1"></label><label>Verified on<input name="verified_at" type="date" value="${new Date().toLocaleDateString("en-CA")}" required></label><label>Route evidence URL<input name="source_url" type="url" placeholder="https://…" required></label><button type="submit">Save reviewed route</button></form></details>
  <details><summary>Correct room facts / add evidence</summary><p>Use published or confirmed facts for costs and eligibility. Estimates are stored separately. Room coordinates can also be corrected here.</p><form id="evidence-form" class="evidence-form"><label>Fields · JSON<textarea name="values" rows="7" spellcheck="false">${esc(JSON.stringify({ room_area_m2: r.room_area_m2, desk: r.desk, latitude: r.latitude, longitude: r.longitude }, null, 2))}</textarea></label><label class="check"><input type="checkbox" name="location_confirmed">Exact room map location confirmed</label><label>Evidence type<select name="kind"><option value="confirmed">Confirmed with operator / inspected</option><option value="published">Published exact-room evidence</option><option value="estimate">Estimate · excluded from confirmed metrics</option></select></label><label>Source URL<input name="source_url" type="url" value="${esc(r.url)}" required></label><label>Evidence note<input name="note" maxlength="4000"></label><button type="submit">Save room evidence</button></form></details>
  <details><summary>Provenance & unresolved estimates</summary><pre>${esc(JSON.stringify({ field_evidence: r.field_evidence || {}, metrics_evidence: r.metrics_evidence, estimated_fields: r.estimated_fields || {}, source_evidence: r.source_evidence }, null, 2))}</pre></details>`;
  if (!$("room-dialog").open) $("room-dialog").showModal();
  const factSection = document.createElement("details");
  factSection.innerHTML = `<summary>Update quote & room facts</summary><p>Leave blank to keep the current fact. Enter 0 only when the charge is explicitly zero. Monthly and entry totals must include mandatory charges for this exact room.</p><form id="room-facts" class="evidence-form"><div class="fact-fields">${factFields.map(([key, label, step, max]) => `<label>${esc(label)}<input name="${key}" type="number" min="0" max="${max}" step="${step}" placeholder="${r[key] == null ? "Unverified" : esc(r[key])}"><small>Current: ${esc(textValue(r[key]))}</small></label>`).join("")}<label>Deposit included in entry charges?<select name="deposit_in_initial_cost"><option value="">Keep current (${textValue(r.deposit_in_initial_cost)})</option><option value="true">Yes</option><option value="false">No · paid separately</option></select></label><label>Desk provided in this room?<select name="desk"><option value="">Keep current (${textValue(r.desk)})</option><option value="true">Yes</option><option value="false">No</option></select></label><label>Billing rule<select name="billing_basis"><option value="">Keep current (${esc(r.billing_basis)})</option><option value="anniversary">Move-in anniversaries</option><option value="calendar_daily">Calendar month / daily proration</option></select></label></div>${r.unknown_costs.length ? `<fieldset><legend>Cost questions resolved by this evidence</legend><p class="muted">Check only when the entered totals cover this charge or the operator confirms it does not apply. Missing amounts and contract terms are still checked separately.</p>${r.unknown_costs.map((gap) => `<label class="check"><input name="resolved_cost" type="checkbox" value="${esc(gap)}">${esc(gap)}</label>`).join("")}</fieldset>` : ""}<label>Evidence type<select name="kind"><option value="published">Published exact-room quote</option><option value="confirmed">Confirmed with operator / inspected</option></select></label><label>Evidence source URL<input name="source_url" type="url" value="${esc(r.url)}" required></label><label>What did you verify?<input name="note" maxlength="4000" required></label><button type="submit">Save verified facts</button></form>`;
  $("room-detail").insertBefore(
    factSection,
    $("room-detail").querySelector("details"),
  );
  $("room-facts").addEventListener(
    "submit",
    run(async (e) => {
      e.preventDefault();
      const f = new FormData(e.target),
        button = e.submitter;
      const values = roomFactUpdate(f, r);
      button.disabled = true;
      try {
        await api("/api/evidence", {
          stable_key: id,
          values,
          source_url: f.get("source_url"),
          kind: f.get("kind"),
          note: f.get("note"),
        });
        await load();
        await openRoom(id);
        notice(
          "Room facts saved with their source. Costs and eligibility recalculated.",
        );
      } finally {
        button.disabled = false;
      }
    }),
  );
  $("room-decision").addEventListener(
    "submit",
    run(async (e) => {
      e.preventDefault();
      const f = new FormData(e.target);
      await api("/api/decisions", {
        stable_key: id,
        status: f.get("status"),
        note: f.get("note"),
      });
      notice("Decision saved.");
      await load();
    }),
  );
  $("evidence-form").addEventListener(
    "submit",
    run(async (e) => {
      e.preventDefault();
      const f = new FormData(e.target);
      await api("/api/evidence", {
        stable_key: id,
        values: {
          ...JSON.parse(f.get("values")),
          ...(f.get("location_confirmed") === "on"
            ? { coordinate_basis: "user_confirmed" }
            : {}),
        },
        source_url: f.get("source_url"),
        kind: f.get("kind"),
        note: f.get("note"),
      });
      await load();
      await openRoom(id);
      notice("Room evidence saved with its source.");
    }),
  );
  $("route-form").addEventListener(
    "submit",
    run(async (e) => {
      e.preventDefault();
      if (r.latitude == null || r.longitude == null)
        throw new Error(
          "Add the room’s coordinates before recording its route.",
        );
      const f = new FormData(e.target),
        d = prefs.destinations.find((d) => d.id === f.get("destination_id"));
      if (!d) throw new Error("Choose an office with a pin.");
      const route = {
        destination_id: d.id,
        destination_latitude: d.latitude,
        destination_longitude: d.longitude,
        origin_latitude: r.latitude,
        origin_longitude: r.longitude,
        minutes: Number(f.get("minutes")),
        fare_yen: f.get("fare_yen") === "" ? null : Number(f.get("fare_yen")),
        verified_at: f.get("verified_at"),
        source_url: f.get("source_url"),
      };
      await api("/api/evidence", {
        stable_key: id,
        values: {
          metrics_evidence: { ...r.metrics_evidence, routes: [route] },
        },
        source_url: route.source_url,
        kind: "confirmed",
        note: "Reviewed route for exact endpoints",
      });
      await load();
      await openRoom(id);
    }),
  );
  $("fetch-routes").addEventListener(
    "click",
    run(async (e) => {
      e.target.disabled = true;
      try {
        const result = await api("/api/routes", { stable_key: id });
        notice(result.message);
        await load();
        await openRoom(id);
      } finally {
        e.target.disabled = false;
      }
    }),
  );
}
function renderSelection() {
  writeSelection(selectionStorage, selected);
  $("selection-tray").hidden = !selected.size;
  $("selection-count").textContent = `${selected.size} of 3 rooms selected`;
  $("selection-items").innerHTML = [...selected]
    .map(
      (id, index) =>
        `<button type="button" data-remove-selection="${esc(id)}" aria-label="Remove ${esc(selectedTitles.get(id) || `room ${index + 1}`)} from comparison">${esc(selectedTitles.get(id) || `Room ${index + 1}`)} ×</button>`,
    )
    .join("");
  for (const box of document.querySelectorAll("[data-compare]")) {
    box.checked = selected.has(box.dataset.compare);
    box.closest("tr").classList.toggle("selected-row", box.checked);
  }
}
async function renderComparison() {
  renderSelection();
  const key = `${snapshot.run_id}:${snapshot.scenario_key}:${snapshot.evidence_revision}:${snapshot.evaluated_at_minute}:${destination}:${[...selected].join(",")}`;
  if (comparisonKey === key) return;
  comparisonKey = null;
  comparisonController?.abort();
  comparisonController = new AbortController();
  const request = ++comparisonSequence;
  if (!selected.size) {
    $("comparison").hidden = true;
    comparisonKey = null;
    return;
  }
  $("comparison").hidden = false;
  $("comparison").setAttribute("aria-busy", "true");
  $("comparison-cards").textContent = "Loading selected rooms…";
  $("cost-chart").replaceChildren();
  $("crossover-note").textContent = "";
  const p = new URLSearchParams();
  p.set("destination", destination);
  for (const id of selected) p.append("id", id);
  let data;
  try {
    data = await api(
      `/api/compare?${p}`,
      undefined,
      comparisonController.signal,
    );
  } catch (e) {
    if (request === comparisonSequence)
      $("comparison-cards").textContent =
        "Comparison could not load. Remove unavailable rooms, or use View comparison to retry.";
    throw e;
  } finally {
    if (request === comparisonSequence)
      $("comparison").removeAttribute("aria-busy");
  }
  if (request !== comparisonSequence) return;
  comparisonKey = key;
  for (const r of data.records) selectedTitles.set(r.stable_key, r.title);
  renderSelection();
  $("comparison").hidden = false;
  $("comparison-cards").innerHTML = data.records
    .map(
      (r) =>
        `<article class="compare-card"><h3>${esc(r.title)}</h3><strong>${expense(r.scenario)}</strong><p>${yen(r.scenario.effective_monthly_yen)} / month · ${r.room_area_m2 ?? "Unknown"} m²</p><small>Cash: ${amount(r.scenario.cash_before_keys_yen, r.scenario.cash_complete)} · ${esc(r.scenario_status.replaceAll("_", " "))}</small><button type="button" data-open="${esc(r.stable_key)}">Details</button></article>`,
    )
    .join("");
  costChart($("cost-chart"), data.cost_series);
  $("crossover-note").textContent =
    data.note +
    " " +
    data.crossovers
      .map((c) =>
        c.first_monthly_crossover
          ? `Ordering changes by ${c.first_monthly_crossover}.`
          : c.comparable_months
            ? "No ordering reversal among eligible complete monthly samples."
            : "No fully comparable contract periods yet.",
      )
      .join(" ");
}
form.addEventListener("input", updateDraft);
$("revert-scenario").addEventListener("click", () => fillPreferences());
form.addEventListener(
  "submit",
  run(async (e) => {
    e.preventDefault();
    const next = { ...prefs };
    for (const [key, value] of new FormData(form))
      next[key] =
        key.startsWith("move_") || key === "housing_region"
          ? value
          : Number(value);
    await savePreferences(next);
    notice(
      "Scenario saved. Table, map, comparisons and exports now use these dates.",
    );
  }),
);
form.elements.price_weight.addEventListener("input", (e) => {
  $("weight-label").textContent = `${Math.round(e.target.value * 100)}%`;
});
for (const button of document.querySelectorAll("[data-months]"))
  button.addEventListener("click", () => {
    form.elements.move_out.value = addCalendarMonths(
      form.elements.move_in.value,
      Number(button.dataset.months),
    );
    updateDraft();
    notice("Stay length updated in the form. Apply scenario to recalculate.");
  });
$("toggle-destinations").addEventListener("click", () => {
  showDestinationEditor($("destination-editor").hidden);
});
$("add-destination").addEventListener("click", () => {
  showDestinationEditor(true);
  editDestination();
});
$("destinations").addEventListener(
  "click",
  run(async (e) => {
    const edit = e.target.closest("[data-edit]"),
      remove = e.target.closest("[data-remove]");
    if (edit)
      editDestination(
        prefs.destinations.find((d) => d.id === edit.dataset.edit),
      );
    if (remove) {
      const before = prefs.destinations;
      const removed = before.find((d) => d.id === remove.dataset.remove);
      const position = before.indexOf(removed);
      await updateDestinations(
        before.filter((d) => d.id !== remove.dataset.remove),
      );
      if (destForm.elements.id.value === removed.id) editDestination();
      notice("Location removed.");
      const undo = document.createElement("button");
      undo.textContent = "Undo";
      undo.addEventListener(
        "click",
        run(async () => {
          if (prefs.destinations.some((d) => d.id === removed.id)) return;
          const restored = [...prefs.destinations];
          restored.splice(Math.min(position, restored.length), 0, removed);
          await updateDestinations(restored);
          notice("Location restored.");
        }),
      );
      $("notice").append(" ", undo);
    }
  }),
);
$("destinations").addEventListener(
  "change",
  run(async (e) => {
    if (e.target.dataset.enable)
      await updateDestinations(
        prefs.destinations.map((d) =>
          d.id === e.target.dataset.enable
            ? { ...d, enabled: e.target.checked }
            : d,
        ),
      );
  }),
);
destForm.addEventListener(
  "submit",
  run(async (e) => {
    e.preventDefault();
    const f = new FormData(destForm);
    const old = prefs.destinations.find((d) => d.id === f.get("id"));
    const d = {
      ...old,
      id: old?.id || crypto.randomUUID(),
      label: f.get("label"),
      kind: f.get("kind"),
      latitude: f.get("latitude") === "" ? null : Number(f.get("latitude")),
      longitude: f.get("longitude") === "" ? null : Number(f.get("longitude")),
      weight: Number(f.get("weight")),
      office_days_per_week:
        f.get("office_days_per_week") === ""
          ? null
          : Number(f.get("office_days_per_week")),
      verified: f.get("verified") === "on",
      enabled: old?.enabled ?? true,
      location_basis: "user pin",
      source_url: old?.source_url || "",
    };
    const changedPin =
      old && (old.latitude !== d.latitude || old.longitude !== d.longitude);
    if ((d.latitude == null) !== (d.longitude == null))
      throw new Error("Enter both latitude and longitude, or clear both.");
    if (changedPin && !(await confirmPinMove(old, d))) {
      notice(
        "Pin move canceled. The form remains a draft; saved routes are unchanged.",
      );
      return;
    }
    await updateDestinations(
      old
        ? prefs.destinations.map((v) => (v.id === d.id ? d : v))
        : [...prefs.destinations, d],
      changedPin ? () => pinHistory.record(old, d) : undefined,
    );
    map.cancel();
    editDestination();
    notice("Location saved.");
  }),
);
$("undo-pin").addEventListener(
  "click",
  run(async () => {
    const change = pinHistory.latest(prefs.destinations);
    if (!change) return;
    await updateDestinations(
      pinHistory.restored(prefs.destinations, change),
      () => pinHistory.complete(change),
    );
    map.cancel();
    editDestination();
    notice(
      `Restored the previous pin for ${change.current.label}. Fresh matching routes are reused; no scan or route request was started.`,
    );
  }),
);
for (const field of ["latitude", "longitude"])
  destForm.elements[field].addEventListener("input", () => {
    destForm.elements.verified.checked = false;
  });
$("drop-pin").addEventListener("click", () => {
  showTab("map");
  map.drop();
  $("housing-map").scrollIntoView({ block: "center" });
  notice(
    "Click the map to place the destination. You can also enter coordinates. Escape cancels pin placement.",
  );
});
$("cancel-destination").addEventListener("click", () => {
  map.cancel();
  editDestination();
  notice("Location edit cleared.");
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") map.cancel();
});
for (const name of ["map", "chart"]) {
  $(`${name}-tab`).addEventListener("click", () => showTab(name));
  $(`${name}-tab`).addEventListener("keydown", (e) => {
    if (["ArrowLeft", "ArrowRight"].includes(e.key)) {
      e.preventDefault();
      const next = name === "map" ? "chart" : "map";
      showTab(next);
      $(`${next}-tab`).focus();
    }
  });
}
let timer;
for (const key of [
  "query", "status", "sort", "decision", "destination", "chart-metric",
])
  $(key).addEventListener(key === "query" ? "input" : "change", () => {
    if (key === "destination") destination = $(key).value;
    clearTimeout(timer);
    timer = setTimeout(
      () => {
        offset = 0;
        load().catch((e) => {
          if (e.name !== "AbortError") error(e.message);
        });
      },
      key === "query" ? 250 : 0,
    );
  });
$("reload").addEventListener(
  "click",
  run(() => load()),
);
async function resetFilters() {
  clearTimeout(timer);
  destination = defaultView.destination;
  for (const key of ["query", "status", "sort", "decision"])
    $(key).value = defaultView[key];
  offset = 0;
  await load();
}
$("reset-filters").addEventListener("click", run(resetFilters));
$("empty-reset").addEventListener("click", run(resetFilters));
$("previous").addEventListener(
  "click",
  run(() => {
    offset = Math.max(0, offset - 50);
    return load();
  }),
);
$("next").addEventListener(
  "click",
  run(() => {
    offset += 50;
    return load();
  }),
);
for (const container of [
  $("room-rows"),
  $("comparison-cards"),
  $("chart-room-list"),
  $("unplotted-list"),
])
  container.addEventListener(
    "click",
    run(async (e) => {
      const button = e.target.closest("[data-open]");
      if (button) {
        map.focus(button.dataset.open);
        await openRoom(button.dataset.open);
      }
    }),
  );
$("room-rows").addEventListener(
  "change",
  run(async (e) => {
    const id = e.target.dataset.compare;
    if (!id) return;
    if (e.target.checked) {
      if (selected.size >= 3) {
        e.target.checked = false;
        throw new Error("Compare up to three rooms at a time.");
      }
      selected.add(id);
    } else selected.delete(id);
    await renderComparison();
  }),
);
function clearSelection() {
  selected.clear();
  return renderComparison();
}
$("clear-comparison").addEventListener("click", run(clearSelection));
$("clear-selection").addEventListener("click", run(clearSelection));
$("selection-items").addEventListener(
  "click",
  run(async (e) => {
    const button = e.target.closest("[data-remove-selection]");
    if (button) {
      selected.delete(button.dataset.removeSelection);
      await renderComparison();
    }
  }),
);
$("view-comparison").addEventListener(
  "click",
  run(async () => {
    await renderComparison();
    $("comparison").scrollIntoView({ block: "start", behavior: "auto" });
  }),
);
$("close-dialog").addEventListener("click", () => $("room-dialog").close());
$("room-dialog").addEventListener("close", () => {
  openId = null;
  detailSequence++;
});
$("toggle-import").addEventListener("click", () => {
  const show = $("import-form").hidden;
  $("import-form").hidden = !show;
  $("toggle-import").setAttribute("aria-expanded", String(show));
  if (show && !$("import-json").value)
    $("import-json").value = JSON.stringify(
      {
        record: {
          source_id: "reviewed_operator",
          source_label: "Operator name",
          external_id: "property-room-id",
          title: "Exact room title",
          url: "https://example.com/exact-room",
          recurring_total_yen: null,
          initial_cost_yen: null,
          exit_cost_yen: null,
        },
        kind: "published",
        note: "Reviewed exact-room listing",
      },
      null,
      2,
    );
});
$("import-form").addEventListener(
  "submit",
  run(async (e) => {
    e.preventDefault();
    const result = await api("/api/import", JSON.parse($("import-json").value));
    await load();
    await openRoom(result.stable_key);
    notice("Reviewed listing imported. Missing facts remain unverified.");
  }),
);
setReady(false);
for (const key of ["query", "status", "sort", "decision"])
  $(key).value = initialView[key];
$("chart-metric").value = initialView.metric;
showTab(initialView.view);
load({ fill: true }).catch((e) => error(e.message));
