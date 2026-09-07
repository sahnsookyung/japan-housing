import test from "node:test";
import assert from "node:assert/strict";
import { roomFactUpdate } from "../web/room-evidence.mjs";
test("blank inputs preserve facts while explicit zero and no are saved", () => {
  const form = new URLSearchParams({
    initial_cost_yen: "0",
    room_area_m2: "4.25",
    desk: "false",
    exit_cost_yen: "",
  });
  assert.deepEqual(roomFactUpdate(form, { unknown_costs: ["insurance"] }), {
    initial_cost_yen: 0,
    room_area_m2: 4.25,
    desk: false,
  });
});
test("resolving one cost question retains other unresolved questions", () => {
  const form = new URLSearchParams({
    resolved_cost: "insurance",
    initial_cost_yen: "20000",
  });
  assert.deepEqual(
    roomFactUpdate(form, { unknown_costs: ["insurance", "guarantor"] }),
    { initial_cost_yen: 20000, unknown_costs: ["guarantor"] },
  );
});
test("invalid amounts and a refund above the existing deposit are rejected", () => {
  for (const value of ["NaN", "-1", "1.5", "100000001"])
    assert.throws(() =>
      roomFactUpdate(new URLSearchParams({ initial_cost_yen: value }), {}),
    );
  assert.throws(
    () =>
      roomFactUpdate(new URLSearchParams({ refundable_deposit_yen: "20000" }), {
        deposit_yen: 10000,
      }),
    /refund/,
  );
  assert.throws(
    () => roomFactUpdate(new URLSearchParams(), {}),
    /Enter a fact/,
  );
});
