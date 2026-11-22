<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import Button      from 'primevue/button'
import Tag         from 'primevue/tag'
import Message     from 'primevue/message'
import Checkbox    from 'primevue/checkbox'
import RadioButton from 'primevue/radiobutton'
import Select      from 'primevue/select'
import ShiftPicker from '../../components/ShiftPicker.vue'
import { portal_get, portal_post } from '../../composables/usePortal.js'

// The volunteer's own record: status, scheduled shifts, picks, and the answers they can still
// change. Edit mode (POST /api/portal/me/update) changes the two shifts, extras, and the
// questions marked `editable` in portal/signup_fields.py, while pending or active — open or
// closed signup alike. Dropping a shift they're scheduled for is allowed; the organizers are
// told (a banner in the organizer app), and this page says so before they save.
//
// Cancelling (me/cancel) is undoable: Rejoin reuses the edit form, posting to me/rejoin, with
// nothing held — their places were given back, so every pick needs space again. Approval
// (status) is separate and untouched by either.

const router = useRouter()
const me     = ref(null)
const error  = ref('')

const vol    = computed(() => me.value?.volunteer)
const status = computed(() => {
  if (me.value?.inactive)  return { label: 'Inactive',  severity: 'secondary' }
  if (me.value?.cancelled) return { label: 'Cancelled', severity: 'secondary' }
  if (vol.value?.status === 'active') return { label: 'Approved', severity: 'success' }
  return { label: 'Awaiting approval', severity: 'warn' }
})
const prefs  = computed(() => (me.value?.picks || []).filter(p => p.kind === 'pref'))
const extras = computed(() => (me.value?.picks || []).filter(p => p.kind === 'add'))

// ── editing ───────────────────────────────────────────────────────────────────
const editing = ref(false)
const rejoining = ref(false)   // the edit form, saving to me/rejoin
const busy    = ref(false)
const message = ref('')
const slots   = ref([])
const fields  = ref([])     // the editable ones only
const edit    = reactive({ prefs: [], extras: [], add_any: false, answers: {} })

const key_of   = (p) => `${p.day_key}_${p.time_key}`
const held     = computed(() => rejoining.value ? [] : prefs.value.map(key_of))
const old_keys = computed(() => (me.value?.picks || []).map(key_of))
const scheduled_keys = computed(() => (me.value?.scheduled || []).map(s => s.key))

// Scheduled shifts this edit would drop from their picks: the organizers get flagged.
const dropping_scheduled = computed(() => (me.value?.scheduled || []).filter(s =>
  old_keys.value.includes(s.key) && !edit.prefs.includes(s.key) && !edit.extras.includes(s.key)))

const problem = computed(() => {
  if (edit.prefs.length === 1) return 'Pick a second shift before saving.'
  if (edit.prefs.length !== 2) return 'Pick two shifts.'
  for (const f of fields.value) {
    if (f.required && (f.type === 'select' || f.type === 'choice') && !edit.answers[f.key])
      return `${f.label}: please choose one.`
  }
  return ''
})

function answer_text(f) {
  const v = me.value?.answers?.[f.key]
  if (f.type === 'check') return v ? 'Yes' : 'No'
  return f.options?.find(o => o.value === v)?.label || v || '—'
}

async function load() {
  const [m, f] = await Promise.all([portal_get('me'), portal_get('signup/fields')])
  if (m.status === 401) { router.replace('/apply/login'); return }
  if (m.status === 403) { router.replace('/apply/signup'); return }
  if (!m.ok) { error.value = m.body?.detail || "Couldn't load your details."; return }
  me.value     = m.body
  fields.value = f.ok ? f.body.fields.filter(x => x.editable) : []
}
onMounted(load)

async function start_edit(rejoin = false) {
  rejoining.value = rejoin
  message.value = ''
  const t = await portal_get('timeslots')
  if (!t.ok) { message.value = "Couldn't load the shifts. Try again."; return }
  slots.value = t.body.timeslots
  Object.assign(edit, {
    prefs: prefs.value.map(key_of), extras: extras.value.map(key_of),
    add_any: !!vol.value.add_any,
    answers: Object.fromEntries(fields.value.map(f => [f.key, me.value.answers?.[f.key] ?? null])),
  })
  editing.value = true
}

