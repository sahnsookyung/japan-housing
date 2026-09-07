export const factFields = [
  ["recurring_total_yen", "Mandatory monthly total · ¥", 1, 100000000],
  [
    "initial_cost_yen",
    "Entry charges · ¥ (excluding prepaid rent)",
    1,
    100000000,
  ],
  ["exit_cost_yen", "Ordinary exit charges · ¥", 1, 100000000],
  ["deposit_yen", "Security deposit paid · ¥", 1, 100000000],
  ["refundable_deposit_yen", "Refundable part of deposit · ¥", 1, 100000000],
  ["upfront_rent_months", "Rent months prepaid before keys", 1, 36],
  ["room_area_m2", "Private room area · m²", "any", 100000],
  ["minimum_stay_months", "Minimum stay · calendar months", 1, 1200],
  ["notice_days", "Move-out notice · days", 1, 3650],
];
export function roomFactUpdate(form, record) {
  const values = {};
  for (const [key, label, step, max] of factFields) {
    const raw = form.get(key);
    if (raw == null || raw.trim() === "") continue;
    const value = Number(raw);
    if (
      !Number.isFinite(value) ||
      value < 0 ||
      value > max ||
      (step === 1 && !Number.isInteger(value))
    )
      throw new Error(
        `${label}: enter a valid nonnegative ${step === 1 ? "whole number" : "number"}.`,
      );
    values[key] = value;
  }
  for (const key of ["desk", "deposit_in_initial_cost"]) {
    const raw = form.get(key);
    if (raw === "true" || raw === "false") values[key] = raw === "true";
  }
  if (["anniversary", "calendar_daily"].includes(form.get("billing_basis")))
    values.billing_basis = form.get("billing_basis");
  const resolved = new Set(form.getAll("resolved_cost"));
  if (resolved.size)
    values.unknown_costs = (record.unknown_costs || []).filter(
      (gap) => !resolved.has(gap),
    );
  const deposit = values.deposit_yen ?? record.deposit_yen;
  const refund = values.refundable_deposit_yen ?? record.refundable_deposit_yen;
  if (deposit != null && refund != null && refund > deposit)
    throw new Error("The deposit refund cannot exceed the deposit paid.");
  if (!Object.keys(values).length)
    throw new Error(
      "Enter a fact or mark a cost question as resolved before saving.",
    );
  return values;
}
