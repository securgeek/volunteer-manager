<script setup>
import { ref, computed, onMounted } from 'vue'
import DataTable    from 'primevue/datatable'
import Column       from 'primevue/column'
import InputText    from 'primevue/inputtext'
import SelectButton from 'primevue/selectbutton'
import Button       from 'primevue/button'
import Message      from 'primevue/message'
import Tag          from 'primevue/tag'
import Dialog       from 'primevue/dialog'

// Every volunteer with their picks, answers and schedule (GET /api/admin/volunteers).
// Approval (orientation) is separate from cancelled (the volunteer's) and inactive (ours), so
// a row's state is the first that applies of inactive, cancelled, approved, pending. Approving
// stays in Pending Volunteers, which a pending row's Review opens; Make inactive / Reinstate
// are here (POST /api/admin/volunteers/set_inactive); assignment stays in the schedule.
// Purge (POST /api/admin/volunteers/purge) deletes inactive volunteers outright, here and on
// the portal, so test volunteers' addresses can sign up again; the organizer types the vol_id,
// or the count for Purge inactive, to confirm.
//
// Answer labels come from the portal's question list, as in PendingVolunteers.vue.
//
// The main section of the Volunteers tab (VolunteersTab.vue). `emails` maps vol_id to an
// address the tab decrypted in the browser; when present there's an Email column, and search
// matches it. `review` asks the tab to scroll to that volunteer's pending card.

const props = defineProps({ emails: { type: Object, default: () => ({}) } })
const emit  = defineEmits(['review'])
const has_emails = computed(() => Object.keys(props.emails).length > 0)

const loading  = ref(false)
const err      = ref('')
const vols     = ref([])
const fields   = ref([])
const search   = ref('')
const status   = ref('all')
const expanded = ref({})

const STATES = {
  pending:   { label: 'Pending',   severity: 'warn' },
  approved:  { label: 'Approved',  severity: 'success' },
  cancelled: { label: 'Cancelled', severity: 'secondary' },
  inactive:  { label: 'Inactive',  severity: 'secondary' },
}
function state(v) {
  if (v.inactive)  return 'inactive'
  if (v.cancelled) return 'cancelled'
  return v.status === 'active' ? 'approved' : 'pending'
}

const acting = ref(null)
async function set_inactive(v, inactive) {
  if (inactive && !confirm(`Make ${v.name} inactive?\n\nThey stay on any shifts they're ` +
      `assigned to, which will be listed as needing a replacement. Reinstate undoes this.`)) return
  acting.value = v.vol_id; err.value = ''
  try {
    const fd = new FormData()
    fd.append('vol_id', v.vol_id)
    fd.append('inactive', inactive ? '1' : '0')
    const r = await fetch('/api/admin/volunteers/set_inactive', { method: 'POST', body: fd })
    if (!r.ok) {
      const b = await r.json().catch(() => null)
      err.value = b?.detail || 'Failed to update the volunteer.'
    }
    await load()
  } catch { err.value = 'Unable to reach the server.' }
  finally { acting.value = null }
}

// ── purge ────────────────────────────────────────────────────────────────────
const purge_list   = ref([])     // the volunteers the dialog is about to delete
const purge_open   = ref(false)
const purge_typed  = ref('')
const purge_busy   = ref(false)
const purge_err    = ref('')
const purge_expect = computed(() => (purge_list.value.length === 1
  ? purge_list.value[0].vol_id : String(purge_list.value.length)))
const purge_shifts = computed(() => purge_list.value.reduce((n, v) => n + (v.shift_count || 0), 0))
const inactive_vols = computed(() => vols.value.filter(v => v.inactive))

function open_purge(list) {
  purge_list.value = list; purge_typed.value = ''; purge_err.value = ''
  purge_open.value = true
}

