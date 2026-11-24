<script setup>
import { ref, reactive, computed } from 'vue'
import Dialog    from 'primevue/dialog'
import Button    from 'primevue/button'
import InputText from 'primevue/inputtext'
import Password  from 'primevue/password'
import Select    from 'primevue/select'
import Message   from 'primevue/message'
import Checkbox  from 'primevue/checkbox'

const ROLES = [
  { label: 'Organizer', value: 'organizer' },
  { label: 'Check-in',  value: 'checkin' },
  { label: 'View',      value: 'view' },
]

const visible = ref(false)
const users   = ref([])
const err     = ref('')

const current_username = localStorage.getItem('bsc_username') || ''

// ── add-user form ─────────────────────────────────────────────────────────────
const show_add   = ref(false)
const add_saving = ref(false)
const add_err    = ref('')
const add_form   = reactive({ username: '', password: '', pw_confirm: '', role: 'view' })

// ── inline email edit ─────────────────────────────────────────────────────────
// Where that user's "Forgot password?" link goes. Organizers can set passwords outright, so
// setting someone's address grants nothing new.
const email_uid    = ref(null)
const email_val    = ref('')
const email_err    = ref('')
const email_saving = ref(false)

// ── invites ───────────────────────────────────────────────────────────────────
// A link the invitee uses to make their own account (backend routers/invites.py). The link
// is shown once, here, right after creation; the server keeps only its hash.
const show_invite   = ref(false)
const invite_form   = reactive({ role: 'view', email: '' })
const invite_saving = ref(false)
const invite_err    = ref('')
const invite_new    = ref(null)    // { link, expires_at, emailed, email_error, email }
const invite_copied = ref(false)
const invites       = ref([])

// ── inline reset-password state ───────────────────────────────────────────────
const reset_uid    = ref(null)
const reset_pw     = ref('')
const reset_pw2    = ref('')
const reset_err    = ref('')
const reset_saving = ref(false)

async function load() {
  err.value = ''
  try {
    const resp = await fetch('/api/users')
    if (resp.ok) users.value = await resp.json()
    else err.value = 'Failed to load users.'
  } catch { err.value = 'Failed to load users.' }
  load_invites()
}

async function load_invites() {
  try {
    const resp = await fetch('/api/invites')
    if (resp.ok) invites.value = await resp.json()
  } catch { /* the list just stays as it was */ }
}

function open() {
  err.value     = ''
  add_err.value = ''
  show_add.value  = false
  reset_uid.value = null
  email_uid.value = null
  show_invite.value = false
  invite_new.value  = null
  invite_err.value  = ''
  Object.assign(invite_form, { role: 'view', email: '' })
  Object.assign(add_form, { username: '', password: '', pw_confirm: '', role: 'view' })
  load()
  visible.value = true
}
defineExpose({ open })

function fmt_date(iso) {
  if (!iso) return 'Never'
  return new Date(iso.includes('T') ? iso : iso + 'Z').toLocaleString()
}

function is_self(user) {
  return user.username === current_username
}

// ── security alerts ───────────────────────────────────────────────────────────
// Who gets security alert emails (backend alerts.py; every organizer can read the Audit Log
// regardless). Organizers only, and any organizer can change anyone's. It may be nobody,
// which the warning above the list says, as the Audit Log does.
const alerts_reach_nobody = computed(() => users.value.length > 0 &&
  !users.value.some(u => u.audit_alerts && u.role === 'organizer' && u.email))

async function set_audit_alerts(user) {
  err.value = ''
  const fd = new FormData()
  fd.append('user_id', user.user_id)
  fd.append('enabled', user.audit_alerts ? '1' : '0')
  const resp = await fetch('/api/users/set_audit_alerts', { method: 'POST', body: fd })
  if (!resp.ok) {
    const body = await resp.json().catch(() => null)
    err.value = body?.detail || 'Failed to change security alerts.'
  }
  load()
}

// ── role change ───────────────────────────────────────────────────────────────
async function set_role(user) {
  err.value = ''
  const fd = new FormData()
  fd.append('user_id', user.user_id)
  fd.append('role', user.role)
  const resp = await fetch('/api/users/set_role', { method: 'POST', body: fd })
  if (!resp.ok) {
    const body = await resp.json().catch(() => null)
    err.value = body?.detail || 'Failed to update role.'
    load()
  }
}