async function save() {
  if (problem.value) return
  busy.value = true; message.value = ''
  const update = { prefs: edit.prefs, extras: edit.add_any ? [] : edit.extras,
                   add_any: edit.add_any, answers: edit.answers }
  const { ok, status: code, body } = await portal_post(rejoining.value ? 'me/rejoin' : 'me/update',
                                                       { update: JSON.stringify(update) })
  busy.value = false
  if (ok) { me.value = body; editing.value = false; return }
  const detail = body?.detail
  if (code === 409 && detail && typeof detail === 'object' && detail.full) {
    // A new pick filled up while they were choosing.
    const t = await portal_get('timeslots')
    if (t.ok) slots.value = t.body.timeslots
    edit.prefs  = edit.prefs.filter(k => !detail.full.includes(k))
    edit.extras = edit.extras.filter(k => !detail.full.includes(k))
    message.value = detail.message
    return
  }
  message.value = (typeof detail === 'string' && detail) || 'Something went wrong. Try again.'
}

async function cancel_registration() {
  if (!confirm('Cancel your volunteer registration?\n\nYour shifts will be given to other ' +
               'volunteers. You can rejoin later from this page if there is still space.')) return
  busy.value = true; message.value = ''
  const { ok, body } = await portal_post('me/cancel')
  busy.value = false
  if (ok) me.value = body
  else message.value = body?.detail || 'Something went wrong. Try again.'
}

// Plain Sign out is in the header (PortalLayout); this page keeps Sign out everywhere.
async function sign_out(everywhere) {
  await portal_post(everywhere ? 'logout_all' : 'logout')
  router.replace('/apply/')
}
</script>

