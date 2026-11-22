// Wording for a check-in result's timing (backend routers/checkin.record_checkin), shared by
// the scanner (VolunteerCheckin.vue) and the slot dialog (TimeslotDialog.vue).
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