// ── password reset ────────────────────────────────────────────────────────────
function toggle_reset(user_id) {
  if (reset_uid.value === user_id) {
    reset_uid.value = null
  } else {
    reset_uid.value  = user_id
    reset_pw.value   = ''
    reset_pw2.value  = ''
    reset_err.value  = ''
  }
}

async function do_reset() {
  reset_err.value = ''
  if (reset_pw.value.length < 8) { reset_err.value = 'Must be at least 8 characters.'; return }
  if (reset_pw.value !== reset_pw2.value) { reset_err.value = 'Passwords do not match.'; return }
  reset_saving.value = true
  try {
    const fd = new FormData()
    fd.append('user_id', reset_uid.value)
    fd.append('password', reset_pw.value)
    const resp = await fetch('/api/users/set_password', { method: 'POST', body: fd })
    if (resp.ok) {
      reset_uid.value = null
    } else {
      const body = await resp.json().catch(() => null)
      reset_err.value = body?.detail || 'Failed to reset password.'
    }
  } finally { reset_saving.value = false }
}

// ── email ─────────────────────────────────────────────────────────────────────
function toggle_email(user) {
  if (email_uid.value === user.user_id) { email_uid.value = null; return }
  email_uid.value = user.user_id
  email_val.value = user.email || ''
  email_err.value = ''
}

async function save_email(user) {
  email_err.value = ''
  email_saving.value = true
  try {
    const fd = new FormData()
    fd.append('user_id', user.user_id)
    fd.append('email', email_val.value.trim())
    const resp = await fetch('/api/users/set_email', { method: 'POST', body: fd })
    const body = await resp.json().catch(() => null)
    if (resp.ok) {
      user.email = body.email
      email_uid.value = null
    } else {
      email_err.value = body?.detail || 'Failed to save the email.'
    }
  } catch { email_err.value = 'Unable to reach the server.' }
  finally { email_saving.value = false }
}

// ── invites ───────────────────────────────────────────────────────────────────
async function create_invite() {
  invite_err.value = ''
  invite_new.value = null
  invite_copied.value = false
  invite_saving.value = true
  try {
    const fd = new FormData()
    fd.append('role', invite_form.role)
    fd.append('email', invite_form.email.trim())
    const resp = await fetch('/api/invites', { method: 'POST', body: fd })
    const body = await resp.json().catch(() => null)
    if (resp.ok) {
      invite_new.value = { ...body, email: invite_form.email.trim() }
      invite_form.email = ''
      load_invites()
    } else {
      invite_err.value = body?.detail || 'Failed to create the invite.'
    }
  } catch { invite_err.value = 'Unable to reach the server.' }
  finally { invite_saving.value = false }
}

async function copy_invite() {
  try {
    await navigator.clipboard.writeText(invite_new.value.link)
    invite_copied.value = true
  } catch { invite_err.value = 'Copy failed; select the link and copy it by hand.' }
}

async function revoke_invite(inv) {
  if (!confirm(`Revoke this ${inv.role} invite${inv.email ? ` for ${inv.email}` : ''}?`)) return
  const fd = new FormData()
  fd.append('invite_id', inv.invite_id)
  const resp = await fetch('/api/invites/revoke', { method: 'POST', body: fd })
  if (!resp.ok) {
    const body = await resp.json().catch(() => null)
    err.value = body?.detail || 'Failed to revoke the invite.'
  }
  load_invites()
}

function role_label(value) {
  return ROLES.find(r => r.value === value)?.label || value
}

// ── delete ────────────────────────────────────────────────────────────────────
async function del_user(user) {
  if (!confirm(`Delete user "${user.username}"? This cannot be undone.`)) return
  const fd = new FormData()
  fd.append('user_id', user.user_id)
  const resp = await fetch('/api/users/delete', { method: 'POST', body: fd })
  if (resp.ok) {
    users.value = users.value.filter(u => u.user_id !== user.user_id)
  } else {
    const body = await resp.json().catch(() => null)
    err.value = body?.detail || 'Failed to delete user.'
  }
}

