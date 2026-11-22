<script setup>
import { ref } from 'vue'
import Dialog   from 'primevue/dialog'
import Button   from 'primevue/button'
import Password from 'primevue/password'
import InputText from 'primevue/inputtext'
import Message  from 'primevue/message'
import Tag      from 'primevue/tag'

// The signed-in user's own account: who they are, and changing their own password
// (POST /api/users/change_password, any role). Organizers reset other people's passwords in
// UserManager; this is the only way to change your own. The email address is where a
// "Forgot password?" link goes (POST /api/users/change_email, which also needs the current
// password: a stolen session mustn't be able to redirect resets). Passkeys have their own dialog,
// which `open-passkeys` asks Admin.vue to open.

const props = defineProps({ username: String, role: String })
const emit  = defineEmits(['open-passkeys'])

const visible = ref(false)
const current = ref('')
const next_pw = ref('')
const confirm_pw = ref('')
const saving  = ref(false)
const err     = ref('')
const done    = ref(false)

const email        = ref('')
const saved_email  = ref('')
const email_pw     = ref('')
const email_saving = ref(false)
const email_err    = ref('')
const email_done   = ref(false)

async function load_me() {
  try {
    const r = await fetch('/api/users/me')
    if (r.ok) email.value = saved_email.value = (await r.json()).email || ''
  } catch { /* the section just starts blank */ }
}

function open() {
  current.value = next_pw.value = confirm_pw.value = ''
  err.value = ''
  done.value = false
  email.value = saved_email.value = email_pw.value = email_err.value = ''
  email_done.value = false
  load_me()
  visible.value = true
}
defineExpose({ open })

async function change_password() {
  err.value = ''; done.value = false
  if (!current.value) { err.value = 'Enter your current password.'; return }
  if (next_pw.value.length < 8) { err.value = 'New password must be at least 8 characters.'; return }
  if (next_pw.value !== confirm_pw.value) { err.value = 'New passwords do not match.'; return }
  saving.value = true
  try {
    const fd = new FormData()
    fd.append('current_password', current.value)
    fd.append('new_password', next_pw.value)
    const r = await fetch('/api/users/change_password', { method: 'POST', body: fd })
    if (r.ok) {
      done.value = true
      current.value = next_pw.value = confirm_pw.value = ''
    } else {
      const b = await r.json().catch(() => null)
      err.value = b?.detail || 'Failed to change your password.'
    }
  } catch { err.value = 'Unable to reach the server.' }
  finally { saving.value = false }
}

async function change_email() {
  email_err.value = ''; email_done.value = false
  if (!email_pw.value) { email_err.value = 'Enter your current password.'; return }
  email_saving.value = true
  try {
    const fd = new FormData()
    fd.append('current_password', email_pw.value)
    fd.append('email', email.value.trim())
    const r = await fetch('/api/users/change_email', { method: 'POST', body: fd })
    const b = await r.json().catch(() => null)
    if (r.ok) {
      email.value = saved_email.value = b.email || ''
      email_pw.value = ''
      email_done.value = true
    } else {
      email_err.value = b?.detail || 'Failed to change your email.'
    }
  } catch { email_err.value = 'Unable to reach the server.' }
  finally { email_saving.value = false }
}
</script>

<template>
  <Dialog v-model:visible="visible" header="Profile" :style="{ width: '26rem' }" modal>
    <div class="who">
      <strong>{{ props.username }}</strong>
      <Tag severity="secondary" :value="props.role" />
    </div>

    <h4>Change password</h4>
    <Message v-if="err" severity="error" :closable="false" class="msg">{{ err }}</Message>
    <Message v-if="done" severity="success" :closable="false" class="msg">Password changed.</Message>
    <div class="form">
      <Password v-model="current" placeholder="Current password" :feedback="false" fluid
                autocomplete="current-password" />
      <Password v-model="next_pw" placeholder="New password" :feedback="true" fluid
                autocomplete="new-password" />
      <Password v-model="confirm_pw" placeholder="Confirm new password" :feedback="false" fluid
                autocomplete="new-password" @keyup.enter="change_password" />
      <Button label="Change Password" :loading="saving" @click="change_password" />
    </div>

    <h4>Email</h4>
    <p class="muted">Where a password reset link is sent. You still sign in with your username.</p>
    <Message v-if="email_err" severity="error" :closable="false" class="msg">{{ email_err }}</Message>
    <Message v-if="email_done" severity="success" :closable="false" class="msg">
      {{ saved_email ? 'Email saved.' : 'Email removed.' }}</Message>
    <div class="form">
      <InputText v-model="email" type="email" placeholder="you@example.com" fluid
                 autocomplete="email" />
      <template v-if="email.trim() !== saved_email">
        <Password v-model="email_pw" placeholder="Current password" :feedback="false" fluid
                  autocomplete="current-password" @keyup.enter="change_email" />
        <Button :label="email.trim() ? 'Save Email' : 'Remove Email'" :loading="email_saving"
                @click="change_email" />
      </template>
    </div>

    <h4>Passkeys</h4>
    <p class="muted">Sign in with your fingerprint, face or device PIN instead of a password.</p>
    <Button label="Manage Passkeys" icon="pi pi-shield" severity="secondary" size="small"
            @click="visible = false; emit('open-passkeys')" />
  </Dialog>
</template>

<style scoped>
.who   { display: flex; align-items: center; gap: 0.5rem; }
h4     { margin: 1.25rem 0 0.5rem; font-size: 0.95rem; }
.form  { display: flex; flex-direction: column; gap: 0.5rem; }
.msg   { margin-bottom: 0.5rem; }
.muted { color: var(--p-text-muted-color); font-size: 0.85rem; margin: 0 0 0.5rem; }
</style>
