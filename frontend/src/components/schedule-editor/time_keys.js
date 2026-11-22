// Deriving time keys.
//
// time_str lives on the global times_table row, not on day_times, so two days running
// different-length shifts from the same start — Saturday 1500-1900, Sunday 1500-1700 —
// cannot share a key: the second would silently relabel the first. A bare HHMM start is
// still the preferred key, so existing schedules keep the keys they have; the end time is
// only appended when a different range already holds that start.

/** Format a pair of "HH:MM" inputs as the "0800-1200" label the schedule displays. */
export function format_time_str(start, end) {
  return `${String(start).replace(':', '')}-${String(end).replace(':', '')}`
}

/**
 * Pick a time_key for a new shift that no other range already claims.
 *
 * @param {string} start    "HH:MM" from a time input.
 * @param {string} end      "HH:MM" from a time input.
 * @param {Array}  existing Times already defined, as `{ time_key, time_str }`.
 * @returns {string} "0800" when the start is free, "1500-1700" when it is taken by a
 *   different range, or that with a "-2", "-3", … suffix in the pathological case.
 */
export function derive_time_key(start, end, existing = []) {
  const hhmm     = String(start).replace(':', '')
  const time_str = format_time_str(start, end)
  const taken    = new Map(existing.map(t => [t.time_key, t.time_str]))

  // Free, or already this exact range — reuse the plain start.
  if (!taken.has(hhmm) || taken.get(hhmm) === time_str) return hhmm
  // Taken by a different range: the end time is what distinguishes them.
  if (!taken.has(time_str) || taken.get(time_str) === time_str) return time_str
  for (let n = 2; n < 100; n++) {
    if (!taken.has(`${time_str}-${n}`)) return `${time_str}-${n}`
  }
  return time_str
}
