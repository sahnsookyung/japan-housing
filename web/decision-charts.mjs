const NS = "http://www.w3.org/2000/svg";
const colors = ["#275d49", "#8b641e", "#425ba3"];
function node(tag, attrs = {}, text) {
  const n = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attrs)) n.setAttribute(key, value);
  if (text !== undefined) n.textContent = text;
  return n;
}
function frame(container, label, xMax, yMax, xLabel, yLabel) {
  container.replaceChildren();
  const svg = node("svg", {
    viewBox: "0 0 640 310",
    role: "img",
    "aria-label": label,
    class: "plot",
  });
  container.append(svg);
  const x = (v) => 65 + (v / xMax) * 535,
    y = (v) => 260 - (v / yMax) * 225;
  for (let i = 0; i <= 4; i++) {
    const yy = y((yMax * i) / 4);
    svg.append(
      node("line", { x1: 65, y1: yy, x2: 600, y2: yy, stroke: "#e3e8e0" }),
      node(
        "text",
        {
          x: 55,
          y: yy + 4,
          "text-anchor": "end",
          "font-size": 10,
          fill: "#66766d",
        },
        Math.round((yMax * i) / 4).toLocaleString(),
      ),
    );
    const xx = x((xMax * i) / 4);
    svg.append(
      node(
        "text",
        {
          x: xx,
          y: 279,
          "text-anchor": "middle",
          "font-size": 10,
          fill: "#66766d",
        },
        Math.round((xMax * i) / 4).toLocaleString(),
      ),
    );
  }
  svg.append(
    node(
      "text",
      { x: 330, y: 302, "text-anchor": "middle", "font-size": 11 },
      xLabel,
    ),
    node("text", { x: 65, y: 17, "font-size": 11 }, yLabel),
  );
  return { svg, x, y };
}
export function plottedFrontier(points) {
  const sorted = points
    .filter(
      (p) => Number.isFinite(p.price) && Number.isFinite(p.value ?? p.minutes),
    )
    .sort(
      (a, b) =>
        a.price - b.price || (a.value ?? a.minutes) - (b.value ?? b.minutes),
    );
  let best = Infinity;
  return sorted.filter((p) => {
    const value = p.value ?? p.minutes;
    if (value >= best) return false;
    best = value;
    return true;
  });
}
export function frontierPath(points, x, y) {
  if (!points.length) return "";
  // A discrete dominance boundary; joining diagonally would imply interpolated rooms.
  let path = `M ${x(points[0].price)} 35 V ${y(points[0].value ?? points[0].minutes)}`;
  for (const p of points.slice(1))
    path += ` H ${x(p.price)} V ${y(p.value ?? p.minutes)}`;
  return path + " H 600";
}
export function tradeoffChart(container, data, onRoom) {
  const distance = data.frontier_metric === "distance";
  const value = (p) => p.value ?? p.minutes;
  const valueLabel = (p) =>
    `${Number(value(p).toFixed(distance ? 2 : 1))} ${distance ? "km straight-line" : "min transit"}`;
  const points = data.points.filter(
    (p) => Number.isFinite(p.price) && Number.isFinite(value(p)),
  );
  if (!points.length) {
    container.replaceChildren();
    const p = document.createElement("p");
    p.className = "empty";
    const hasDestinations = data.preferences?.destinations.some(
      (d) => d.enabled && d.kind === "office" && d.office_days_per_week !== 0,
    );
    p.textContent = hasDestinations === false
      ? "Add or enable an office commute location above to compare cost against distance or transit. Room lists remain available without a destination."
      : distance
      ? "No potential matches currently have both a known cost minimum and coordinates for every compared location. Rooms without a point are listed below with the missing data."
      : "No potential matches currently have observed routes to every compared location. Rooms without a point are listed below. Choose Straight-line distance above to inspect geographic trade-offs without a routing API.";
    container.append(p);
    return;
  }
  const { svg, x, y } = frame(
    container,
    distance
      ? "Effective cost versus straight-line distance Pareto frontier"
      : "Effective cost versus observed commute Pareto frontier",
    Math.max(1, ...points.map((p) => p.price)) * 1.1,
    Math.max(1, ...points.map(value)) * 1.1,
    "Effective expense / known minimum · ¥ per month",
    distance
      ? "Weighted straight-line distance · km (estimate)"
      : "Weighted one-way transit · minutes",
  );
  const frontier = plottedFrontier(points.filter((p) => p.frontier));
  const provisional = points.some((p) => p.provisional)
    ? plottedFrontier(points)
    : [];
  for (const [choices, kind, color, dash] of [
    [provisional, "provisional", "#9b681c", "6 4"],
    [frontier, "complete", "#275d49", "none"],
  ])
    if (choices.length)
      svg.append(
        node("path", {
          d: frontierPath(choices, x, y),
          fill: "none",
          stroke: color,
          "stroke-width": 2.5,
          "stroke-dasharray": dash,
          "data-frontier": kind,
          "pointer-events": "none",
          "aria-label":
            kind === "complete"
              ? "Frontier among rooms meeting requirements with complete costs and location data"
              : "Provisional frontier using plotted values; missing fees can change this boundary",
        }),
      );
  const tooltip = document.createElement("div");
  tooltip.className = "chart-tooltip";
  tooltip.id = `${container.id}-tooltip`;
  tooltip.setAttribute("role", "tooltip");
  tooltip.hidden = true;
  container.append(tooltip);
  for (const p of points) {
    const costLabel = `${p.price_is_floor ? "at least " : ""}¥${p.price.toLocaleString()} per month`;
    const evidenceLabel = p.provisional
      ? `Needs confirmation${p.unknown_costs?.length ? `: ${p.unknown_costs.join("; ")}` : ""}${!distance && !p.endpoints_confirmed ? "; route endpoints unconfirmed" : ""}`
      : "Complete expenses and eligibility";
    const description = `${p.title} · ${costLabel} · ${valueLabel(p)} · ${evidenceLabel}${p.frontier ? " · Frontier among rooms with complete comparison evidence" : ""}`;
    const dot = node("circle", {
      cx: x(p.price),
      cy: y(value(p)),
      r: p.frontier ? 6 : 4.5,
      fill: p.provisional ? "#fff" : p.frontier ? "#275d49" : "#a7b8ab",
      stroke: p.provisional ? "#9b681c" : "#275d49",
      "stroke-width": p.provisional ? 1.7 : 1,
      "data-room-id": p.id,
      "data-evidence": p.provisional ? "provisional" : "complete",
      tabindex: 0,
      role: "button",
      "aria-label": description,
    });
    dot.append(node("title", {}, description));
    const showTooltip = () => {
      tooltip.replaceChildren();
      const title = document.createElement("strong"),
        detail = document.createElement("span");
      title.textContent = p.title;
      detail.textContent = `${costLabel} · ${valueLabel(p)}\n${evidenceLabel}`;
      tooltip.append(title, detail);
      tooltip.hidden = false;
      dot.setAttribute("aria-describedby", tooltip.id);
      const box = container.getBoundingClientRect(),
        point = dot.getBoundingClientRect();
      const left =
        point.left + point.width / 2 - box.left - tooltip.offsetWidth / 2;
      tooltip.style.left = `${Math.max(0, Math.min(left, box.width - tooltip.offsetWidth))}px`;
      const top = point.top - box.top - tooltip.offsetHeight - 10;
      tooltip.style.top = `${top >= 0 ? top : point.bottom - box.top + 10}px`;
    };
    const hideTooltip = () => {
      tooltip.hidden = true;
      dot.removeAttribute("aria-describedby");
    };
    dot.addEventListener("mouseenter", showTooltip);
    dot.addEventListener("mouseleave", () => {
      if (document.activeElement !== dot) hideTooltip();
    });
    dot.addEventListener("focus", showTooltip);
    dot.addEventListener("blur", hideTooltip);
    dot.addEventListener("click", () => onRoom(p.id));
    dot.addEventListener("keydown", (e) => {
      if (e.key === "Escape") hideTooltip();
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        onRoom(p.id);
      }
    });
    svg.append(dot);
  }
  if (data.chart_total > points.length) {
    const p = document.createElement("p");
    p.textContent = `Showing ${points.length} of ${data.chart_total} points. Narrow your filters to inspect the remainder.`;
    container.append(p);
  }
}
export function costChart(container, series) {
  const values = series
    .flatMap((s) => s.values.map((v) => v.total_yen))
    .filter(Number.isFinite);
  if (!values.length) {
    container.textContent =
      "Recurring charges are incomplete; there is no comparable cost curve yet.";
    return;
  }
  const { svg, x, y } = frame(
    container,
    "Equal-period expense at monthly anniversaries",
    12,
    Math.max(1, ...values) * 1.12,
    "Stay · months",
    "Total expense · ¥",
  );
  series.forEach((s, index) => {
    for (let i = 1; i < s.values.length; i++) {
      const a = s.values[i - 1],
        b = s.values[i];
      if (a.total_yen == null || b.total_yen == null) continue;
      const solid =
        a.complete &&
        b.complete &&
        a.contract_eligible === true &&
        b.contract_eligible === true;
      svg.append(
        node("line", {
          x1: x(a.months),
          y1: y(a.total_yen),
          x2: x(b.months),
          y2: y(b.total_yen),
          stroke: colors[index],
          "stroke-width": 2,
          "stroke-dasharray": solid ? "none" : "5 4",
        }),
      );
    }
    const label = document.createElement("p");
    label.style.color = colors[index];
    label.textContent = s.title;
    container.append(label);
  });
  const note = document.createElement("small");
  note.textContent =
    "Dashed segments have incomplete costs or unconfirmed/ineligible contract lengths. They are not firm quotes.";
  container.append(note);
}
export function addCalendarMonths(value, months) {
  const [year, month, day] = value.split("-").map(Number);
  const date = new Date(Date.UTC(year, month - 1 + months, 1));
  const last = new Date(
    Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 0),
  ).getUTCDate();
  date.setUTCDate(Math.min(day, last));
  return date.toISOString().slice(0, 10);
}
