<script setup>
import { ref, computed, watch } from 'vue'
import Dialog  from 'primevue/dialog'
import Button  from 'primevue/button'
import Message from 'primevue/message'
import Tag     from 'primevue/tag'
import ToggleSwitch from 'primevue/toggleswitch'
import InputText from 'primevue/inputtext'
import SignupInvites from './SignupInvites.vue'
import Tabs     from 'primevue/tabs'
import TabList  from 'primevue/tablist'
import Tab      from 'primevue/tab'
import TabPanels from 'primevue/tabpanels'
import TabPanel from 'primevue/tabpanel'
import LandingEditor       from './portal-content/LandingEditor.vue'
import QuestionnaireEditor from './portal-content/QuestionnaireEditor.vue'
import InviteEditor        from './portal-content/InviteEditor.vue'
import { usePortalContent } from '../composables/usePortalContent.js'

// The Email Key dialog lives in Admin.vue; this asks it to open.
const emit = defineEmits(['open-email-key'])

const visible   = ref(false)
// Status, or one of the three content editors. Each editor mounts when its tab is first shown
// and then stays, so switching tabs keeps unsaved edits. Opening the dialog starts afresh.
const tab       = ref('status')
const seen      = ref(new Set(['status']))
watch(tab, t => { seen.value = new Set([...seen.value, t]) })
const content   = usePortalContent()
const loading   = ref(false)
const syncing   = ref(false)
const error_msg = ref('')
const status    = ref(null)
// What volunteers see on the signup form: the portal's own timeslots and remaining counts
// (overage included), not our copy of them.
const portal_slots = ref([])
const overage_in   = ref('')
const saving_overage = ref(false)

async function load() {
  loading.value = true
  error_msg.value = ''
  try {
    const [resp, pt] = await Promise.all([fetch('/api/admin/portal/status'),
                                          fetch('/api/portal/timeslots')])
    if (pt.ok) portal_slots.value = (await pt.json()).timeslots
    if (resp.ok) {
      status.value = await resp.json()
      overage_in.value = String(status.value.overage_pct ?? 0)
    } else {
      const body = await resp.json().catch(() => null)
      error_msg.value = body?.detail || 'Failed to read portal status.'
    }
  } catch {
    error_msg.value = 'Unable to reach the server.'
  } finally {
    loading.value = false
  }
}

function open() {
  tab.value = 'status'
  seen.value = new Set(['status'])
  content.data.value = null       // the editors fetch it again, with current answer counts
  status.value = null
  error_msg.value = ''
  load()
  visible.value = true
}
defineExpose({ open })

async function sync_now() {
  syncing.value = true
  error_msg.value = ''
  try {
    const resp = await fetch('/api/admin/portal/sync_now', { method: 'POST' })
    if (!resp.ok) {
      const body = await resp.json().catch(() => null)
      error_msg.value = body?.detail || 'Sync failed.'
    }
    await load()
  } catch {
    error_msg.value = 'Unable to reach the server.'
  } finally {
    syncing.value = false
  }
}

// ── signup open / closed ─────────────────────────────────────────────────────
const toggling = ref(false)

// Why signup can't actually take anyone, even with the switch on. The portal checks the same
// things (routers/signup.py signup_state); this just says so before anyone tries.
const signup_blockers = computed(() => {
  const out = []
  if (!status.value?.has_pubkey)
    out.push({ text: 'Upload an email public key.', action: 'Email Key', run: () => emit('open-email-key') })
  if ((status.value?.open_timeslots || 0) < 2)
    out.push({ text: 'Fewer than two shifts have space. Add slots in Edit Schedule, or raise the overage.' })
  return out
})

const slots_by_day = computed(() => {
  const days = []
  for (const t of portal_slots.value) {
    let d = days.find(x => x.day_key === t.day_key)
    if (!d) days.push(d = { day_key: t.day_key, day_str: t.day_str, slots: [] })
    d.slots.push(t)
  }
  return days
})

