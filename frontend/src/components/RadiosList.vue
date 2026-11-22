<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import Button    from 'primevue/button'
import InputText from 'primevue/inputtext'
import Message   from 'primevue/message'
import Tag       from 'primevue/tag'
import { format_elapsed, format_when } from './radio_time.js'

// The Radios tab's lists (RadiosTab.vue): what's checked out and with whom, what's on the
// shelf, and a log of every checkout and return, newest first.
//
// GET /api/radios gives every known radio with its open checkout, if any; the log adds
// GET /api/radios/history (completed checkouts, latest 500) — each completed one is two
// events, an open one is one. No backend of its own. RadiosTab calls reload() on the `radio`
// SSE event, so every browser stays current.

const props = defineProps({ role: String })
const emit  = defineEmits(['changed'])

const CAN_WRITE = ['organizer', 'checkin']
const can_write = computed(() => CAN_WRITE.includes(props.role))

const radios    = ref([])
const history   = ref([])
const filter    = ref('')
const err       = ref('')
const busy_code = ref('')

// Elapsed times are relative to now, so they need a nudge to stay honest.
const tick = ref(0)
let tick_timer = null

const matches = (...fields) => {
  const q = filter.value.trim().toLowerCase()
  return !q || fields.some(f => (f || '').toLowerCase().includes(q))
}
const out       = computed(() => radios.value.filter(r => r.out && matches(r.barcode, r.person_name)))
const available = computed(() => radios.value.filter(r => !r.out && matches(r.barcode)))

const log = computed(() => {
  const events = []
  for (const h of history.value) {
    events.push({ kind: 'out', at: h.checked_out_at, barcode: h.barcode, who: h.person_name, by: h.checked_out_by })
    events.push({ kind: 'in',  at: h.checked_in_at,  barcode: h.barcode, who: h.person_name, by: h.checked_in_by,
                  held: format_elapsed(h.checked_out_at, h.checked_in_at) })
  }
  for (const r of radios.value.filter(r => r.out))
    events.push({ kind: 'out', at: r.checked_out_at, barcode: r.barcode, who: r.person_name, by: r.checked_out_by })
  return events.filter(e => matches(e.barcode, e.who))
               .sort((a, b) => (b.at || '').localeCompare(a.at || ''))
})

async function reload() {
  err.value = ''
  try {
    const [r, h] = await Promise.all([fetch('/api/radios'), fetch('/api/radios/history?limit=500')])
    if (r.ok) radios.value = await r.json()
    else err.value = 'Failed to load radios.'
    if (h.ok) history.value = await h.json()
  } catch { err.value = 'Unable to reach the server.' }
}

async function check_in(barcode) {
  busy_code.value = barcode
  err.value = ''
  const form = new FormData()
  form.append('barcode', barcode)
  try {
    const r = await fetch('/api/radios/checkin', { method: 'POST', body: form })
    if (!r.ok) {
      const body = await r.json().catch(() => null)
      err.value = body?.detail || 'Check-in failed.'
    } else {
      emit('changed')
    }
  } catch { err.value = 'Unable to reach the server.' }
  finally {
    busy_code.value = ''
    await reload()
  }
}

onMounted(() => { reload(); tick_timer = setInterval(() => { tick.value++ }, 60000) })
onUnmounted(() => clearInterval(tick_timer))
defineExpose({ reload })

function elapsed(iso) {
  tick.value  // re-evaluate on the minute
  return format_elapsed(iso)
}
</script>

<template>
  <div class="radios">
    <div class="toolbar">
      <InputText v-model="filter" placeholder="Filter by barcode or person" autocomplete="off"
                 size="small" style="flex:1; min-width:12rem; max-width:22rem;" />
      <Button icon="pi pi-refresh" severity="secondary" text aria-label="Refresh" @click="reload" />
    </div>
    <Message v-if="err" severity="error" :closable="false" style="margin:0;">{{ err }}</Message>

    <section>
      <h3>Checked out <span class="count">{{ out.length }}</span></h3>
      <table class="radio-table">
        <thead>
          <tr><th>Barcode</th><th>Who</th><th>Since</th><th>Out for</th><th v-if="can_write"></th></tr>
        </thead>
        <tbody>
          <tr v-for="r in out" :key="r.barcode">
            <td><code>{{ r.barcode }}</code></td>
            <td>{{ r.person_name }}</td>
            <td>{{ format_when(r.checked_out_at) }}</td>
            <td>{{ elapsed(r.checked_out_at) }}</td>
            <td v-if="can_write" class="actions">
              <Button size="small" label="Check In" :loading="busy_code === r.barcode"
                      @click="check_in(r.barcode)" />
            </td>
          </tr>
          <tr v-if="!out.length">
            <td :colspan="can_write ? 5 : 4" class="empty">
              {{ filter ? 'Nothing matches that filter.' : 'No radios are out.' }}
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <section>
      <h3>Available <span class="count">{{ available.length }}</span></h3>
      <div v-if="available.length" class="shelf">
        <code v-for="r in available" :key="r.barcode" class="chip">{{ r.barcode }}</code>
      </div>
      <p v-else class="empty">
        {{ filter ? 'Nothing matches that filter.'
                  : (radios.length ? 'Every known radio is out.' : 'No radios on record yet; they register on first checkout.') }}
      </p>
    </section>

    <section>
      <h3>Log</h3>
      <table class="radio-table">
        <thead>
          <tr><th>When</th><th></th><th>Barcode</th><th>Who</th><th>By</th><th>Held for</th></tr>
        </thead>
        <tbody>
          <tr v-for="(e, i) in log" :key="i">
            <td>{{ format_when(e.at) }}</td>
            <td>
              <Tag v-if="e.kind === 'out'" severity="warn" value="Out" />
              <Tag v-else severity="success" value="In" />
            </td>
            <td><code>{{ e.barcode }}</code></td>
            <td>{{ e.who }}</td>
            <td class="muted">{{ e.by || '—' }}</td>
            <td class="muted">{{ e.held || '' }}</td>
          </tr>
          <tr v-if="!log.length">
            <td colspan="6" class="empty">Nothing yet.</td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>

<style scoped>
.radios  { display: flex; flex-direction: column; gap: 1rem; }
.toolbar { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }
section h3 { margin: 0 0 0.5rem; font-size: 1rem; display: flex; align-items: center; gap: 0.5rem; }
.count   { font-size: 0.8rem; font-weight: normal; color: var(--p-text-muted-color); }
.radio-table { width: 100%; border-collapse: collapse; }
.radio-table th {
  text-align: left; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.04em;
  color: var(--p-text-muted-color); padding: 0.35rem 0.5rem;
  border-bottom: 1px solid var(--p-content-border-color);
}
.radio-table td { padding: 0.45rem 0.5rem; border-bottom: 1px solid var(--p-content-border-color);
                  font-size: 0.9rem; }
.actions { text-align: right; }
.empty   { color: var(--p-text-muted-color); font-size: 0.88rem; }
.muted   { color: var(--p-text-muted-color); }
.shelf   { display: flex; flex-wrap: wrap; gap: 0.4rem; }
.chip    { padding: 0.15rem 0.5rem; border: 1px solid var(--p-content-border-color);
           border-radius: 4px; font-size: 0.85rem; }
</style>