async function do_purge() {
  purge_busy.value = true; purge_err.value = ''
  const fd = new FormData()
  for (const v of purge_list.value) fd.append('vol_ids', v.vol_id)
  fd.append('confirm', purge_typed.value.trim())
  try {
    const r = await fetch('/api/admin/volunteers/purge', { method: 'POST', body: fd })
    const b = await r.json().catch(() => null)
    if (r.ok) { purge_open.value = false; await load() }
    else purge_err.value = b?.detail || `Purge failed (${r.status}).`
  } catch { purge_err.value = 'Unable to reach the server.' }
  finally { purge_busy.value = false }
}

async function load() {
  loading.value = true
  err.value     = ''
  try {
    const [v, f] = await Promise.all([fetch('/api/admin/volunteers'),
                                      fetch('/api/portal/signup/fields')])
    if (v.ok) vols.value = (await v.json()).volunteers
    else err.value = 'Failed to load volunteers.'
    if (f.ok) fields.value = (await f.json()).fields
  } catch {
    err.value = 'Unable to reach the server.'
  } finally {
    loading.value = false
  }
}

onMounted(load)
// `vols` lets the tab decrypt everyone's email_enc without fetching the list again.
defineExpose({ reload: load, vols })

const counts = computed(() => {
  const c = { all: vols.value.length, pending: 0, approved: 0, cancelled: 0, inactive: 0 }
  for (const v of vols.value) c[state(v)]++
  return c
})

const status_options = computed(() => [
  { value: 'all', label: `All (${counts.value.all})` },
  ...Object.entries(STATES).map(([k, s]) => ({ value: k, label: `${s.label} (${counts.value[k]})` })),
])

const shown = computed(() => {
  const q = search.value.trim().toLowerCase()
  return vols.value.filter(v =>
    (status.value === 'all' || state(v) === status.value) &&
    (!q || v.name.toLowerCase().includes(q) || v.vol_id.toLowerCase().includes(q) ||
     (props.emails[v.vol_id] || '').toLowerCase().includes(q)))
})

// Picks when there are any; the stored text for volunteers an organizer added.
function picks(v) {
  const prefs = v.picks.filter(p => p.kind === 'pref')
  if (prefs.length) return prefs.map(p => p.label)
  return (v.time_pref || '').split(';').map(s => s.trim()).filter(Boolean)
}
function extras(v) {
  if (v.add_any) return 'Any time'
  const ex = v.picks.filter(p => p.kind === 'add').map(p => p.label)
  if (ex.length) return ex.join(', ')
  return (v.add_time || '').split(';').map(s => s.trim()).filter(Boolean).join(', ')
}

function answer_rows(v) {
  const a = v.answers || {}
  const rows = []
  for (const f of fields.value) {
    if (!(f.key in a)) continue
    if (f.type === 'ack') continue   // agreed by everyone who submitted; not worth a row each
    if (f.type === 'check') { rows.push({ label: 'Flexible', value: a[f.key] ? 'Yes' : 'No' }); continue }
    let value = a[f.key]
    const opt = f.options?.find(o => o.value === value)
    if (opt) value = opt.label + (opt.detail && a[opt.detail.key] ? ` — ${a[opt.detail.key]}` : '')
    rows.push({ label: f.label, value })
  }
  return rows
}

function fmt_date(iso) { return iso ? new Date(iso).toLocaleDateString() : '' }
</script>