// Overage changes the remaining counts, which travel in the schedule snapshot; syncing now
// shows the result here straight away instead of after the next tick.
async function save_overage() {
  saving_overage.value = true
  error_msg.value      = ''
  try {
    const fd = new FormData()
    fd.append('overage_pct', overage_in.value.trim())
    const resp = await fetch('/api/admin/portal/settings', { method: 'POST', body: fd })
    if (!resp.ok) {
      const body = await resp.json().catch(() => null)
      error_msg.value = body?.detail || 'Failed to save the overage.'
    } else {
      await fetch('/api/admin/portal/sync_now', { method: 'POST' }).catch(() => {})
    }
    await load()
  } catch {
    error_msg.value = 'Unable to reach the server.'
  } finally {
    saving_overage.value = false
  }
}

async function set_signup_open(open) {
  toggling.value  = true
  error_msg.value = ''
  try {
    const fd = new FormData()
    fd.append('signup_open', open ? '1' : '0')
    const resp = await fetch('/api/admin/portal/settings', { method: 'POST', body: fd })
    if (!resp.ok) {
      const body = await resp.json().catch(() => null)
      error_msg.value = body?.detail || 'Failed to change signup.'
    }
    await load()
  } catch {
    error_msg.value = 'Unable to reach the server.'
  } finally {
    toggling.value = false
  }
}

// ── derived state ────────────────────────────────────────────────────────────
// A dead sync is the failure mode that hurts most: a volunteer signs up, no organizer ever
// sees them, and nobody finds out until the event. So "stale" is surfaced as loudly as an
// outright error rather than being left for someone to infer from a timestamp.
const STALE_MULTIPLIER = 3

const configured = computed(() => status.value?.configured === true)
// Named by the backend, so the hint matches what compose actually passes through.
const missing    = computed(() => status.value?.missing?.length
  ? status.value.missing : ['PORTAL_SYNC_URL', 'SYNC_KEY_ID', 'SYNC_SECRET'])

const streams = computed(() => status.value?.streams || [])

const last_ok = computed(() => {
  const times = streams.value.map(s => s.last_ok_at).filter(Boolean).sort()
  return times.length ? times[times.length - 1] : null
})

const seconds_since_ok = computed(() => {
  if (!last_ok.value) return null
  const then = new Date(last_ok.value.endsWith('Z') ? last_ok.value : last_ok.value + 'Z')
  return Math.max(0, Math.round((Date.now() - then.getTime()) / 1000))
})

const is_stale = computed(() => {
  const age = seconds_since_ok.value
  if (age === null) return true
  return age > (status.value?.interval_sec || 30) * STALE_MULTIPLIER
})

const errors = computed(() => streams.value.filter(s => s.last_error))

const key_mismatch = computed(() => {
  const run = status.value?.last_run
  return run && run.hmac_key_match === false
})

const overall = computed(() => {
  if (!configured.value) return { severity: 'secondary', label: 'Not configured' }
  if (key_mismatch.value) return { severity: 'danger',  label: 'Key mismatch' }
  if (errors.value.length) return { severity: 'danger',  label: 'Error' }
  if (is_stale.value)      return { severity: 'warn',    label: 'Stale' }
  return { severity: 'success', label: 'Healthy' }
})