// ── passkeys ──────────────────────────────────────────────────────────────────
// For someone who lost a device. Their own passkeys are managed in MyPasskeys.
async function clear_passkeys(user) {
  const n = user.passkey_count
  if (!confirm(`Remove ${n} passkey${n === 1 ? '' : 's'} from "${user.username}"? ` +
               `They can still sign in with their password and add new ones.`)) return
  const fd = new FormData()
  fd.append('user_id', user.user_id)
  const resp = await fetch('/api/passkeys/clear_user', { method: 'POST', body: fd })
  if (resp.ok) {
    user.passkey_count = 0
  } else {
    const body = await resp.json().catch(() => null)
    err.value = body?.detail || 'Failed to remove passkeys.'
  }
}

// ── add user ──────────────────────────────────────────────────────────────────
async function add_user() {
  add_err.value = ''
  if (!add_form.username.trim() || !add_form.password) {
    add_err.value = 'Username and password are required.'; return
  }
  if (add_form.password.length < 8) {
    add_err.value = 'Password must be at least 8 characters.'; return
  }
  if (add_form.password !== add_form.pw_confirm) {
    add_err.value = 'Passwords do not match.'; return
  }
  add_saving.value = true
  try {
    const fd = new FormData()
    fd.append('username',   add_form.username.trim())
    fd.append('password',   add_form.password)
    fd.append('role',       add_form.role)
    const resp = await fetch('/api/users/add', { method: 'POST', body: fd })
    if (resp.ok) {
      users.value.push(await resp.json())
      Object.assign(add_form, { username: '', password: '', pw_confirm: '', role: 'view' })
      show_add.value = false
    } else {
      const body = await resp.json().catch(() => null)
      add_err.value = body?.detail || 'Failed to add user.'
    }
  } finally { add_saving.value = false }
}
</script>

