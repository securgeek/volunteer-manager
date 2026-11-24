<script setup>
import { ref, reactive, computed, watch, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Button      from 'primevue/button'
import InputText   from 'primevue/inputtext'
import Textarea    from 'primevue/textarea'
import RadioButton from 'primevue/radiobutton'
import Checkbox    from 'primevue/checkbox'
import Select      from 'primevue/select'
import Tag         from 'primevue/tag'
import Message     from 'primevue/message'
import ShiftPicker from '../../components/ShiftPicker.vue'
import { portal_get, portal_post, APPLY_EMAIL_KEY, usePortalBranding } from '../../composables/usePortal.js'
import { check, problem as rule_problem, signup_answers, TOKEN, NAME_MAX, HANDLE_MAX } from '../../lib/portal_rules.js'

// Volunteer signup, email first (vol_signup.md §7-§8; portal/routers/signup.py).
//
//   email -> check your inbox -> about you -> questions -> shifts -> review -> done
//
// Nothing is created until the last step: check_email only mails a link, and the click gives
// this browser a *signup session* that unlocks the form. The questions themselves come from
// the portal (signup/fields), so this page never hard-codes them.
//
// The draft is kept in localStorage so the tab the mail link opens picks up whatever was
// typed in the first one. It holds timeslot *keys* (`day_time`), so a relabelled time shows its
// new label and one no longer offered is dropped with a note rather than submitted.
//
// The two picks can't overlap (portal/routers/signup.py); each timeslot lists the keys it
// clashes with, so a clashing choice is greyed out rather than refused at submit.

const DRAFT_KEY = 'bsc_signup_draft'
const POLL_MS   = 5000
const RESEND_COOLDOWN_SEC = 60

const router = useRouter()
const route  = useRoute()
// The organizers' contact address (App Settings), for the "invite link doesn't work" card.
const { branding } = usePortalBranding()

// An early-access invite (?invite=… on the link an organizer sent) lets this one person
// sign up while signup is closed. Kept in sessionStorage, and taken out of the address bar so
// it isn't in history; the portal ties it to the mailed link, so the form step needs nothing.
const INVITE_KEY = 'bsc_apply_invite'
const invite = ref('')
const invite_status = ref('')   // valid | used | expired | revoked | unknown

// loading | closed | invite_gone | full | email | inbox | form | done
const view    = ref('loading')
const step    = ref(1)   // within the form: 1 about you, 2 shifts, 3 additional questions, 4 review
const busy    = ref(false)
const SHIFTS_STEP = 2, ADDITIONAL_STEP = 3
const message = ref('')

const email      = ref(sessionStorage.getItem(APPLY_EMAIL_KEY) || '')
const registered = ref(false)
const cooldown   = ref(0)

const fields = ref([])
const slots  = ref([])

const draft = reactive({ first_name: '', last_name: '', handle: '', answers: {},
                         prefs: [], extras: [], add_any: false })
const name_hint     = ref(false)   // the "have you signed up before?" box
const name_hint_ack = ref(false)

let poll_timer = null
let tick_timer = null

// ── draft ─────────────────────────────────────────────────────────────────────
function load_draft() {
  try {
    const saved = JSON.parse(localStorage.getItem(DRAFT_KEY) || 'null')
    if (saved && typeof saved === 'object') Object.assign(draft, saved, { answers: saved.answers || {} })
  } catch { /* a corrupt draft is just discarded */ }
}
watch(draft, () => {
  if (view.value === 'form') localStorage.setItem(DRAFT_KEY, JSON.stringify(draft))
}, { deep: true })

// Drop picks for timeslots no longer offered, or full. Returns whether any went.
function prune_picks() {
  const known    = new Set(slots.value.map(t => t.key))
  const pickable = new Set(slots.value.filter(t => !t.full).map(t => t.key))
  const before = draft.prefs.length + draft.extras.length
  draft.prefs  = draft.prefs.filter(k => pickable.has(k))
  draft.extras = draft.extras.filter(k => known.has(k) && !draft.prefs.includes(k))
  return draft.prefs.length + draft.extras.length < before
}

// ── loading ───────────────────────────────────────────────────────────────────
async function start() {
  if (route.query.invite) {
    try { sessionStorage.setItem(INVITE_KEY, String(route.query.invite)) } catch { /* private mode */ }
    invite.value = String(route.query.invite)
    router.replace({ query: {} })
  } else {
    try { invite.value = sessionStorage.getItem(INVITE_KEY) || '' } catch { /* private mode */ }
  }
  // A link mangled on the way (wrapped by a mail app, half copied) can't be an invite. It's
  // treated as unknown here rather than sent: the portal records anything else as an anomaly.
  const invite_shape_ok = !invite.value || TOKEN.test(invite.value)
  const [st, sess, inv] = await Promise.all([
    portal_get('signup/status'), portal_get('signup/session'),
    invite.value && invite_shape_ok
      ? portal_get(`signup/invite?t=${encodeURIComponent(invite.value)}`)
      : Promise.resolve(invite.value ? { ok: true, body: { status: 'unknown' } } : null)])
  if (sess.ok && sess.body.volunteer)      { router.replace('/apply/me'); return }
  if (sess.ok && sess.body.signup_session) { await open_form(); return }
  const state = st.ok ? st.body.state : 'closed'
  invite_status.value = inv?.ok ? inv.body.status : ''
  if (state === 'closed' && invite.value) {
    // The invite is the only way in; say plainly when it no longer works.
    view.value = invite_status.value === 'valid' ? 'email' : 'invite_gone'
    return
  }
  view.value = state === 'open' ? 'email' : state
}

const invited = computed(() => invite_status.value === 'valid')

async function open_form() {
  const [f, t] = await Promise.all([portal_get('signup/fields'), portal_get('timeslots')])
  if (!f.ok || !t.ok) { message.value = "Couldn't load the signup form. Try reloading."; return }
  fields.value = f.body.fields
  slots.value  = t.body.timeslots
  load_draft()
  const dropped = prune_picks()
  view.value = 'form'
  if (dropped) {
    step.value    = SHIFTS_STEP
    message.value = 'Some shifts you picked earlier are no longer available. Please check your picks.'
  }
}

onMounted(start)
onUnmounted(() => { clearInterval(poll_timer); clearInterval(tick_timer) })

// ── email ─────────────────────────────────────────────────────────────────────
// The address, by the portal's rules (lib/portal_rules.js); '' when it's fine.
const email_problem = computed(() => rule_problem('email', email.value))
const email_touched = ref(false)
// Only a well-formed invite token is ever sent along.
const invite_to_send = computed(() => TOKEN.test(invite.value) ? invite.value : '')

async function check_email() {
  email_touched.value = true
  if (email_problem.value) return
  email.value = check('email', email.value).clean
  busy.value = true; message.value = ''; registered.value = false
  const { ok, body } = await portal_post('signup/check_email',
                                         { email: email.value, invite: invite_to_send.value })
  busy.value = false
  if (ok && body.registered) {
    registered.value = true
    message.value    = body.detail
    sessionStorage.setItem(APPLY_EMAIL_KEY, email.value.trim())
    return
  }
  if (!ok) { message.value = body?.detail || 'Something went wrong. Try again.'; return }
  sessionStorage.setItem(APPLY_EMAIL_KEY, email.value.trim())
  view.value = 'inbox'
  start_cooldown()
  clearInterval(poll_timer)
  // The link usually opens in a new tab; the session cookie is shared, so this tab notices.
  poll_timer = setInterval(async () => {
    const { ok: polled, body: s } = await portal_get('signup/session')
    if (polled && s.signup_session) { clearInterval(poll_timer); await open_form() }
  }, POLL_MS)
}

function start_cooldown() {
  cooldown.value = RESEND_COOLDOWN_SEC
  clearInterval(tick_timer)
  tick_timer = setInterval(() => { if (--cooldown.value <= 0) clearInterval(tick_timer) }, 1000)
}

async function resend() {
  const { ok, body } = await portal_post('signup/check_email',
                                         { email: email.value, invite: invite_to_send.value })
  message.value = body?.detail || ''
  if (ok && body.sent) start_cooldown()
}

async function different_email() {
  await portal_post('logout')   // drops a signup session, if this browser had one
  clearInterval(poll_timer)
  message.value = ''; name_hint.value = false; name_hint_ack.value = false
  view.value = 'email'
}

// ── form: validation per step ─────────────────────────────────────────────────
// A function, not a const: load_draft replaces draft.answers wholesale.
function ans() { return draft.answers }

function field_problem(f) {
  const v = ans()[f.key]
  if (f.type === 'ack') return v === true ? '' : 'Please check this box.'
  if (f.type === 'check') return ''
  if (f.type === 'choice' || f.type === 'select') {
    if (!v) return f.required ? 'Please choose one.' : ''
    const opt = f.options.find(o => o.value === v)
    if (opt?.detail) {
      const text = ans()[opt.detail.key] || ''
      if (!text.trim()) return `${opt.detail.label} is required.`
      return rule_problem('text', text, { label: opt.detail.label, max: opt.detail.max })
    }
    return ''
  }
  if (f.type === 'text') {
    if (f.required && !(v || '').trim()) return 'Required.'
    return rule_problem('text', v || '', { label: 'This answer', max: f.max })
  }
  return ''
}

// Each question's step comes from its `section` in portal/signup_fields.py.
const SECTION_FOR_STEP = { [SHIFTS_STEP]: 'shifts', [ADDITIONAL_STEP]: 'additional' }
const step_fields = computed(() =>
  fields.value.filter(f => f.section === SECTION_FOR_STEP[step.value]))

// The names, by the portal's rules (lib/portal_rules.js): '' when fine.
const name_problems = computed(() => ({
  first:  rule_problem('name', draft.first_name, { label: 'First name' }),
  last:   rule_problem('name', draft.last_name, { label: 'Last name' }),
  handle: rule_problem('handle', draft.handle, { label: 'Handle' }),
}))
const touched = reactive({ first: false, last: false, handle: false })
const show_name = (k) => (touched[k] || show_problems.value) && name_problems.value[k]

const step_problem = computed(() => {
  if (step.value === 1) {
    const p = name_problems.value
    if (p.first || p.last || p.handle) return p.first || p.last || p.handle
  }
  if (step.value === SHIFTS_STEP && draft.prefs.length !== 2) return 'Pick exactly two shifts.'
  for (const f of step_fields.value) {
    const p = field_problem(f)
    if (p) return `${f.label}: ${p}`
  }
  return ''
})

const show_problems = ref(false)

async function next() {
  show_problems.value = true
  if (step_problem.value) return
  message.value = ''
  if (step.value === 1 && !name_hint_ack.value) {
    busy.value = true
    const { ok, body } = await portal_post('signup/check_name', names_payload())
    busy.value = false
    if (!ok) { message.value = body?.detail || 'Something went wrong. Try again.'; return }
    if (body.possible_match) { name_hint.value = true; return }
  }
  show_problems.value = false
  step.value++
}

function not_me() {
  name_hint.value = false
  name_hint_ack.value = true
  next()
}

function back() { message.value = ''; show_problems.value = false; step.value-- }

// ── shifts ────────────────────────────────────────────────────────────────────
// The picker itself is components/ShiftPicker.vue, shared with editing on ApplyMe.
const slot_label = (key) => slots.value.find(t => t.key === key)?.label || key

// ── submit ────────────────────────────────────────────────────────────────────
// Exactly the fields the portal takes, cleaned the way it cleans them. The draft comes back
// from localStorage and can hold anything an older page put there; none of that is sent.
function names_payload() {
  return { first_name: check('name', draft.first_name).clean,
           last_name:  check('name', draft.last_name).clean,
           handle:     check('handle', draft.handle).clean }
}


async function submit() {
  busy.value = true; message.value = ''
  const payload = { ...names_payload(), answers: signup_answers(fields.value, draft.answers),
                    prefs: [...draft.prefs], extras: draft.add_any ? [] : [...draft.extras],
                    add_any: draft.add_any === true }
  const { ok, status, body } = await portal_post('signup/submit', { signup: JSON.stringify(payload) })
  busy.value = false
  if (ok) {
    localStorage.removeItem(DRAFT_KEY)
    try { sessionStorage.removeItem(INVITE_KEY) } catch { /* private mode */ }
    view.value = 'done'
    return
  }
  const detail = body?.detail
  if (status === 409 && detail && typeof detail === 'object' && detail.full) {
    // A pick filled up (or vanished) while they were filling in the form.
    const t = await portal_get('timeslots')
    if (t.ok) slots.value = t.body.timeslots
    draft.prefs  = draft.prefs.filter(k => !detail.full.includes(k))
    draft.extras = draft.extras.filter(k => !detail.full.includes(k))
    prune_picks()
    step.value    = SHIFTS_STEP
    message.value = detail.message
    return
  }
  if (status === 409 && typeof detail === 'string' && /already signed up/i.test(detail)) {
    localStorage.removeItem(DRAFT_KEY)
    router.replace('/apply/me')
    return
  }
  message.value = (typeof detail === 'string' && detail) || 'Something went wrong. Try again.'
}

const STEPS = ['About you', 'Shifts', 'Additional questions', 'Review']
</script>

<template>
  <!-- ── loading / closed / full ──────────────────────────────────────────── -->
  <div v-if="view === 'loading'" class="apply-card"><i class="pi pi-spin pi-spinner" /></div>

  <div v-else-if="view === 'closed'" class="apply-card">
    <h2>Signups open soon</h2>
    <p>We're not taking new volunteer signups just yet. Check back soon — this is where
       you'll sign up and choose your shifts.</p>
    <p class="apply-links">Already a volunteer? <RouterLink to="/apply/login">Sign in</RouterLink></p>
  </div>

  <div v-else-if="view === 'invite_gone'" class="apply-card">
    <h2>This invite link doesn't work</h2>
    <p v-if="invite_status === 'used'">It has already been used to sign up.</p>
    <p v-else-if="invite_status === 'expired'">It has expired.</p>
    <p v-else>It's no longer valid.</p>
    <p>Signups aren't open to everyone yet. If you were expecting to sign up early, ask the
       organizers for a new link<template v-if="branding.contact_email">:
       <a :href="`mailto:${branding.contact_email}`">{{ branding.contact_email }}</a></template>.</p>
    <p class="apply-links">Already a volunteer? <RouterLink to="/apply/login">Sign in</RouterLink></p>
  </div>

  <div v-else-if="view === 'full'" class="apply-card">
    <h2>All shifts are full</h2>
    <p>Every volunteer shift is currently taken. Thank you for your interest! If you'd still
       like to help, please contact an organizer.</p>
    <p class="apply-links">Already a volunteer? <RouterLink to="/apply/login">Sign in</RouterLink></p>
  </div>

  <!-- ── email ────────────────────────────────────────────────────────────── -->
  <form v-else-if="view === 'email'" class="apply-card" @submit.prevent="check_email">
    <h2>Volunteer sign up</h2>
    <p v-if="invited" class="invited">You've been invited to sign up early.</p>
    <p>Start with your email address. We'll send you a link to confirm it, then you can fill
       in the rest. No password needed.</p>
    <div class="field">
      <label for="signup-email">Email</label>
      <InputText id="signup-email" v-model="email" type="email" autocomplete="email" autofocus
                 :invalid="email_touched && !!email_problem" @blur="email_touched = !!email.trim()" />
      <small v-if="email_touched && email_problem" class="problem">{{ email_problem }}</small>
    </div>
    <Button type="submit" label="Continue" :loading="busy" :disabled="!!email_problem" />
    <p v-if="message" class="apply-msg" :class="{ err: !registered }">
      {{ message }}
      <RouterLink v-if="registered" to="/apply/login">Sign in</RouterLink>
    </p>
  </form>

  <!-- ── inbox ────────────────────────────────────────────────────────────── -->
  <div v-else-if="view === 'inbox'" class="apply-card">
    <h2>Check your email</h2>
    <p>We sent a link to <strong>{{ email }}</strong>. Click it to confirm your address and
       continue signing up. It works once and expires shortly. This page will move on by
       itself once you've clicked it.</p>
    <Button :label="cooldown > 0 ? `Resend in ${cooldown}s` : 'Resend link'"
            severity="secondary" :disabled="cooldown > 0" @click="resend" />
    <p v-if="message" class="apply-msg">{{ message }}</p>
    <p class="apply-links">Wrong address? <a href="#" @click.prevent="different_email">Use a different email</a></p>
  </div>

  <!-- ── done ─────────────────────────────────────────────────────────────── -->
  <div v-else-if="view === 'done'" class="apply-card">
    <h2>You're signed up!</h2>
    <p>Thank you for volunteering. An organizer will review your signup, and we'll email you
       once your shifts are set.</p>
    <Button label="See your details" @click="router.replace('/apply/me')" />
  </div>

  <!-- ── the form ─────────────────────────────────────────────────────────── -->
  <div v-else-if="view === 'form'" class="apply-card wide">
    <h2>Volunteer sign up</h2>
    <ol class="steps">
      <li v-for="(s, i) in STEPS" :key="s" :class="{ on: step === i + 1, done: step > i + 1 }">
        {{ s }}
      </li>
    </ol>

    <Message v-if="message" severity="warn" :closable="false" class="msg">{{ message }}</Message>

    <!-- 1. about you -->
    <template v-if="step === 1">
      <div class="field">
        <label for="su-first">First name</label>
        <InputText id="su-first" v-model="draft.first_name" autocomplete="given-name"
                   :maxlength="NAME_MAX" :invalid="!!show_name('first')" @blur="touched.first = true" />
        <small v-if="show_name('first')" class="problem">{{ name_problems.first }}</small>
      </div>
      <div class="field">
        <label for="su-last">Last name</label>
        <InputText id="su-last" v-model="draft.last_name" autocomplete="family-name"
                   :maxlength="NAME_MAX" :invalid="!!show_name('last')" @blur="touched.last = true" />
        <small v-if="show_name('last')" class="problem">{{ name_problems.last }}</small>
      </div>
      <div class="field">
        <label for="su-handle">Handle <span class="opt">(optional)</span></label>
        <InputText id="su-handle" v-model="draft.handle" :maxlength="HANDLE_MAX"
                   :invalid="!!show_name('handle')" @blur="touched.handle = true" />
        <small v-if="show_name('handle')" class="problem">{{ name_problems.handle }}</small>
      </div>

      <div v-if="name_hint" class="hint">
        <strong>Have you signed up before?</strong>
        <p>Someone with this name is already registered. If that's you, you may have used a
           different email address — sign in with that one, or contact us and we'll sort it
           out. Otherwise, go right ahead.</p>
        <div class="row">
          <Button size="small" severity="secondary" label="Try a different email" @click="different_email" />
          <Button size="small" label="This isn't me — continue" @click="not_me" />
        </div>
      </div>
    </template>

    <!-- 2. shifts: the picker, then the shift questions -->
    <template v-if="step === SHIFTS_STEP">
      <p>Pick <strong>exactly two</strong> shifts. They can't overlap, so two shifts on the same
         day have to be the first and the last.</p>
      <ShiftPicker :slots="slots" v-model:prefs="draft.prefs" v-model:extras="draft.extras"
                   v-model:add-any="draft.add_any" id-prefix="su" />
    </template>

    <!-- 2 and 3: the questions for this step (signup_fields.py `section`) -->
    <template v-if="step_fields.length">
      <div v-for="f in step_fields" :key="f.key" class="field">
        <template v-if="f.type === 'ack' || f.type === 'check'">
          <div class="check">
            <Checkbox v-model="draft.answers[f.key]" :inputId="`su-${f.key}`" binary />
            <label :for="`su-${f.key}`">{{ f.label }}</label>
          </div>
        </template>
        <template v-else>
          <label :for="`su-${f.key}`">
            {{ f.label }} <span v-if="!f.required" class="opt">(optional)</span>
          </label>
          <div v-if="f.type === 'choice'" class="choices">
            <div v-for="o in f.options" :key="o.value">
              <div class="check">
                <RadioButton v-model="draft.answers[f.key]" :inputId="`su-${f.key}-${o.value}`"
                             :name="f.key" :value="o.value" />
                <label :for="`su-${f.key}-${o.value}`">{{ o.label }}</label>
              </div>
              <InputText v-if="o.detail && draft.answers[f.key] === o.value"
                         v-model="draft.answers[o.detail.key]" :placeholder="o.detail.label"
                         :maxlength="o.detail.max" class="detail" />
            </div>
          </div>
          <Select v-else-if="f.type === 'select'" v-model="draft.answers[f.key]"
                  :inputId="`su-${f.key}`" :options="f.options" optionLabel="label"
                  optionValue="value" placeholder="Choose…" style="max-width:12rem;" />
          <Textarea v-else-if="f.type === 'text'" v-model="draft.answers[f.key]"
                    :id="`su-${f.key}`" :maxlength="f.max" rows="2" autoResize />
        </template>
        <small v-if="show_problems && field_problem(f)" class="problem">{{ field_problem(f) }}</small>
      </div>
    </template>

    <!-- 4. review -->
    <template v-if="step === 4">
      <dl class="review">
        <dt>Name</dt>
        <dd>{{ draft.first_name }} {{ draft.last_name }}<template v-if="draft.handle"> ({{ draft.handle }})</template></dd>
        <dt>Shifts</dt>
        <dd>1. {{ slot_label(draft.prefs[0]) }}<br />2. {{ slot_label(draft.prefs[1]) }}</dd>
        <dt>Also available</dt>
        <dd>{{ draft.add_any ? 'Any time' : (draft.extras.map(slot_label).join(', ') || '—') }}</dd>
        <template v-for="f in fields" :key="f.key">
          <dt>{{ f.type === 'ack' ? 'Agreed' : f.type === 'check' ? 'Flexible' : f.label }}</dt>
          <dd v-if="f.type === 'ack'">{{ f.label }}</dd>
          <dd v-else-if="f.type === 'check'">{{ draft.answers[f.key] ? 'Yes' : 'No' }}</dd>
          <dd v-else-if="f.options">
            {{ f.options.find(o => o.value === draft.answers[f.key])?.label || '—' }}
            <template v-for="o in f.options" :key="o.value">
              <template v-if="o.detail && draft.answers[f.key] === o.value"> — {{ draft.answers[o.detail.key] }}</template>
            </template>
          </dd>
          <dd v-else>{{ draft.answers[f.key] || '—' }}</dd>
        </template>
      </dl>
    </template>

    <p v-if="show_problems && step_problem" class="apply-msg err">{{ step_problem }}</p>

    <div class="nav">
      <Button v-if="step > 1" label="Back" severity="secondary" @click="back" />
      <span class="spacer" />
      <Button v-if="step < 4 && !name_hint" label="Next" :loading="busy" @click="next" />
      <Button v-if="step === 4" label="Submit signup" :loading="busy" @click="submit" />
    </div>
    <p class="apply-links">Not you? <a href="#" @click.prevent="different_email">Use a different email</a></p>
  </div>
</template>

<style scoped>
.wide      { max-width: 36rem; }
.steps     { display: flex; gap: 0.75rem; flex-wrap: wrap; list-style: none; padding: 0;
             margin: 0.5rem 0 1rem; font-size: 0.82rem; color: var(--p-text-muted-color); }
.steps li.on   { color: var(--p-primary-color); font-weight: 600; }
.steps li.done { text-decoration: line-through; }
.msg       { margin-bottom: 0.75rem; }
.opt       { color: var(--p-text-muted-color); font-weight: normal; font-size: 0.85em; }
.check     { display: flex; gap: 0.5rem; align-items: flex-start; }
.check label { line-height: 1.4; }
.choices   { display: flex; flex-direction: column; gap: 0.5rem; }
.detail    { margin: 0.4rem 0 0 1.75rem; max-width: 20rem; }
.problem   { color: var(--p-red-400); }
.hint      { border: 1px solid var(--p-content-border-color); border-radius: 6px;
             padding: 0.9rem; margin-top: 1rem; }
.hint p    { margin: 0.4rem 0 0.75rem; }
.row       { display: flex; gap: 0.5rem; flex-wrap: wrap; }
.review    { display: grid; grid-template-columns: 9rem 1fr; gap: 0.5rem 1rem; margin: 0; }
.review dt { color: var(--p-text-muted-color); font-size: 0.85rem; }
.review dd { margin: 0; }
.nav       { display: flex; gap: 0.5rem; margin-top: 1.5rem; }
.spacer    { flex: 1; }
@media (max-width: 30rem) { .review { grid-template-columns: 1fr; } .review dt { margin-top: 0.5rem; } }
.invited   { color: var(--p-primary-color); font-weight: 600; }
</style>
