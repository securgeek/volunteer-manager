<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import Button    from 'primevue/button'
import InputText from 'primevue/inputtext'
import Message   from 'primevue/message'

import BarcodeScanner from './BarcodeScanner.vue'
import { timing_label, confirm_text, shift_start_ms, closest_shift } from '../lib/checkin_timing.js'

// Inline at the top of the Schedule tab (Admin.vue), mounted only while open, so the camera
// runs only then. Two ways in, both staying open for a run of check-ins:
//   - scan the code from a volunteer's schedule email (POST /api/checkin/scan)
//   - find them by name and press Check in on a shift (POST /api/checkin_vol)
// Both go through the server's record_checkin, so the same rule applies.
//
// More than 45 min early or 10 min late comes back as needs_confirm with nothing written.
// The request is held as `pending` until the operator picks Check in anyway (resent with force)
// or Skip; further scans and name check-ins are ignored meanwhile, so the question can't be
// lost under a new one.
//
// The name list is built from the `schedule` prop (Admin.vue's GET /api/get_schedule, which it
// reloads on every `checkin` event), so a shift's check-in status stays live, including
// check-ins made at another desk. The "Next" shift is the one not yet checked in whose start
// is nearest now, in the event timezone from /api/app_config.
const props = defineProps({
  schedule: { type: Array, default: () => [] },
})
const emit = defineEmits(['close'])

const MAX_MATCHES = 8

const busy    = ref(false)
const err     = ref('')
const pending = ref(null)   // { req, result } awaiting Check in anyway / Skip
const done    = ref([])     // newest first, like a receipt
const query   = ref('')
const tz      = ref(null)   // event timezone; until it loads, no shift is marked Next
const now     = ref(Date.now())

let tick = null
onMounted(async () => {
  tick = setInterval(() => { now.value = Date.now() }, 60_000)
  try {
    const r = await fetch('/api/app_config')
    if (r.ok) tz.value = (await r.json()).event_tz || null
  } catch { /* no Next marker; check-in itself still works */ }
})
onUnmounted(() => clearInterval(tick))

// Checked in during this visit, before the schedule reload arrives.
const checked_here = computed(() => new Set(
  done.value.filter(r => r.checked_in).map(r => `${r.vol_id}|${r.slot_key}`)))

// Everyone with a shift, each with their shifts in schedule order (day, then time).
const volunteers = computed(() => {
  const by_id = new Map()
  for (const day of props.schedule) {
    for (const time of day.times ?? []) {
      for (const cat of time.categories ?? []) {
        for (const pos of cat.positions ?? []) {
          if (!pos.vol_id) continue
          let v = by_id.get(pos.vol_id)
          if (!v) {
            v = { vol_id: pos.vol_id, name: pos.vol_name, nick: pos.vol_nick || '', shifts: [] }
            v.search = `${v.name} ${v.nick}`.toLowerCase()
            by_id.set(pos.vol_id, v)
          }
          v.shifts.push({
            key: pos.key, day: pos.day, time: pos.time,
            position: pos.title, category: pos.category,
            needs_replacement: pos.needs_replacement,
            checked_in: pos.checked_in || checked_here.value.has(`${pos.vol_id}|${pos.key}`),
            start_ms: shift_start_ms(day.event_date, pos.time, tz.value),
          })
        }
      }
    }
  }
  return [...by_id.values()].sort((a, b) => a.name.localeCompare(b.name))
})

const all_matches = computed(() => {
  const q = query.value.trim().toLowerCase()
  if (q.length < 2) return []
  return volunteers.value.filter(v => v.search.includes(q))
})

const matches = computed(() => all_matches.value.slice(0, MAX_MATCHES).map(v => ({
  ...v, next: closest_shift(v.shifts, now.value),
})))

// req is { code } from the scanner or { vol_id, slot_key } from the name search.
async function send(req, force) {
  busy.value = true
  err.value  = ''
  const form = new FormData()
  let url
  if (req.code !== undefined) {
    url = '/api/checkin/scan'
    form.append('code', req.code)
  } else {
    url = '/api/checkin_vol'
    form.append('vol_id', req.vol_id)
    form.append('pos_key', req.slot_key)
  }
  if (force) form.append('force', 'true')
  try {
    const r    = await fetch(url, { method: 'POST', body: form })
    const body = await r.json().catch(() => null)
    if (!r.ok) {
      err.value = body?.detail || 'Check-in failed.'
    } else if (body.needs_confirm) {
      pending.value = { req, result: body }
    } else {
      done.value.unshift(body)
      if (req.code === undefined) query.value = ''   // ready for the next name
    }
  } catch {
    err.value = 'Unable to reach the server.'
  } finally {
    busy.value = false
  }
}

function scanned(code) {
  if (busy.value || pending.value) return
  send({ code }, false)
}

function check_in_by_name(vol, shift) {
  if (busy.value || pending.value) return
  send({ vol_id: vol.vol_id, slot_key: shift.key }, false)
}

async function check_in_anyway() {
  const { req } = pending.value
  pending.value = null
  await send(req, true)
}

function skip() {
  pending.value = null
}
</script>

