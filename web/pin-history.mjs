const fields = ["latitude", "longitude", "verified", "location_basis"];
export const pinPosition = (destination) =>
  Object.fromEntries(fields.map((key) => [key, destination[key]]));
export const samePin = (left, right) =>
  fields.every((key) => left[key] === right[key]);

/** Local, bounded undo history; drafts never reach saved preferences. */
export function createPinHistory(storage, onStorageError = () => {}) {
  const key = "japan-housing.pin-history.v1";
  let entries = [];
  try {
    const saved = JSON.parse(storage.getItem(key) || "[]");
    if (Array.isArray(saved))
      entries = saved
        .filter(
          (entry) =>
            typeof entry?.id === "string" &&
            [entry.before, entry.after].every(
              (p) =>
                p &&
                typeof p.verified === "boolean" &&
                typeof p.location_basis === "string" &&
                ["latitude", "longitude"].every(
                  (k) => p[k] === null || Number.isFinite(p[k]),
                ),
            ),
        )
        .slice(-20);
  } catch {
    onStorageError();
  }
  function persist() {
    try {
      storage.setItem(key, JSON.stringify(entries));
    } catch {
      onStorageError();
    }
  }
  return {
    record(before, after) {
      if (samePin(before, after)) return;
      entries.push({
        id: before.id,
        before: pinPosition(before),
        after: pinPosition(after),
      });
      entries = entries.slice(-20);
      persist();
    },
    latest(destinations) {
      for (let index = entries.length - 1; index >= 0; index--) {
        const entry = entries[index];
        const current = destinations.find((d) => d.id === entry.id);
        if (current && samePin(current, entry.after))
          return { entry, current, index };
      }
      return null;
    },
    restored(destinations, change) {
      return destinations.map((d) =>
        d.id === change.entry.id ? { ...d, ...change.entry.before } : d,
      );
    },
    complete(change) {
      entries.splice(change.index, 1);
      persist();
    },
  };
}
