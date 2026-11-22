<script setup>
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import InputText from 'primevue/inputtext'
import Password  from 'primevue/password'
import Button    from 'primevue/button'
import Message   from 'primevue/message'

// The admin invite link lands here (backend routers/invites.py). Loading only GETs check,
// which is read-only, because mail and chat previews fetch links; the token is spent by the
// POST behind Create Account, which also signs the new account in.

const route  = useRoute()
const router = useRouter()
const token  = typeof route.query.t === 'string' ? route.query.t : ''

const state      = ref('loading')     // loading | ready | invalid
const role       = ref('')
const expires_at = ref('')
const username   = ref('')
const password   = ref('')
const confirm_pw = ref('')
const email      = ref('')
const err        = ref('')
const busy       = ref(false)

const ROLE_LABEL = { organizer: 'Organizer', checkin: 'Check-in', view: 'View' }

onMounted(async () => {
  if (!token) { state.value = 'invalid'; return }
  try {
    const r = await fetch(`/api/invites/check?t=${encodeURIComponent(token)}`)
    const b = r.ok ? await r.json() : { valid: false }
    if (!b.valid) { state.value = 'invalid'; return }
    role.value       = b.role
    email.value      = b.email || ''
    expires_at.value = new Date(b.expires_at).toLocaleString()
    state.value      = 'ready'
  } catch { state.value = 'invalid' }
})

async function accept() {
  err.value = ''
  if (!username.value.trim()) { err.value = 'Pick a username.'; return }
  if (password.value.length < 8) { err.value = 'Password must be at least 8 characters.'; return }
  if (password.value !== confirm_pw.value) { err.value = 'Passwords do not match.'; return }
  busy.value = true
  try {
    const fd = new FormData()
    fd.append('t', token)
    fd.append('username', username.value.trim())
    fd.append('password', password.value)
    fd.append('email', email.value.trim())
    const r = await fetch('/api/invites/accept', { method: 'POST', body: fd })
    const b = await r.json().catch(() => null)
    if (!r.ok) {
      err.value = b?.detail || 'Could not create the account.'
      if (r.status === 400 && /expired|already used|revoked/.test(err.value)) state.value = 'invalid'
      return
    }
    localStorage.setItem('bsc_username', b.username)
    localStorage.setItem('bsc_role',     b.role)
    // replace, not push: the token must not sit in history.
    router.replace('/admin')
  } catch { err.value = 'Unable to reach the server.' }
  finally { busy.value = false }
}
</script>

<template>
  <div class="wrap">
    <div class="card">
      <h2>Create your account</h2>
      <p v-if="state === 'loading'" class="muted">Checking the invite…</p>

      <template v-else-if="state === 'invalid'">
        <Message severity="warn" :closable="false">
          This invite link has expired, was already used, or was revoked. Ask an organizer
          for a new one.
        </Message>
        <Button label="Go to sign in" severity="secondary" @click="router.replace('/admin')" fluid />
      </template>

      <template v-else>
        <p class="muted">
          You've been invited as <strong>{{ ROLE_LABEL[role] || role }}</strong>.
          This link works once, until {{ expires_at }}.
        </p>
        <Message v-if="err" severity="error" :closable="false">{{ err }}</Message>
        <InputText v-model="username" placeholder="Username" autocomplete="username" fluid />
        <Password v-model="password" placeholder="Password" :feedback="true"
                  autocomplete="new-password" fluid />
        <Password v-model="confirm_pw" placeholder="Confirm password" :feedback="false"
                  autocomplete="new-password" fluid />
        <InputText v-model="email" type="email" placeholder="Email (optional)"
                   autocomplete="email" fluid @keyup.enter="accept" />
        <p class="hint">Your email is only used to send you a password reset link.</p>
        <Button label="Create Account" :loading="busy" @click="accept" fluid />
      </template>
    </div>
  </div>
</template>

<style scoped>
.wrap  { display: flex; justify-content: center; align-items: center; min-height: 80vh;
         padding: 0 1rem; }
.card  { width: 100%; max-width: 22rem; display: flex; flex-direction: column; gap: 1rem; }
h2     { margin: 0; text-align: center; }
.muted { color: var(--p-text-muted-color); margin: 0; line-height: 1.5; }
.hint  { color: var(--p-text-muted-color); font-size: 0.82rem; margin: -0.5rem 0 0; }
</style>
