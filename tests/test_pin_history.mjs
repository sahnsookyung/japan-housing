import test from "node:test";
import assert from "node:assert/strict";
import { createPinHistory } from "../web/pin-history.mjs";

function storage() {
  const values = new Map();
  return {
    getItem: (key) => values.get(key),
    setItem: (key, value) => values.set(key, value),
  };
}
const before = {
  id: "work",
  label: "Office",
  latitude: 35.7,
  longitude: 139.7,
  verified: true,
  location_basis: "user confirmed",
  weight: 1,
};
const after = {
  ...before,
  latitude: 35.8,
  verified: false,
  location_basis: "moved",
};

test("undo survives reload and restores exact coordinates and confirmation without reverting other edits", () => {
  const saved = storage();
  const history = createPinHistory(saved);
  assert.equal(history.latest([before]), null); // No recorded commit means no undo.
  history.record(before, after);
  const reloaded = createPinHistory(saved);
  const current = { ...after, label: "Renamed office", weight: 3 };
  const change = reloaded.latest([current]);
  assert.deepEqual(reloaded.restored([current], change), [
    { ...before, label: current.label, weight: 3 },
  ]);
  reloaded.complete(change);
  assert.equal(createPinHistory(saved).latest([before]), null);
});

test("multiple moves undo in reverse order and never overwrite an unrelated pin position", () => {
  const history = createPinHistory(storage());
  const third = { ...after, longitude: 139.9 };
  history.record(before, after);
  history.record(after, third);
  assert.equal(history.latest([{ ...third, longitude: 140 }]), null);
  let change = history.latest([third]);
  let restored = history.restored([third], change);
  assert.deepEqual(restored, [after]);
  history.complete(change);
  change = history.latest(restored);
  assert.deepEqual(history.restored(restored, change), [before]);
});

test("undo history is bounded and remains usable when storage is unavailable", () => {
  const saved = storage();
  const history = createPinHistory(saved);
  for (let i = 0; i < 25; i++)
    history.record({ ...before, latitude: i }, { ...after, latitude: i + 1 });
  assert.equal(
    JSON.parse(saved.getItem("japan-housing.pin-history.v1")).length,
    20,
  );
  const offline = createPinHistory({
    getItem() {
      throw Error();
    },
    setItem() {
      throw Error();
    },
  });
  offline.record(before, after);
  assert.deepEqual(offline.restored([after], offline.latest([after])), [
    before,
  ]);
});