function ago(iso) {
  if (!iso) return 'never'
  const then = new Date(iso.endsWith('Z') ? iso : iso + 'Z')
  const secs = Math.max(0, Math.round((Date.now() - then.getTime()) / 1000))
  if (secs < 60)   return `${secs}s ago`
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`
  return `${Math.round(secs / 3600)}h ago`
}
</script>

<template>
  <Dialog v-model:visible="visible" header="Volunteer Portal"
          :style="{ width: '58rem', maxWidth: '96vw' }" modal>
   <Tabs v-model:value="tab">
    <TabList>
      <Tab value="status">Status</Tab>
      <Tab value="landing">Landing page</Tab>
      <Tab value="questionnaire">Questionnaire</Tab>
      <Tab value="invite">Invite email</Tab>
    </TabList>
    <TabPanels>
     <TabPanel value="status">
    <div class="section">
      <div class="hdr">
        <Tag :severity="overall.severity" :value="overall.label" />
        <span v-if="status?.base_url" class="url">{{ status.base_url }}</span>
        <span class="spacer" />
        <Button size="small" severity="secondary" icon="pi pi-refresh" label="Reload"
                :loading="loading" @click="load" />
        <Button size="small" icon="pi pi-sync" label="Sync now"
                :disabled="!configured" :loading="syncing" @click="sync_now" />
      </div>

      <Message v-if="error_msg" severity="error" :closable="false">{{ error_msg }}</Message>

      <Message v-if="!loading && !configured" severity="info" :closable="false">
        Portal sync is off. Set
        <template v-for="(name, i) in missing" :key="name"><code>{{ name }}</code>{{
          i < missing.length - 2 ? ', ' : i === missing.length - 2 ? ' and ' : '' }}</template>
        in <code>.env</code> and recreate the backend to enable it.
      </Message>

      <Message v-if="key_mismatch" severity="error" :closable="false">
        <strong>The portal's email HMAC key does not match ours.</strong>
        Volunteers added here will get a lookup hash the portal cannot match, so they will not be
        able to sign in. Both services must be given the same <code>EMAIL_HMAC_KEY</code>.
      </Message>

      <Message v-else-if="configured && is_stale" severity="warn" :closable="false">
        No successful sync {{ last_ok ? ago(last_ok) : 'on record' }}. New signups will not appear
        here until this recovers.
      </Message>

      <div v-if="configured && status" class="signup">
        <ToggleSwitch :modelValue="status.signup_open" inputId="signup-open"
                      :disabled="toggling || (!status.signup_open && !status.has_pubkey)"
                      @update:modelValue="set_signup_open" />
        <label for="signup-open"><strong>Volunteer signup {{ status.signup_open ? 'open' : 'closed' }}</strong></label>
        <span v-if="status.pending_count" class="pending">
          {{ status.pending_count }} awaiting approval
        </span>
      </div>
      <Message v-if="configured && status && signup_blockers.length" severity="info" :closable="false">
        <div>Before signup can take anyone:</div>
        <div v-for="b in signup_blockers" :key="b.text" class="blocker">
          {{ b.text }}
          <Button v-if="b.action" size="small" variant="text" :label="b.action" @click="b.run" />
        </div>
      </Message>

      <SignupInvites v-if="configured && status" />

      <div v-if="configured" class="slots">
        <div class="hdr">
          <strong>Shifts</strong>
          <span class="muted">as volunteers see them</span>
          <span class="spacer" />
          <label for="overage" class="muted">Overbook by</label>
          <InputText id="overage" v-model="overage_in" size="small" inputmode="numeric"
                     style="width:4rem;" @keyup.enter="save_overage" />
          <span class="muted">%</span>
          <Button size="small" severity="secondary" label="Save" :loading="saving_overage"
                  :disabled="overage_in.trim() === String(status?.overage_pct ?? 0)"
                  @click="save_overage" />
        </div>
        <div class="muted">
          Each shift offers its slots plus this percentage, to allow for volunteers who drop out
          before the event. Admin → Shift Demand shows the real numbers.
        </div>
        <div v-if="!portal_slots.length" class="none">
          No shifts yet: the schedule has no timeslots.
        </div>
        <div v-for="d in slots_by_day" :key="d.day_key" class="slot-day">
          <span class="day">{{ d.day_str }}</span>
          <span v-for="t in d.slots" :key="t.key" class="slot">
            {{ t.time_str }}
            <Tag v-if="t.full" severity="warn" value="Full" />
            <span v-else class="muted">{{ t.remaining }} left</span>
          </span>
        </div>
      </div>

      <table v-if="configured && streams.length" class="streams">
        <thead>
          <tr><th>Stream</th><th>Cursor</th><th>Last OK</th><th>Last error</th></tr>
          <!-- Cursor is em-dash for push streams: they have no cursor, rather than one at 0. -->
        </thead>
        <tbody>
          <tr v-for="s in streams" :key="s.stream">
            <td class="mono">{{ s.stream }}</td>
            <td class="mono num">{{ s.last_rev ?? '—' }}</td>
            <td>{{ ago(s.last_ok_at) }}</td>
            <td class="err">{{ s.last_error || '—' }}</td>
          </tr>
        </tbody>
      </table>

      <div v-if="configured && status?.volunteer_counts" class="counts">
        <span v-for="(n, k) in status.volunteer_counts" :key="k" class="count">
          <strong>{{ n }}</strong> {{ k }}
        </span>
        <span v-if="!Object.keys(status.volunteer_counts).length" class="none">
          No volunteers yet.
        </span>
      </div>

      <div v-if="configured" class="hint">
        Polls every {{ status?.interval_sec }}s, and pushes immediately after a schedule change.
        Volunteer data is pulled from the portal; the schedule, branding and settings are pushed to it.
      </div>
    </div>
     </TabPanel>
     <TabPanel value="landing"><LandingEditor v-if="seen.has('landing')" /></TabPanel>
     <TabPanel value="questionnaire">
       <QuestionnaireEditor v-if="seen.has('questionnaire')" />
     </TabPanel>
     <TabPanel value="invite"><InviteEditor v-if="seen.has('invite')" /></TabPanel>
    </TabPanels>
   </Tabs>
  </Dialog>

</template>

<style scoped>
.section { padding: 0.5rem 0; display: flex; flex-direction: column; gap: 0.85rem; }
.hdr     { display: flex; align-items: center; gap: 0.75rem; }
.spacer  { flex: 1; }
.url     { font-family: monospace; font-size: 0.78rem; color: var(--p-text-muted-color);
           overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 16rem; }
.streams { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
.streams th, .streams td { text-align: left; padding: 0.35rem 0.5rem;
                           border-bottom: 1px solid var(--p-content-border-color); }
.streams th { font-weight: 600; color: var(--p-text-muted-color); font-size: 0.75rem;
              text-transform: uppercase; letter-spacing: 0.03em; }
.mono    { font-family: monospace; }
.num     { text-align: right; }
.err     { color: var(--p-red-400); max-width: 14rem; overflow: hidden;
           text-overflow: ellipsis; white-space: nowrap; }
.counts  { display: flex; gap: 1rem; flex-wrap: wrap; font-size: 0.85rem; }
.signup  { display: flex; align-items: center; gap: 0.6rem; }
.blocker { display: flex; align-items: center; gap: 0.25rem; }
.slots   { display: flex; flex-direction: column; gap: 0.4rem; font-size: 0.85rem; }
.muted   { color: var(--p-text-muted-color); font-size: 0.8rem; }
.slot-day { display: flex; gap: 0.75rem; flex-wrap: wrap; align-items: baseline; }
.day     { font-weight: 600; min-width: 5.5rem; }
.slot    { display: inline-flex; align-items: center; gap: 0.35rem; }
.pending { font-size: 0.82rem; color: var(--p-text-muted-color); }
.count strong { font-size: 1.05rem; }
.none    { color: var(--p-text-muted-color); font-style: italic; }
.hint    { font-size: 0.78rem; color: var(--p-text-muted-color); line-height: 1.5; }
.hint code, .section code {
  background: var(--p-content-background); border: 1px solid var(--p-content-border-color);
  border-radius: 3px; padding: 0.05rem 0.3rem; font-size: 0.74rem;
}
</style>
