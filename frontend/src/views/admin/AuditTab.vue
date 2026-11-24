<script setup>
import { ref, reactive, computed, onMounted, watch } from 'vue'
import Button    from 'primevue/button'
import DataTable from 'primevue/datatable'
import Column    from 'primevue/column'
import InputText from 'primevue/inputtext'
import Select    from 'primevue/select'
import Tag       from 'primevue/tag'
import Message   from 'primevue/message'

// The Audit Log tab (backend routers/audit_log.py), for organizers holding audit-log access:
// where security alerts go, and the audit_events table, newest first, filtered and paged.
//
// Everything shown here is text from the log, and parts of it (a username typed at sign-in,
// a user agent, a snippet of a request) were chosen by whoever sent the request. It's only
// ever interpolated ({{ }}), never v-html.

const SEVERITIES = [
  { label: 'All', value: 'info' }, { label: 'Low and up', value: 'low' },
  { label: 'Medium and up', value: 'medium' }, { label: 'High and up', value: 'high' },
  { label: 'Critical', value: 'critical' },
]
const SEVERITY_TAG = { info: 'secondary', low: 'info', medium: 'warn', high: 'danger', critical: 'danger' }
// Common event groups for the Event filter; any other prefix can be typed in.
const GROUPS = [
  { label: 'Any event', value: '' },
  { label: 'Alerts (alert.)', value: 'alert.' },
  { label: 'Sign-in (auth.)', value: 'auth.' },
  { label: 'Attack signals (security.)', value: 'security.' },
  { label: 'Portal anomalies (portal.anomaly)', value: 'portal.anomaly' },
  { label: 'Users (user.)', value: 'user.' },
  { label: 'Passwords (password.)', value: 'password.' },
  { label: 'Passkeys (passkey.)', value: 'passkey.' },
  { label: 'Export API keys (api_key.)', value: 'api_key.' },
  { label: 'Volunteers (volunteer.)', value: 'volunteer.' },
  { label: 'Schedule (schedule.)', value: 'schedule.' },
  { label: 'Settings (settings.)', value: 'settings.' },
]
const SOURCES = [
  { label: 'Both sites', value: '' }, { label: 'Organizer site', value: 'backend' },
  { label: 'Volunteer portal', value: 'portal' },
]
const PAGE = 100

const filters = reactive({ event: '', min_severity: 'info', actor: '', ip: '', source: '', q: '' })
const events   = ref([])
const next_id  = ref(null)
const loading  = ref(false)
const error    = ref('')
const expanded = ref({})

function query(before_id) {
  const p = new URLSearchParams({ limit: String(PAGE) })
  for (const [k, v] of Object.entries(filters)) if (v && !(k === 'min_severity' && v === 'info')) p.set(k, v)
  if (before_id) p.set('before_id', String(before_id))
  return p
}

async function load(more = false) {
  loading.value = true
  error.value = ''
  try {
    const resp = await fetch(`/api/admin/audit?${query(more ? next_id.value : null)}`)
    const body = await resp.json().catch(() => ({}))
    if (!resp.ok) { error.value = body.detail || "Couldn't load the audit log."; return }
    events.value = more ? [...events.value, ...body.events] : body.events
    next_id.value = body.next_before_id
  } catch {
    error.value = "Couldn't reach the server."
  } finally {
    loading.value = false
  }
}

// Typing in a text filter reloads after a pause; the selects reload at once.
let timer = null
watch(() => [filters.actor, filters.ip, filters.q], () => {
  clearTimeout(timer)
  timer = setTimeout(() => load(), 400)
})
watch(() => [filters.event, filters.min_severity, filters.source], () => load())

function clear_filters() {
  Object.assign(filters, { event: '', min_severity: 'info', actor: '', ip: '', source: '', q: '' })
}
const filtered = computed(() => Object.entries(filters).some(
  ([k, v]) => v && !(k === 'min_severity' && v === 'info')))

function when(ts) {
  return ts ? new Date(ts).toLocaleString() : ''
}
function details(row) {
  return JSON.stringify(row.data || {}, null, 2)
}
function row_class(row) {
  if (row.event === 'alert.fired') return 'alert-row'
  if (row.severity === 'high' || row.severity === 'critical') return 'high-row'
  return ''
}

// ── where alerts go ───────────────────────────────────────────────────────────
const status  = ref(null)
const testing = ref(false)
const test_result = ref(null)     // { ok, text }

async function load_status() {
  try {
    const resp = await fetch('/api/admin/audit/status')
    if (resp.ok) status.value = await resp.json()
  } catch { /* the card just stays empty */ }
}

async function send_test() {
  testing.value = true
  test_result.value = null
  try {
    const resp = await fetch('/api/admin/audit/test_alert', { method: 'POST' })
    const body = await resp.json().catch(() => ({}))
    if (!resp.ok) test_result.value = { ok: false, text: body.detail || 'The test alert failed.' }
    else if (!body.recipients) test_result.value = { ok: false, text: 'No one to send it to: add an email address in Profile.' }
    else if (body.reached === body.recipients)
      test_result.value = { ok: true, text: `Sent to ${body.reached} ${body.reached === 1 ? 'person' : 'people'}. Check your inbox.` }
    else test_result.value = { ok: false, text: `Reached ${body.reached} of ${body.recipients}. The failures are in the log below.` }
  } catch {
    test_result.value = { ok: false, text: "Couldn't reach the server." }
  } finally {
    testing.value = false
    load(); load_status()
  }
}

onMounted(() => { load(); load_status() })
</script>