<template>
  <div>
    <Message v-if="err" severity="error" :closable="false" style="margin-bottom:0.75rem;">{{ err }}</Message>

    <div class="toolbar">
      <SelectButton v-model="status" :options="status_options" optionLabel="label"
                    optionValue="value" :allowEmpty="false" size="small" />
      <span class="spacer" />
      <InputText v-model="search" :placeholder="has_emails ? 'Search name, ID or email' : 'Search name or ID'"
                 size="small" style="width:14rem;" />
      <Button v-if="inactive_vols.length" size="small" severity="danger" variant="outlined"
              icon="pi pi-trash" :label="`Purge inactive (${inactive_vols.length})`"
              @click="open_purge(inactive_vols)" />
      <Button size="small" severity="secondary" icon="pi pi-refresh" :loading="loading"
              v-tooltip.top="'Reload'" @click="load" />
    </div>

    <Dialog v-model:visible="purge_open" modal :closable="!purge_busy" style="width:min(32rem, 95vw);"
            :header="purge_list.length === 1 ? 'Purge volunteer' : `Purge ${purge_list.length} volunteers`">
      <p style="margin-top:0;">
        Permanently deletes
        <template v-if="purge_list.length === 1"><strong>{{ purge_list[0].name }}</strong>
          (<span class="mono">{{ purge_list[0].vol_id }}</span>)</template>
        <template v-else>these inactive volunteers</template>
        here and on the volunteer portal, so the same email address can sign up again from
        scratch. This can't be undone.
      </p>
      <ul v-if="purge_list.length > 1" class="purge-names">
        <li v-for="v in purge_list.slice(0, 8)" :key="v.vol_id">{{ v.name }}
          <span class="mono muted">{{ v.vol_id }}</span></li>
        <li v-if="purge_list.length > 8" class="muted">and {{ purge_list.length - 8 }} more</li>
      </ul>
      <ul class="purge-what">
        <li>Their picks, answers, notes, ratings and check-ins are deleted.</li>
        <li v-if="purge_shifts">{{ purge_shifts }} assigned shift{{ purge_shifts === 1 ? '' : 's' }}
          become{{ purge_shifts === 1 ? 's' : '' }} empty.</li>
        <li>Portal sign-ins end; radio history keeps the name.</li>
      </ul>
      <label for="purge-confirm" class="small">
        Type <strong class="mono">{{ purge_expect }}</strong> to confirm
      </label>
      <InputText id="purge-confirm" v-model="purge_typed" size="small" autocomplete="off"
                 style="width:100%; margin-top:0.35rem;" @keyup.enter="purge_typed.trim() === purge_expect && do_purge()" />
      <Message v-if="purge_err" severity="error" :closable="false" style="margin-top:0.75rem;">
        {{ purge_err }}</Message>
      <template #footer>
        <Button size="small" severity="secondary" label="Cancel" :disabled="purge_busy"
                @click="purge_open = false" />
        <Button size="small" severity="danger" icon="pi pi-trash" label="Purge"
                :loading="purge_busy" :disabled="purge_typed.trim() !== purge_expect"
                @click="do_purge" />
      </template>
    </Dialog>

    <DataTable :value="shown" dataKey="vol_id" v-model:expandedRows="expanded" size="small"
               stripedRows :loading="loading && !vols.length"
               sortField="name" :sortOrder="1">
      <template #empty>
        <span class="muted">{{ vols.length ? 'No volunteers match.' : 'No volunteers yet.' }}</span>
      </template>
      <Column expander style="width:2.5rem" />
      <Column field="vol_id" header="ID" sortable style="width:8rem">
        <template #body="{ data }"><span class="mono">{{ data.vol_id }}</span></template>
      </Column>
      <Column field="name" header="Name" sortable />
      <Column v-if="has_emails" header="Email">
        <template #body="{ data }">
          <span v-if="props.emails[data.vol_id]" class="mono small">{{ props.emails[data.vol_id] }}</span>
          <span v-else class="muted">—</span>
        </template>
      </Column>
      <Column field="status" header="Status" sortable style="width:8rem">
        <template #body="{ data }">
          <Tag :value="STATES[state(data)].label" :severity="STATES[state(data)].severity" />
        </template>
      </Column>
      <Column header="Requested">
        <template #body="{ data }">
          <span v-if="picks(data).length">{{ picks(data).join(' · ') }}</span>
          <span v-else class="muted">—</span>
        </template>
      </Column>
      <Column field="shift_count" header="Shifts" sortable style="width:7rem">
        <template #body="{ data }">
          <span v-if="data.shift_count">{{ data.shift_count }}</span>
          <span v-else class="muted">not scheduled</span>
        </template>
      </Column>
      <Column style="width:6rem">
        <template #body="{ data }">
          <Button v-if="state(data) === 'pending'" size="small" variant="text" label="Review"
                  @click="emit('review', data.vol_id)" />
        </template>
      </Column>

      <template #expansion="{ data }">
        <div class="detail">
          <div class="col">
            <h4>Schedule</h4>
            <table v-if="data.shifts.length" class="shifts">
              <tr v-for="s in data.shifts" :key="`${s.day_key}_${s.time_key}_${s.pos_key}`">
                <td>{{ s.day_str || s.day_key }}</td>
                <td class="mono">{{ s.time_str || s.time_key }}</td>
                <td>{{ s.pos_str || s.pos_key }} <span class="muted">{{ s.pos_cat }}</span></td>
                <td><Tag v-if="s.checked_in" severity="success" value="Checked in" /></td>
              </tr>
            </table>
            <p v-else class="muted">Not scheduled yet.</p>
          </div>
          <div class="col">
            <h4>Availability</h4>
            <dl>
              <dt>Requested</dt>
              <dd>
                <template v-for="(p, i) in picks(data)" :key="p">{{ i + 1 }}. {{ p }}<br /></template>
                <span v-if="!picks(data).length" class="muted">—</span>
              </dd>
              <dt>Also available</dt><dd>{{ extras(data) || '—' }}</dd>
              <template v-if="data.created_at">
                <dt>Signed up</dt><dd>{{ fmt_date(data.created_at) }}</dd>
              </template>
              <template v-for="row in answer_rows(data)" :key="row.label">
                <dt>{{ row.label }}</dt><dd>{{ row.value }}</dd>
              </template>
            </dl>
            <Tag v-if="data.possible_duplicate_of.length" severity="warn"
                 :value="`Possible duplicate of ${data.possible_duplicate_of.join(', ')}`" />
            <div class="row-actions">
              <Button v-if="!data.inactive" size="small" severity="secondary" icon="pi pi-ban"
                      label="Make inactive" :loading="acting === data.vol_id"
                      @click="set_inactive(data, true)" />
              <template v-else>
                <Button size="small" icon="pi pi-replay" label="Reinstate"
                        :loading="acting === data.vol_id" @click="set_inactive(data, false)" />
                <Button size="small" severity="danger" variant="outlined" icon="pi pi-trash"
                        label="Purge" @click="open_purge([data])" />
              </template>
              <span v-if="data.cancelled && !data.inactive" class="muted">
                Cancelled by the volunteer; they can rejoin from the portal.
              </span>
            </div>
          </div>
        </div>
      </template>
    </DataTable>
  </div>
</template>

<style scoped>
.toolbar { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; margin-bottom: 0.75rem; }
.spacer  { flex: 1; }
.muted   { color: var(--p-text-muted-color); font-size: 0.85em; }
.mono    { font-family: monospace; }
.small   { font-size: 0.85em; }
.detail  { display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; padding: 0.5rem 1rem 0.75rem; }
.detail h4 { margin: 0 0 0.5rem; font-size: 0.9rem; }
.shifts  { border-collapse: collapse; font-size: 0.88rem; }
.shifts td { padding: 0.2rem 0.75rem 0.2rem 0; }
dl       { display: grid; grid-template-columns: 9rem 1fr; gap: 0.25rem 0.75rem; margin: 0 0 0.5rem;
           font-size: 0.88rem; }
dt       { color: var(--p-text-muted-color); }
dd       { margin: 0; }
.purge-names, .purge-what { margin: 0 0 0.75rem; padding-left: 1.25rem; font-size: 0.88rem; }
.purge-what li { margin-bottom: 0.2rem; }
.row-actions { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; margin-top: 0.75rem; }
@media (max-width: 48rem) { .detail { grid-template-columns: 1fr; } }
</style>