<template>
  <div class="apply-card" :class="{ wide: editing }">
    <p v-if="error" class="apply-msg err">{{ error }}</p>
    <i v-else-if="!vol" class="pi pi-spin pi-spinner" />

    <!-- ── viewing ────────────────────────────────────────────────────────── -->
    <template v-else-if="!editing">
      <h2>Hi, {{ vol.nickname || vol.first_name || vol.name }}</h2>
      <Tag :value="status.label" :severity="status.severity" />
      <p v-if="me.inactive" class="pending-note">
        Your registration isn't active. Please contact an organizer if you have questions.
      </p>
      <p v-else-if="me.cancelled" class="pending-note">
        You've cancelled your registration. Changed your mind? Rejoin below, if there's still
        space in the shifts you want.
      </p>
      <p v-else-if="vol.status === 'pending'" class="pending-note">
        Thanks for signing up! An organizer will review your signup.
      </p>

      <h3 class="me-head">Your scheduled shifts</h3>
      <ul v-if="me.scheduled.length" class="me-list">
        <li v-for="s in me.scheduled" :key="s.key + s.position">{{ s.label }} — {{ s.position }}</li>
      </ul>
      <p v-else>Your shifts will appear here once the schedule is set.</p>

      <h3 class="me-head">Your availability</h3>
      <ul v-if="prefs.length" class="me-list">
        <li v-for="p in prefs" :key="key_of(p)">{{ p.label }}</li>
      </ul>
      <p v-else>No preferences on file.</p>
      <template v-if="extras.length || vol.add_any">
        <h3 class="me-head">Also available if needed</h3>
        <p v-if="vol.add_any">Any time</p>
        <ul v-else class="me-list">
          <li v-for="p in extras" :key="key_of(p)">{{ p.label }}</li>
        </ul>
      </template>

      <template v-if="fields.length">
        <h3 class="me-head">Your details</h3>
        <dl class="me-details">
          <template v-for="f in fields" :key="f.key">
            <dt>{{ f.type === 'check' ? 'Flexible' : f.label }}</dt><dd>{{ answer_text(f) }}</dd>
          </template>
        </dl>
      </template>

      <div class="me-actions">
        <Button v-if="me.editable" label="Change shifts or details" icon="pi pi-pencil"
                size="small" @click="start_edit(false)" />
        <Button v-if="me.can_rejoin" label="Rejoin" icon="pi pi-replay" size="small"
                @click="start_edit(true)" />
        <Button label="Sign out everywhere" severity="secondary" text size="small"
                @click="sign_out(true)" />
      </div>
      <p v-if="me.editable" class="apply-links">
        Can't volunteer after all?
        <a href="#" @click.prevent="cancel_registration">Cancel my registration</a>
      </p>
      <p v-if="message" class="apply-msg err">{{ message }}</p>
    </template>

    <!-- ── editing ────────────────────────────────────────────────────────── -->
    <template v-else>
      <h2>{{ rejoining ? 'Rejoin' : 'Change your registration' }}</h2>
      <p v-if="rejoining">Pick <strong>two</strong> shifts that don't overlap. Your earlier
         shifts were offered to others when you cancelled, so pick again from what's free.</p>
      <p v-else>Pick <strong>two</strong> shifts that don't overlap. If you drop one, choose
         another before saving.</p>

      <Message v-if="message" severity="warn" :closable="false" class="msg">{{ message }}</Message>

      <ShiftPicker :slots="slots" v-model:prefs="edit.prefs" v-model:extras="edit.extras"
                   v-model:add-any="edit.add_any" :held="held" :scheduled="scheduled_keys"
                   id-prefix="me" />

      <Message v-if="dropping_scheduled.length" severity="warn" :closable="false" class="msg">
        You're scheduled for
        <strong>{{ dropping_scheduled.map(s => `${s.label} (${s.position})`).join(', ') }}</strong>.
        If you drop it, the organizers will be told so they can find someone else. Please only
        do this if you really can't make it.
      </Message>

      <h3 class="me-head">Your details</h3>
      <div v-for="f in fields" :key="f.key" class="field">
        <div v-if="f.type === 'check'" class="check">
          <Checkbox v-model="edit.answers[f.key]" :inputId="`me-${f.key}`" binary />
          <label :for="`me-${f.key}`">{{ f.label }}</label>
        </div>
        <template v-else>
          <label :for="`me-${f.key}`">{{ f.label }}</label>
          <Select v-if="f.type === 'select'" v-model="edit.answers[f.key]" :inputId="`me-${f.key}`"
                  :options="f.options" optionLabel="label" optionValue="value" style="max-width:12rem;" />
          <div v-else-if="f.type === 'choice'" class="choices">
            <div v-for="o in f.options" :key="o.value" class="check">
              <RadioButton v-model="edit.answers[f.key]" :inputId="`me-${f.key}-${o.value}`"
                           :name="`me-${f.key}`" :value="o.value" />
              <label :for="`me-${f.key}-${o.value}`">{{ o.label }}</label>
            </div>
          </div>
        </template>
      </div>

      <p v-if="problem" class="apply-msg err">{{ problem }}</p>
      <div class="me-actions">
        <Button :label="rejoining ? 'Rejoin' : 'Save changes'" :loading="busy"
                :disabled="!!problem" @click="save" />
        <Button label="Cancel" severity="secondary" @click="editing = false; message = ''" />
      </div>
    </template>
  </div>
</template>

<style scoped>
.wide       { max-width: 36rem; }
.me-head    { font-size: 0.95rem; margin: 1.5rem 0 0.5rem; }
.pending-note { margin-top: 1rem; }
.me-list    { margin: 0; padding-left: 1.25rem; line-height: 1.7; }
.me-details { display: grid; grid-template-columns: 8rem 1fr; gap: 0.3rem 0.75rem; margin: 0; }
.me-details dt { color: var(--p-text-muted-color); }
.me-details dd { margin: 0; }
.me-actions { display: flex; gap: 0.5rem; margin-top: 1.5rem; flex-wrap: wrap; }
.msg        { margin: 0.75rem 0; }
.check      { display: flex; gap: 0.5rem; align-items: flex-start; }
.check label { line-height: 1.4; }
.choices    { display: flex; flex-direction: column; gap: 0.45rem; }
</style>
