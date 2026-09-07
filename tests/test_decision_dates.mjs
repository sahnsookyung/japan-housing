import test from "node:test";
import assert from "node:assert/strict";
import { addCalendarMonths } from "../web/decision-charts.mjs";
test("stay presets clamp calendar month ends and preserve the selected local date", () => {
  assert.equal(addCalendarMonths("2024-01-31", 1), "2024-02-29");
  assert.equal(addCalendarMonths("2026-01-31", 1), "2026-02-28");
  assert.equal(addCalendarMonths("2026-09-10", 3), "2026-12-10");
  assert.equal(addCalendarMonths("2026-12-31", 2), "2027-02-28");
});
