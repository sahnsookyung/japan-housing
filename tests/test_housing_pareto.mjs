import assert from "node:assert/strict";
import test from "node:test";

import {
  housingParetoCandidate,
  housingParetoFrontierIds,
  housingParetoLocation,
  housingParetoPoints,
} from "../web/housing-pareto.mjs";

function housing(stableKey, price, location, overrides = {}) {
  return {
    stable_key: stableKey,
    classification: "eligible",
    decision_status: "new",
    track: "landing",
    ranking_price_yen: price,
    pareto_location_score: location,
    ...overrides,
  };
}

test("frontier keeps price and location trade-offs", () => {
  const records = [
    housing("cheap-far", 60_000, 90),
    housing("balanced", 70_000, 60),
    housing("dominated", 75_000, 70),
    housing("close", 90_000, 30),
  ];

  assert.deepEqual(
    [...housingParetoFrontierIds(records)].sort(),
    ["balanced", "cheap-far", "close"],
  );
});

test("equal choices and independent housing tracks stay on the frontier", () => {
  const records = [
    housing("landing-a", 70_000, 50),
    housing("landing-b", 70_000, 50),
    housing("long-term", 50_000, 20, { track: "long_term" }),
  ];

  assert.deepEqual(
    [...housingParetoFrontierIds(records)].sort(),
    ["landing-a", "landing-b", "long-term"],
  );
});

test("review, reserved, rejected, and invalid choices fail closed", () => {
  const records = [
    housing("review", 40_000, 10, { classification: "review" }),
    housing("reserved", 45_000, 10, { decision_status: "reserved" }),
    housing("rejected", 50_000, 10, { decision_status: "not_interested" }),
    housing("invalid", 55_000, null),
    housing("choice", 70_000, 40),
  ];

  assert.equal(housingParetoCandidate(records[0]), null);
  assert.deepEqual([...housingParetoFrontierIds(records)], ["choice"]);
});

test("filtered records recompute the frontier", () => {
  const records = [
    housing("dominant", 60_000, 30),
    housing("revealed-after-filter", 70_000, 40),
  ];

  assert.deepEqual([...housingParetoFrontierIds(records)], ["dominant"]);
  const filtered = records.filter((item) => item.stable_key !== "dominant");
  assert.deepEqual([...housingParetoFrontierIds(filtered)], ["revealed-after-filter"]);
  assert.equal(housingParetoPoints(filtered)[0].frontier, true);
});

test("each commute perspective computes an independent frontier", () => {
  const records = [
    housing("cheap-kichijoji", 60_000, 50, {
      pareto_location_scores: { balanced: 50, kichijoji: 20, shibuya: 80 },
    }),
    housing("close-shibuya", 70_000, 40, {
      pareto_location_scores: { balanced: 40, kichijoji: 40, shibuya: 20 },
    }),
  ];

  assert.equal(housingParetoLocation(records[0], "kichijoji"), 20);
  assert.deepEqual([...housingParetoFrontierIds(records, "kichijoji")], [
    "cheap-kichijoji",
  ]);
  assert.deepEqual(
    [...housingParetoFrontierIds(records, "shibuya")].sort(),
    ["cheap-kichijoji", "close-shibuya"],
  );
  assert.deepEqual(
    housingParetoPoints(records, null, "shibuya")
      .filter((point) => point.frontier)
      .map((point) => point.stableKey)
      .sort(),
    ["cheap-kichijoji", "close-shibuya"],
  );
});
