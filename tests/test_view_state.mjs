import test from "node:test";
import assert from "node:assert/strict";
import {
  defaultView,
  readViewState,
  viewSearch,
  readSelection,
  writeSelection,
} from "../web/view-state.mjs";

test("view links round trip search punctuation, filters, metric and page", () => {
  const view = {
    ...defaultView,
    query: "A & B / 中野",
    status: "all",
    sort: "distance",
    decision: "shortlisted",
    metric: "transit",
    view: "chart",
    offset: 50,
  };
  assert.deepEqual(readViewState(viewSearch(view)), view);
  assert.equal(viewSearch(defaultView), "");
});
test("invalid URL values fall back without reaching the API", () => {
  const view = readViewState(
    `?query=${"a".repeat(205)}&status=bad&metric=rail&offset=-3&sort=bad`,
  );
  assert.equal(view.query.length, 200);
  assert.deepEqual({ ...view, query: "" }, defaultView);
  assert.equal(readViewState("?offset=1.5").offset, 0);
});
test("comparison selections survive reload, reject malformed values and remain bounded", () => {
  let saved;
  const storage = {
    getItem: () => saved,
    setItem: (_, value) => {
      saved = value;
    },
  };
  writeSelection(storage, new Set(["a", "b", "c", "d"]));
  assert.deepEqual(readSelection(storage), ["a", "b", "c"]);
  saved = '["a", "a", null, "", 4, "b"]';
  assert.deepEqual(readSelection(storage), ["a", "b"]);
  saved = "bad JSON";
  assert.deepEqual(readSelection(storage), []);
  const blocked = {
    getItem: () => {
      throw Error();
    },
    setItem: () => {
      throw Error();
    },
  };
  assert.deepEqual(readSelection(blocked), []);
  assert.doesNotThrow(() => writeSelection(blocked, ["a"]));
});
