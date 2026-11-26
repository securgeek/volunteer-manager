// When a shift starts and which shift is closest, for name check-in (VolunteerCheckin.vue).
//   node --test src/lib/checkin_timing.test.js      (from frontend/; no packages needed)
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { shift_start_ms, closest_shift } from './checkin_timing.js'

const NY = 'America/New_York'

test('start is read in the event timezone, not the browser\'s', () => {
  // 08:00 in New York in summer (EDT, UTC-4) and winter (EST, UTC-5).
  assert.equal(shift_start_ms('2026-07-11', '0800-1200', NY), Date.UTC(2026, 6, 11, 12, 0))
  assert.equal(shift_start_ms('2026-12-05', '0800-1200', NY), Date.UTC(2026, 11, 5, 13, 0))
  assert.equal(shift_start_ms('2026-07-11', '0800-1200', 'Europe/London'),
               Date.UTC(2026, 6, 11, 7, 0))
  assert.equal(shift_start_ms('2026-07-11', '0800-1200', 'UTC'), Date.UTC(2026, 6, 11, 8, 0))
})

test('three-digit times and the day DST changes', () => {
  assert.equal(shift_start_ms('2026-07-11', '930-1130', NY), Date.UTC(2026, 6, 11, 13, 30))
  // 2026-03-08: New York springs forward at 02:00, so 08:00 that day is already EDT.
  assert.equal(shift_start_ms('2026-03-08', '0800-1200', NY), Date.UTC(2026, 2, 8, 12, 0))
  // 2026-11-01: falls back at 02:00, so 08:00 is EST.
  assert.equal(shift_start_ms('2026-11-01', '0800-1200', NY), Date.UTC(2026, 10, 1, 13, 0))
})

test('a shift past midnight starts on its own day', () => {
  assert.equal(shift_start_ms('2026-07-11', '2200-0200', NY), Date.UTC(2026, 6, 12, 2, 0))
})

test('null whenever the start can\'t be told', () => {
  assert.equal(shift_start_ms('', '0800-1200', NY), null)
  assert.equal(shift_start_ms(null, '0800-1200', NY), null)
  assert.equal(shift_start_ms('2026-07-11', 'Morning', NY), null)
  assert.equal(shift_start_ms('2026-07-11', '', NY), null)
  assert.equal(shift_start_ms('2026-02-30', '0800-1200', NY), null)
  assert.equal(shift_start_ms('2026-07-11', '2500-2600', NY), null)
  assert.equal(shift_start_ms('07/11/2026', '0800-1200', NY), null)
  assert.equal(shift_start_ms('2026-07-11', '0800-1200', 'Not/AZone'), null)
  assert.equal(shift_start_ms('2026-07-11', '0800-1200', null), null)
})

const H = 3600_000
const now = Date.UTC(2026, 6, 11, 12, 0)

test('closest is nearest start either side of now', () => {
  const shifts = [
    { start_ms: now - 3 * H, checked_in: false },
    { start_ms: now + 1 * H, checked_in: false },
    { start_ms: now + 26 * H, checked_in: false },
  ]
  assert.equal(closest_shift(shifts, now), 1)
  assert.equal(closest_shift(shifts, now - 2.5 * H), 0)
})

test('checked-in and undated shifts never win; none left is -1', () => {
  assert.equal(closest_shift([
    { start_ms: now, checked_in: true },
    { start_ms: null, checked_in: false },
    { start_ms: now + 5 * H, checked_in: false },
  ], now), 2)
  assert.equal(closest_shift([{ start_ms: now, checked_in: true },
                              { start_ms: null, checked_in: false }], now), -1)
  assert.equal(closest_shift([], now), -1)
})

test('a tie goes to the earlier shift', () => {
  assert.equal(closest_shift([
    { start_ms: now + H, checked_in: false },
    { start_ms: now - H, checked_in: false },
  ], now), 1)
})