<template>
  <Dialog v-model:visible="visible" header="User Management"
          :style="{ width: '72rem', maxWidth: '95vw' }" modal>

    <Message v-if="err" severity="error" :closable="false" style="margin-bottom:0.75rem;">
      {{ err }}
    </Message>
    <Message v-if="alerts_reach_nobody" severity="warn" :closable="false" style="margin-bottom:0.75rem;">
      Security alerts aren't emailed to anyone. Tick <strong>Security alerts</strong> for an
      organizer who has an email address.
    </Message>

    <!-- ── user list ─────────────────────────────────────────────────────────── -->
    <div class="user-list">
      <!-- header row -->
      <div class="user-row header-row">
        <span>Username</span>
        <span>Role</span>
        <span>Email</span>
        <span>Last login</span>
        <span>Failed logins</span>
        <span>Passkeys</span>
        <span v-tooltip.top="'Gets security alert emails (needs an email address)'">Security alerts</span>
        <span></span>
      </div>

      <div v-for="user in users" :key="user.user_id" class="user-block">
        <div class="user-row">
          <span class="uname" :class="{ self: is_self(user) }">
            {{ user.username }}
            <span v-if="is_self(user)" class="you-badge">you</span>
          </span>
          <Select v-model="user.role" :options="ROLES"
                  option-value="value" option-label="label"
                  size="small" :disabled="is_self(user)"
                  @change="set_role(user)"
                  style="width:9rem;" />
          <span class="email" :class="{ muted: !user.email }">
            <span class="email-text">{{ user.email || '—' }}</span>
            <Button size="small" icon="pi pi-pencil" severity="secondary" variant="text"
                    v-tooltip.top="'Edit email'"
                    :class="{ 'active-action': email_uid === user.user_id }"
                    @click="toggle_email(user)" />
          </span>
          <span class="muted">{{ fmt_date(user.last_login_at) }}</span>
          <span class="muted center">
            <span v-if="user.failed_login_cnt > 0" class="warn-badge">
              {{ user.failed_login_cnt }}
            </span>
            <span v-else>—</span>
          </span>
          <span class="muted center">{{ user.passkey_count || '—' }}</span>
          <span class="center">
            <Checkbox v-model="user.audit_alerts" binary
                      :disabled="user.role !== 'organizer' && !user.audit_alerts"
                      :aria-label="`Security alert emails for ${user.username}`"
                      v-tooltip.top="user.role !== 'organizer' ? 'Organizers only'
                                     : user.audit_alerts && !user.email ? 'No email address: alerts can\'t reach them' : ''"
                      @change="set_audit_alerts(user)" />
          </span>
          <div class="actions">
            <Button size="small" icon="pi pi-key" severity="secondary" variant="text"
                    v-tooltip.top="'Reset password'"
                    :disabled="is_self(user)"
                    :class="{ 'active-action': reset_uid === user.user_id }"
                    @click="toggle_reset(user.user_id)" />
            <Button size="small" icon="pi pi-shield" severity="secondary" variant="text"
                    v-tooltip.top="'Remove passkeys'"
                    :disabled="is_self(user) || !user.passkey_count"
                    @click="clear_passkeys(user)" />
            <Button size="small" icon="pi pi-trash" severity="danger" variant="text"
                    v-tooltip.top="'Delete user'"
                    :disabled="is_self(user)"
                    @click="del_user(user)" />
          </div>
        </div>

        <!-- inline email edit -->
        <div v-if="email_uid === user.user_id" class="reset-form">
          <InputText v-model="email_val" type="email" placeholder="Email (blank to clear)"
                     size="small" style="width:18rem;" @keyup.enter="save_email(user)" />
          <Button size="small" label="Save Email" :loading="email_saving" @click="save_email(user)" />
          <Button size="small" label="Cancel" severity="secondary" @click="email_uid = null" />
          <span v-if="email_err" class="reset-err">{{ email_err }}</span>
        </div>

        <!-- inline password reset -->
        <div v-if="reset_uid === user.user_id" class="reset-form">
          <Password v-model="reset_pw"  placeholder="New password" :feedback="true"
                    size="small" style="width:13rem;" />
          <Password v-model="reset_pw2" placeholder="Confirm"      :feedback="false"
                    size="small" style="width:10rem;" />
          <Button size="small" label="Set Password" :loading="reset_saving" @click="do_reset" />
          <Button size="small" label="Cancel" severity="secondary" @click="reset_uid = null" />
          <span v-if="reset_err" class="reset-err">{{ reset_err }}</span>
        </div>
      </div>
    </div>

    <!-- ── invite section ─────────────────────────────────────────────────────── -->
    <div class="add-section">
      <Button size="small" :icon="show_invite ? 'pi pi-minus' : 'pi pi-envelope'"
              :label="show_invite ? 'Close' : 'Invite Someone'"
              severity="secondary"
              @click="show_invite = !show_invite; invite_err = ''; invite_new = null" />

      <div v-if="show_invite" class="add-form">
        <p class="muted" style="margin:0 0 0.5rem;">
          They choose their own username and password. The link works once, for 72 hours.
          With an email address it's sent to them; either way you can copy it below.
        </p>
        <Message v-if="invite_err" severity="error" :closable="false" style="margin-bottom:0.5rem;">
          {{ invite_err }}
        </Message>
        <div class="add-grid">
          <Select    v-model="invite_form.role" :options="ROLES"
                     option-value="value" option-label="label"
                     size="small" style="width:9rem;" />
          <InputText v-model="invite_form.email" type="email" placeholder="Email (optional)"
                     size="small" style="width:18rem;" @keyup.enter="create_invite" />
          <Button size="small" label="Create Invite" :loading="invite_saving" @click="create_invite" />
        </div>

        <div v-if="invite_new" class="invite-new">
          <Message v-if="invite_new.emailed" severity="success" :closable="false">
            Sent to {{ invite_new.email }}.</Message>
          <Message v-else-if="invite_new.email_error" severity="warn" :closable="false">
            {{ invite_new.email_error }}</Message>
          <div class="invite-link">
            <InputText :model-value="invite_new.link" readonly size="small" fluid
                       @focus="$event.target.select()" />
            <Button size="small" :icon="invite_copied ? 'pi pi-check' : 'pi pi-copy'"
                    :label="invite_copied ? 'Copied' : 'Copy'" severity="secondary"
                    @click="copy_invite" />
          </div>
          <p class="muted" style="margin:0;">This link won't be shown again.</p>
        </div>
      </div>

      <div v-if="invites.length" class="invite-list">
        <div class="list-head">Outstanding invites</div>
        <div v-for="inv in invites" :key="inv.invite_id" class="invite-row">
          <span>{{ role_label(inv.role) }}</span>
          <span class="muted">{{ inv.email || 'no email' }}</span>
          <span class="muted">by {{ inv.created_by }}, expires {{ fmt_date(inv.expires_at) }}</span>
          <Button size="small" label="Revoke" severity="danger" variant="text"
                  @click="revoke_invite(inv)" />
        </div>
      </div>
    </div>

    <!-- ── add-user section ───────────────────────────────────────────────────── -->
    <div class="add-section">
      <Button size="small" :icon="show_add ? 'pi pi-minus' : 'pi pi-plus'"
              :label="show_add ? 'Cancel' : 'Add User'"
              severity="secondary"
              @click="show_add = !show_add; add_err = ''" />

      <div v-if="show_add" class="add-form">
        <Message v-if="add_err" severity="error" :closable="false" style="margin-bottom:0.5rem;">
          {{ add_err }}
        </Message>
        <div class="add-grid">
          <InputText v-model="add_form.username"   placeholder="Username" size="small" />
          <Password  v-model="add_form.password"   placeholder="Password" :feedback="true"  size="small" />
          <Password  v-model="add_form.pw_confirm" placeholder="Confirm"  :feedback="false" size="small" />
          <Select    v-model="add_form.role" :options="ROLES"
                     option-value="value" option-label="label"
                     size="small" style="width:9rem;" />
          <Button size="small" label="Create User" :loading="add_saving" @click="add_user" />
        </div>
      </div>
    </div>

  </Dialog>
