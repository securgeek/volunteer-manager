<script setup>
import { ref, onMounted } from 'vue'
import Button    from 'primevue/button'
import InputText from 'primevue/inputtext'
import Message   from 'primevue/message'
import Tag       from 'primevue/tag'

// Portal signups waiting on an organizer (backend/routers/portal_admin.py). Approve (they've
// been through orientation), with an optional badge number that becomes their vol_id, or make
// them inactive (the decline endpoint). Neither schedules anyone: the picks stay preferences,
// and assignment is done in the schedule as usual.
//
// A signup whose name resembles an existing volunteer is shown beside that record. Merging
// the two isn't built yet; approve as a separate person, or make them inactive.
//
// Answer labels come from the portal's own question list (GET /api/portal/signup/fields), so
// they always match what the volunteer was asked.
//
// A section of the Volunteers tab (VolunteersTab.vue), not a dialog: it loads itself, renders
// nothing while the queue is empty, and gives each card the id `pending-<vol_id>` so the
// list's Review button can scroll to it. `changed` lets the tab refresh the list and badge.

const emit = defineEmits(['changed'])

const loading = ref(false)
const err     = ref('')
const pending = ref([])
const fields  = ref([])
const badge   = ref({})     // vol_id -> badge number being typed
const acting  = ref(null)

async function load() {
  loading.value = true
  err.value     = ''
  try {
    const [p, f] = await Promise.all([fetch('/api/admin/portal/pending'),
                                      fetch('/api/portal/signup/fields')])
    if (p.ok) pending.value = (await p.json()).pending
    else err.value = 'Failed to load pending signups.'
    if (f.ok) fields.value = (await f.json()).fields
  } catch {
    err.value = 'Unable to reach the server.'
  } finally {
    loading.value = false
  }
}

onMounted(load)
defineExpose({ reload: load })

function answer_rows(vol) {
  const a = vol.answers || {}
  const rows = []
  for (const f of fields.value) {
    if (!(f.key in a)) continue
    if (f.type === 'ack') { rows.push({ label: 'Agreed', value: f.label }); continue }
    if (f.type === 'check') { rows.push({ label: 'Flexible', value: a[f.key] ? 'Yes' : 'No' }); continue }
    let value = a[f.key]
    const opt = f.options?.find(o => o.value === value)
    if (opt) value = opt.label + (opt.detail && a[opt.detail.key] ? ` — ${a[opt.detail.key]}` : '')
    rows.push({ label: f.label, value })
  }
  // Anything the current question list doesn't describe (an older form) still shows.
  const known = new Set(fields.value.flatMap(f => [f.key, ...(f.options || [])
    .filter(o => o.detail).map(o => o.detail.key)]))
  for (const [k, v] of Object.entries(a)) if (!known.has(k)) rows.push({ label: k, value: String(v) })
  return rows
}

const prefs  = (vol) => vol.picks.filter(p => p.kind === 'pref')
const extras = (vol) => vol.picks.filter(p => p.kind === 'add')

function fmt_date(iso) { return iso ? new Date(iso).toLocaleString() : '' }

async function act(vol, action) {
  if (action === 'decline' && !confirm(`Make ${vol.name} inactive?\n\nTheir signup stays ` +
      `on file and can be reinstated from Admin → Volunteers.`)) return
  acting.value = vol.vol_id
  err.value    = ''
  try {
    const fd = new FormData()
    fd.append('vol_id', vol.vol_id)
    if (action === 'approve' && (badge.value[vol.vol_id] || '').trim())
      fd.append('new_vol_id', badge.value[vol.vol_id].trim())
    const resp = await fetch(`/api/admin/portal/${action}`, { method: 'POST', body: fd })
    if (resp.ok) {
      pending.value = pending.value.filter(p => p.vol_id !== vol.vol_id)
      emit('changed')
    } else {
      const body = await resp.json().catch(() => null)
      err.value = body?.detail || `Failed to ${action}.`
    }
  } catch {
    err.value = 'Unable to reach the server.'
  } finally {
    acting.value = null
  }
}
</script>

<template>
  <section v-if="pending.length || err" class="pending">
    <h3>Awaiting approval ({{ pending.length }})</h3>
    <Message v-if="err" severity="error" :closable="false" style="margin-bottom:0.75rem;">{{ err }}</Message>

    <div v-for="vol in pending" :key="vol.vol_id" :id="`pending-${vol.vol_id}`" class="card">
      <div class="head">
        <strong>{{ vol.name }}</strong>
        <span class="muted small mono">{{ vol.vol_id }}</span>
        <span class="spacer" />
        <span class="muted small">{{ fmt_date(vol.created_at) }}</span>
      </div>

      <div v-if="vol.possible_duplicates.length" class="dupe">
        <Tag severity="warn" value="Possible duplicate" />
        <span>Their name resembles
          <template v-for="(d, i) in vol.possible_duplicates" :key="d.vol_id">
            <strong>{{ d.name }}</strong> ({{ d.vol_id }}, {{ d.status }}){{ i < vol.possible_duplicates.length - 1 ? ', ' : '' }}
          </template>.
          They may have signed up before with another address.
        </span>
      </div>

      <dl class="facts">
        <dt>Shifts</dt>
        <dd>
          <template v-for="p in prefs(vol)" :key="p.day_key + p.time_key">
            {{ p.rank }}. {{ p.label }}<br />
          </template>
          <span v-if="!prefs(vol).length" class="muted">none</span>
        </dd>
        <dt>Also available</dt>
        <dd>{{ vol.add_any ? 'Any time' : (extras(vol).map(p => p.label).join(', ') || '—') }}</dd>
        <template v-for="row in answer_rows(vol)" :key="row.label + row.value">
          <dt>{{ row.label }}</dt><dd>{{ row.value }}</dd>
        </template>
      </dl>

      <div class="actions">
        <InputText v-model="badge[vol.vol_id]" size="small" placeholder="Badge # (optional)"
                   style="width:10rem;" maxlength="32" />
        <Button size="small" label="Approve" icon="pi pi-check"
                :loading="acting === vol.vol_id" @click="act(vol, 'approve')" />
        <Button size="small" label="Make inactive" severity="secondary" icon="pi pi-ban"
                :disabled="acting === vol.vol_id" @click="act(vol, 'decline')" />
      </div>
    </div>
  </section>
</template>

<style scoped>
.pending h3 { margin: 0 0 0.6rem; font-size: 1rem; }
.pending { margin-bottom: 1.25rem; }
.muted  { color: var(--p-text-muted-color); }
.small  { font-size: 0.8rem; }
.mono   { font-family: monospace; }
.spacer { flex: 1; }
.card   { border: 1px solid var(--p-content-border-color); border-radius: 6px;
          padding: 0.85rem 1rem; margin-bottom: 0.75rem; }
.head   { display: flex; align-items: baseline; gap: 0.6rem; margin-bottom: 0.5rem; }
.dupe   { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap;
          font-size: 0.85rem; margin-bottom: 0.5rem; }
.facts  { display: grid; grid-template-columns: 10rem 1fr; gap: 0.3rem 1rem; margin: 0;
          font-size: 0.88rem; }
.facts dt { color: var(--p-text-muted-color); }
.facts dd { margin: 0; }
.actions { display: flex; gap: 0.5rem; align-items: center; margin-top: 0.75rem; flex-wrap: wrap; }
</style>