<template>
  <div class="tab">
    <!-- where alerts go -->
    <section v-if="status" class="card">
      <div class="card-head">
        <strong>Security alerts</strong>
        <Button size="small" label="Send test alert" icon="pi pi-send" severity="secondary"
                :loading="testing" @click="send_test" />
      </div>
      <Message v-if="!status.recipients" severity="error" :closable="false">
        Alerts aren't reaching anyone: nobody with audit log access has an email address.
        Add yours in Profile.
      </Message>
      <Message v-if="!status.portal_configured" severity="warn" :closable="false">
        The volunteer portal isn't connected, so alerts can't be emailed (they're still
        recorded here).
      </Message>
      <p class="muted">
        Alerts go to everyone with audit log access who has an email address:
        <template v-for="(h, i) in status.holders" :key="h.username">
          <span :class="{ missing: !h.has_email }">{{ h.username }}<template v-if="!h.has_email"> (no email)</template></span><template v-if="i < status.holders.length - 1">, </template>
        </template>.
        Give someone else access under Admin → Users. This table keeps
        {{ status.retention_days ? `${status.retention_days} days` : 'everything' }}.
      </p>
      <p v-if="status.last_alert" class="muted">
        Last alert: {{ status.last_alert.message }}, {{ when(status.last_alert.ts) }}.
      </p>
      <p v-if="status.last_failure" class="muted failure">
        Last delivery failure: {{ when(status.last_failure.ts) }}
        <template v-if="status.last_failure.data?.error">({{ status.last_failure.data.error }})</template>
      </p>
      <p v-if="test_result" :class="test_result.ok ? 'ok' : 'failure'">{{ test_result.text }}</p>
    </section>

    <!-- filters -->
    <div class="filters">
      <Select v-model="filters.event" :options="GROUPS" optionLabel="label" optionValue="value"
              editable placeholder="Event" size="small" class="f-event" />
      <Select v-model="filters.min_severity" :options="SEVERITIES" optionLabel="label"
              optionValue="value" size="small" class="f-sev" />
      <Select v-model="filters.source" :options="SOURCES" optionLabel="label" optionValue="value"
              size="small" class="f-src" />
      <InputText v-model="filters.actor" placeholder="Who" size="small" class="f-short" />
      <InputText v-model="filters.ip" placeholder="Address starts with" size="small" class="f-short" />
      <InputText v-model="filters.q" placeholder="Message contains" size="small" class="f-short" />
      <Button size="small" icon="pi pi-filter-slash" severity="secondary" variant="text"
              v-tooltip.top="'Clear filters'" :disabled="!filtered" @click="clear_filters" />
      <Button size="small" icon="pi pi-refresh" severity="secondary" variant="text"
              v-tooltip.top="'Refresh'" :loading="loading" @click="load()" />
    </div>

    <Message v-if="error" severity="error" :closable="false">{{ error }}</Message>

    <DataTable :value="events" dataKey="id" v-model:expandedRows="expanded" size="small"
               :rowClass="row_class" :loading="loading && !events.length">
      <template #empty>No events{{ filtered ? ' match these filters' : '' }}.</template>
      <Column expander style="width:2.5rem" />
      <Column header="When" style="width:11rem">
        <template #body="{ data }"><span v-tooltip.top="data.ts">{{ when(data.ts) }}</span></template>
      </Column>
      <Column header="Severity" style="width:6.5rem">
        <template #body="{ data }"><Tag :value="data.severity" :severity="SEVERITY_TAG[data.severity]" /></template>
      </Column>
      <Column field="event" header="Event" style="width:13rem" />
      <Column header="Who" style="width:9rem">
        <template #body="{ data }">{{ data.actor || '—' }}</template>
      </Column>
      <Column header="From" style="width:9rem">
        <template #body="{ data }">
          {{ data.ip || '—' }}<Tag v-if="data.source === 'portal'" value="portal" severity="secondary" class="src" />
        </template>
      </Column>
      <Column field="message" header="What" />
      <template #expansion="{ data }">
        <div class="detail">
          <p v-if="data.path"><span class="muted">Request:</span> {{ data.method }} {{ data.path }}</p>
          <p v-if="data.user_id"><span class="muted">User id:</span> {{ data.user_id }}</p>
          <p><span class="muted">Time (UTC):</span> {{ data.ts }} · <span class="muted">id</span> {{ data.id }}</p>
          <pre>{{ details(data) }}</pre>
        </div>
      </template>
    </DataTable>

    <div v-if="next_id" class="more">
      <Button size="small" label="Load more" severity="secondary" :loading="loading" @click="load(true)" />
    </div>
  </div>
</template>

<style scoped>
.tab       { display: flex; flex-direction: column; gap: 1rem; }
.card      { border: 1px solid var(--p-content-border-color); border-radius: 6px; padding: 0.75rem 1rem;
             display: flex; flex-direction: column; gap: 0.5rem; }
.card p    { margin: 0; }
.card-head { display: flex; justify-content: space-between; align-items: center; gap: 1rem; }
.muted     { color: var(--p-text-muted-color); }
.missing   { color: var(--p-red-400); }
.failure   { color: var(--p-red-400); }
.ok        { color: var(--p-green-400); }
.filters   { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; }
.f-event   { width: 16rem; }
.f-sev     { width: 10rem; }
.f-src     { width: 10rem; }
.f-short   { width: 11rem; }
.src       { margin-left: 0.35rem; font-size: 0.7rem; }
.detail    { padding: 0.25rem 0.5rem; }
.detail p  { margin: 0 0 0.25rem; }
.detail pre { margin: 0.5rem 0 0; white-space: pre-wrap; word-break: break-word; font-size: 0.8rem; }
.more      { display: flex; justify-content: center; }
:deep(.alert-row) { background: color-mix(in srgb, var(--p-red-500) 12%, transparent); }
:deep(.high-row)  { background: color-mix(in srgb, var(--p-orange-500) 8%, transparent); }
</style>
