// Shared time formatting for the radio dialogs. The API returns UTC ISO strings
// with a Z suffix, so these parse correctly whatever the viewer's timezone.

/** "2h 14m" / "8m" / "just now" between two ISO timestamps (default: now). */
export function format_elapsed(from_iso, to_iso = null) {
  if (!from_iso) return ''
  const from = new Date(from_iso).getTime()
  const to   = to_iso ? new Date(to_iso).getTime() : Date.now()
  const mins = Math.floor((to - from) / 60000)
  if (!Number.isFinite(mins) || mins < 1) return 'just now'
  if (mins < 60) return `${mins}m`
  const h = Math.floor(mins / 60)
  const m = mins % 60
  return m ? `${h}h ${m}m` : `${h}h`
}

/** Local clock time, e.g. "Sat 2:05 PM". */
export function format_when(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return d.toLocaleString(undefined, {
    weekday: 'short', hour: 'numeric', minute: '2-digit',
  })
}
