import test from "node:test";
import assert from "node:assert/strict";
import { plottedFrontier, frontierPath } from "../web/decision-charts.mjs";
test("plotted frontier retains trade-offs and drops dominated values and duplicate vertices", () => {
  const points = [
    { price: 50, value: 10 },
    { price: 30, value: 30 },
    { price: 30, value: 40 },
    { price: 40, value: 35 },
    { price: 50, value: 10 },
    { price: null, value: 0 },
  ];
  assert.deepEqual(plottedFrontier(points), [
    { price: 30, value: 30 },
    { price: 50, value: 10 },
  ]);
  assert.equal(points.length, 6);
});
test("single-point and multiple-point boundaries use steps rather than interpolation", () => {
  assert.equal(
    frontierPath(
      [],
      (v) => v,
      (v) => v,
    ),
    "",
  );
  assert.equal(
    frontierPath(
      [{ price: 30, value: 20 }],
      (v) => v,
      (v) => v,
    ),
    "M 30 35 V 20 H 600",
  );
  assert.equal(
    frontierPath(
      [
        { price: 30, value: 20 },
        { price: 50, value: 10 },
      ],
      (v) => v,
      (v) => v,
    ),
    "M 30 35 V 20 H 50 V 10 H 600",
  );
});