<template>
  <div class="checkin-panel">
    <h3>Check in volunteers</h3>
    <div style="display:flex; flex-direction:column; gap:1rem;">
      <p style="margin:0; color:var(--p-text-muted-color); font-size:0.9rem;">
        Scan the code from the volunteer's schedule email (each shift has its own code), or
        find them by name below.
      </p>

      <BarcodeScanner @scanned="scanned" />

      <div class="by-name">
        <label for="checkin-name" class="by-name-label">Find by name</label>
        <InputText id="checkin-name" v-model="query" placeholder="Name or handle"
                   autocomplete="off" style="width:100%;" />

        <div v-if="query.trim().length >= 2 && !all_matches.length" class="muted">
          No one with a shift matches “{{ query.trim() }}”.
        </div>

        <div v-for="v in matches" :key="v.vol_id" class="match">
          <div class="match-name"><strong>{{ v.name }}</strong> <span class="muted">{{ v.nick }}</span></div>
          <div v-for="(s, i) in v.shifts" :key="s.key"
               :class="['shift-row', { next: i === v.next, done: s.checked_in }]">
            <span>{{ s.day }} {{ s.time }} · {{ s.position }}</span>
            <span v-if="i === v.next" class="next-tag">Next</span>
            <span v-if="s.needs_replacement" class="muted small">cancelled / inactive</span>
            <span class="shift-action">
              <span v-if="s.checked_in" class="checked">
                <i class="pi pi-check-circle" /> Checked in
              </span>
              <Button v-else size="small" label="Check in" icon="pi pi-check"
                      :severity="i === v.next ? undefined : 'secondary'"
                      :disabled="busy || !!pending" @click="check_in_by_name(v, s)" />
            </span>
          </div>
        </div>

        <div v-if="all_matches.length > MAX_MATCHES" class="muted small">
          {{ all_matches.length - MAX_MATCHES }} more. Keep typing to narrow it down.
        </div>
      </div>

      <Message v-if="err" severity="error" :closable="false" style="margin:0;">{{ err }}</Message>

      <div v-if="pending" class="confirm">
        <div class="confirm-who">
          <i class="pi pi-exclamation-triangle" />
          <strong>{{ pending.result.name }}</strong>
          <span>{{ pending.result.category }} / {{ pending.result.position }}</span>
        </div>
        <div>{{ confirm_text(pending.result) }}</div>
        <div class="confirm-actions">
          <Button size="small" label="Check in anyway" icon="pi pi-check" severity="warn"
                  :loading="busy" @click="check_in_anyway()" />
          <Button size="small" label="Skip" severity="secondary" @click="skip()" />
        </div>
      </div>

      <div v-if="done.length" class="results">
        <div v-for="r in done" :key="r.slot_key + r.vol_id" class="result-row">
          <i :class="r.already ? 'pi pi-info-circle' : 'pi pi-check-circle'"
             :style="{ color: r.already ? 'var(--p-text-muted-color)' : 'var(--p-green-500)' }" />
          <strong>{{ r.name }}</strong>
          <span>{{ r.day }} {{ r.time }} · {{ r.position }}</span>
          <span class="tag">
            <template v-if="r.already">already checked in</template>
            <template v-else-if="timing_label(r)">{{ timing_label(r) }}</template>
            <template v-else-if="r.timing === 'unknown'">no shift date set</template>
          </span>
        </div>
      </div>
    </div>

    <div class="panel-actions">
      <Button label="Done" severity="secondary" @click="emit('close')" />
    </div>
  </div>
</template>

<style scoped>
.checkin-panel { border: 1px solid var(--p-content-border-color); border-radius: 6px;
                 padding: 1rem; max-width: 34rem; margin-bottom: 0.75rem; }
.checkin-panel h3 { margin: 0 0 0.75rem; font-size: 1rem; }
.panel-actions { display: flex; justify-content: flex-end; gap: 0.5rem; margin-top: 1rem; }
.muted { color: var(--p-text-muted-color); }
.small { font-size: 0.8rem; }
.by-name { display: flex; flex-direction: column; gap: 0.5rem; }
.by-name-label { font-size: 0.9rem; font-weight: 600; }
.match {
  display: flex; flex-direction: column; gap: 0.3rem;
  padding: 0.5rem 0.6rem;
  border: 1px solid var(--p-content-border-color);
  border-radius: 6px;
  font-size: 0.9rem;
}
.shift-row {
  display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap;
  padding: 0.3rem 0.5rem;
  border: 1px solid transparent;
  border-radius: 6px;
}
.shift-row.next { border-color: var(--p-primary-color); }
.shift-row.done { color: var(--p-text-muted-color); }
.next-tag {
  font-size: 0.75rem; font-weight: 600;
  padding: 0 0.4rem; border-radius: 4px;
  background: var(--p-primary-color); color: var(--p-primary-contrast-color);
}
.shift-action { margin-left: auto; }
.checked { color: var(--p-green-500); font-size: 0.85rem; white-space: nowrap; }
.confirm {
  display: flex; flex-direction: column; gap: 0.5rem;
  padding: 0.75rem;
  border: 1px solid var(--p-orange-400);
  border-radius: 6px;
  font-size: 0.9rem;
}
.confirm-who { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
.confirm-who .pi { color: var(--p-orange-400); }
.confirm-actions { display: flex; gap: 0.5rem; flex-wrap: wrap; }
.results { display: flex; flex-direction: column; gap: 0.4rem; }
.result-row {
  display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap;
  font-size: 0.9rem;
  padding: 0.4rem 0.6rem;
  background: var(--p-content-background);
  border: 1px solid var(--p-content-border-color);
  border-radius: 6px;
}
.tag { margin-left: auto; color: var(--p-text-muted-color); font-size: 0.8rem; }
</style>
