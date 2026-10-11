export const defaultView = {
  query: "",
  status: "feasible",
  sort: "recommended",
  decision: "all",
  destination: "",
  metric: "distance",
  view: "map",
  offset: 0,
};
const choices = {
  status: [
    "feasible",
    "eligible",
    "needs_confirmation",
    "excluded",
    "stale",
    "all",
  ],
  sort: [
    "recommended",
    "price",
    "cash",
    "space",
    "commute",
    "distance",
    "verification",
  ],
  decision: [
    "all",
    "new",
    "shortlisted",
    "contacted",
    "reserved",
    "not_interested",
  ],
  metric: ["transit", "distance"],
  view: ["map", "chart"],
};
export function readViewState(search) {
  const params = new URLSearchParams(search),
    state = { ...defaultView };
  state.query = (params.get("query") || "").slice(0, 200);
  const destination = params.get("destination") || "";
  if (destination.length <= 80) state.destination = destination;
  for (const [key, values] of Object.entries(choices))
    if (values.includes(params.get(key))) state[key] = params.get(key);
  const offset = Number(params.get("offset"));
  if (Number.isInteger(offset) && offset >= 0 && offset <= 100000)
    state.offset = offset;
  return state;
}
export function viewSearch(state) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(defaultView))
    if (state[key] != null && state[key] !== value) params.set(key, state[key]);
  return params.toString();
}
export function readSelection(storage) {
  try {
    const ids = JSON.parse(storage.getItem("japan-housing.comparison.v1"));
    return Array.isArray(ids)
      ? [
          ...new Set(
            ids.filter(
              (id) =>
                typeof id === "string" && id.length > 0 && id.length <= 400,
            ),
          ),
        ].slice(0, 3)
      : [];
  } catch {
    return [];
  }
}
export function writeSelection(storage, ids) {
  try {
    storage.setItem(
      "japan-housing.comparison.v1",
      JSON.stringify([...ids].slice(0, 3)),
    );
  } catch {
    /* The selection remains usable in memory when storage is unavailable. */
  }
}