</template>

<style scoped>
.user-list   { display: flex; flex-direction: column; gap: 0; margin-bottom: 1rem; }
.user-block  { border-bottom: 1px solid var(--p-content-border-color); }
.user-row    {
  display: grid;
  grid-template-columns: 1fr 9rem 1.4fr 11rem 6rem 5rem 5rem 7rem;
  align-items: center;
  gap: 0.5rem;
  padding: 0.45rem 0.25rem;
}
.header-row  {
  font-size: 0.78rem;
  color: var(--p-text-muted-color);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding-bottom: 0.3rem;
  border-bottom: 1px solid var(--p-content-border-color);
  margin-bottom: 0.1rem;
}
.uname       { font-weight: 500; display: flex; align-items: center; gap: 0.4rem; }
.uname.self  { color: var(--p-primary-color); }
.you-badge   {
  font-size: 0.7rem; font-weight: 600; padding: 0.1rem 0.35rem;
  background: var(--p-primary-color); color: var(--p-primary-contrast-color);
  border-radius: 3px; text-transform: uppercase; letter-spacing: 0.03em;
}
.warn-badge  {
  font-size: 0.75rem; font-weight: 600; padding: 0.1rem 0.4rem;
  background: var(--p-orange-500); color: #fff; border-radius: 3px;
}
.muted       { font-size: 0.82rem; color: var(--p-text-muted-color); }
.center      { text-align: center; }
.actions     { display: flex; gap: 0.1rem; justify-content: flex-end; }
.active-action { color: var(--p-primary-color) !important; }

.reset-form  {
  display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap;
  padding: 0.5rem 0.25rem 0.6rem 1rem;
  background: var(--p-content-background);
}
.reset-err   { font-size: 0.82rem; color: var(--p-red-400); }

.add-section { margin-top: 0.25rem; margin-bottom: 0.75rem; }
.email       { display: flex; align-items: center; gap: 0.1rem; min-width: 0; font-size: 0.85rem; }
.email-text  { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.invite-new  { margin-top: 0.75rem; display: flex; flex-direction: column; gap: 0.5rem; }
.invite-link { display: flex; gap: 0.5rem; align-items: center; }
.invite-list { margin-top: 0.75rem; }
.list-head   {
  font-size: 0.78rem; color: var(--p-text-muted-color); text-transform: uppercase;
  letter-spacing: 0.04em; padding-bottom: 0.3rem;
  border-bottom: 1px solid var(--p-content-border-color);
}
.invite-row  {
  display: grid; grid-template-columns: 7rem 1fr 1.5fr 5rem; align-items: center; gap: 0.5rem;
  padding: 0.3rem 0.25rem; border-bottom: 1px solid var(--p-content-border-color);
}
.add-form    { margin-top: 0.75rem; }
.add-grid    {
  display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: flex-start;
}
</style>
