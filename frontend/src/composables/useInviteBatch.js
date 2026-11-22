import { ref, computed } from 'vue'

// Send invites: one early-access invite email per person, paced (SignupInvites.vue).
//
// Module-level on purpose: the Volunteer Portal dialog unmounts its contents when it closes,
// and a batch must keep going (and still show its progress when the dialog is reopened).
// Only closing the browser tab stops it, and the browser asks first.
//
// The server makes each invite, pushes it to the portal and mails it through the relay
// (POST /api/admin/portal/invites/send); the address goes no further and isn't kept. It
// enforces the same gap, and answers a send that comes early, or the portal's hourly limit,
// with 429 + Retry-After, which this waits out and retries instead of failing the person.

export const GAP_SEC   = 10
export const BATCH_MAX = 200

const rows      = ref([])     // { name, email, state: waiting|sending|sent|failed, note }
const running   = ref(false)
const paused    = ref(false)
const stopping  = ref(false)
const countdown = ref(0)
let on_finished = null

const done     = computed(() => rows.value.filter(r => r.state === 'sent' || r.state === 'failed').length)
const sent_n   = computed(() => rows.value.filter(r => r.state === 'sent').length)
const failed_n = computed(() => rows.value.filter(r => r.state === 'failed').length)
const percent  = computed(() => (rows.value.length ? Math.round(100 * done.value / rows.value.length) : 0))

const sleep = (ms) => new Promise(r => setTimeout(r, ms))

// Waits `sec` seconds, ticking the countdown; holds while paused, and gives up on Stop.
async function wait_out(sec) {
  countdown.value = sec
  while (countdown.value > 0 || paused.value) {
    if (stopping.value) return
    await sleep(1000)
    if (!paused.value && countdown.value > 0) countdown.value--
  }
}

async function send_one(row, days) {
  const fd = new FormData()
  fd.append('name', row.name)
  fd.append('email', row.email)
  fd.append('days', days)
  for (;;) {
    row.state = 'sending'; row.note = ''
    let resp
    try {
      resp = await fetch('/api/admin/portal/invites/send', { method: 'POST', body: fd })
    } catch {
      row.state = 'failed'; row.note = 'Unable to reach the server.'
      return
    }
    if (resp.ok) { row.state = 'sent'; return }
    const body = await resp.json().catch(() => null)
    if (resp.status === 429) {
      // Early (another organizer is sending too) or the hourly limit: wait, then try again.
      row.state = 'waiting'
      row.note  = body?.detail || 'Rate limited; waiting.'
      await wait_out(Number(resp.headers.get('Retry-After')) || GAP_SEC)
      if (stopping.value) { row.note = 'Stopped before sending.'; return }
      continue
    }
    row.state = 'failed'; row.note = body?.detail || `Failed (${resp.status}).`
    return
  }
}

function warn_unload(e) { e.preventDefault(); e.returnValue = '' }

async function start(people, days, finished) {
  if (running.value) return
  rows.value = people.map(p => ({ name: p.name, email: p.email, state: 'waiting', note: '' }))
  running.value = true; paused.value = false; stopping.value = false
  on_finished = finished
  window.addEventListener('beforeunload', warn_unload)
  try {
    for (let i = 0; i < rows.value.length; i++) {
      if (stopping.value) break
      await send_one(rows.value[i], days)
      if (stopping.value) break
      if (i < rows.value.length - 1) await wait_out(GAP_SEC)
    }
  } finally {
    running.value = false; countdown.value = 0
    window.removeEventListener('beforeunload', warn_unload)
    on_finished?.()
  }
}

function stop()          { stopping.value = true; paused.value = false }
function toggle_pause()  { paused.value = !paused.value }
function clear()         { if (!running.value) rows.value = [] }
// The people not yet sent, as lines for the box, so sending again picks up where it stopped.
function unsent_lines()  {
  return rows.value.filter(r => r.state !== 'sent').map(r => `${r.name}, ${r.email}`).join('\n')
}
// The finished callback belongs to whichever SignupInvites is mounted now.
function set_finished(fn) { on_finished = fn }

export function useInviteBatch() {
  return { rows, running, paused, stopping, countdown, done, sent_n, failed_n, percent,
           start, stop, toggle_pause, clear, unsent_lines, set_finished }
}
