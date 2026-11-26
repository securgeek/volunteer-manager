// Wording for a check-in result's timing (backend routers/checkin.record_checkin), shared by
// the scanner (VolunteerCheckin.vue) and the slot dialog (TimeslotDialog.vue); plus when a
// shift starts and which one is closest, for VolunteerCheckin.vue's name search.
//
// The server decides early / late / on_time / unknown; this only says it in words. More than
// 45 minutes early or 10 minutes late comes back as needs_confirm, and the caller offers
// "Check in anyway", which resends with force.

function span(minutes) {
  if (minutes < 60) return `${minutes} min`
  if (minutes >= 24 * 60) {
    const d = Math.round(minutes / (24 * 60))
    return d === 1 ? 'about a day' : `about ${d} days`
  }
  const h = Math.floor(minutes / 60), m = minutes % 60
  return m ? `${h} h ${m} min` : `${h} h`
}

// "52 min early" / "1 h 5 min late" / '' when on time or unknown.
export function timing_label(result) {
  if (!result) return ''
  if (result.timing === 'early') return `${span(result.minutes)} early`
  if (result.timing === 'late')  return `${span(result.minutes)} late`
  return ''
}

// The sentence shown with "Check in anyway".
export function confirm_text(result) {
  const when = `${result.day} ${result.time}`
  if (result.timing === 'early')
    return `${span(result.minutes)} early for ${when}. Check-in opens 45 min before the shift.`
  return `${span(result.minutes)} late for ${when}. The shift started more than 10 min ago.`
}

// ── Finding the shift someone is most likely here for (name check-in) ─────────────
//
// The browser's copy of backend checkin_codes.shift_start: a shift starts at the day's
// event_date plus the start of its time_str range, read in the event timezone (App Settings).
// Display only: the server still decides early / late when the check-in is sent.

const RANGE = /^\s*(\d{3,4})\s*-\s*(\d{3,4})/
const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/

// How far ahead of UTC `tz`'s wall clock is at the instant `ms`, in ms. Throws on a bad tz.
function tz_offset(tz, ms) {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: tz, hourCycle: 'h23',
    year: 'numeric', month: 'numeric', day: 'numeric',
    hour: 'numeric', minute: 'numeric', second: 'numeric',
  }).formatToParts(new Date(ms))
  const p = Object.fromEntries(parts.map(x => [x.type, Number(x.value)]))
  const wall = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second)
  return wall - Math.floor(ms / 1000) * 1000
}

// When a shift starts, as epoch ms, or null when it can't be told (no date, a time_str that
// isn't a range, or an unknown timezone).
export function shift_start_ms(event_date, time_str, tz) {
  const d = ISO_DATE.exec((event_date || '').trim())
  const m = RANGE.exec(time_str || '')
  if (!d || !m || !tz) return null
  const hhmm = m[1].padStart(4, '0')
  const [y, mo, day, h, mi] = [+d[1], +d[2], +d[3], +hhmm.slice(0, 2), +hhmm.slice(2)]
  if (mo < 1 || mo > 12 || day < 1 || day > 31 || h > 23 || mi > 59) return null
  const wall = Date.UTC(y, mo - 1, day, h, mi)
  if (new Date(wall).getUTCDate() !== day) return null        // e.g. 2026-02-30
  try {
    // Two passes, so a start just after a DST change uses the offset in force then.
    const first = wall - tz_offset(tz, wall)
    return wall - tz_offset(tz, first)
  } catch {
    return null
  }
}

// Index of the shift not yet checked in whose start is nearest `now`, before or after;
// -1 when there is none. Shifts are { start_ms, checked_in }; a null start_ms never wins.
// A tie goes to the earlier shift.
export function closest_shift(shifts, now) {
  let best = -1, best_gap = Infinity
  shifts.forEach((s, i) => {
    if (s.checked_in || s.start_ms == null) return
    const gap = Math.abs(s.start_ms - now)
    if (gap < best_gap || (gap === best_gap && s.start_ms < shifts[best].start_ms)) {
      best = i
      best_gap = gap
    }
  })
  return best
}
