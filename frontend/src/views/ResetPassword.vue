<script setup>
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Password from 'primevue/password'
import Button   from 'primevue/button'
import Message  from 'primevue/message'

// The password reset link lands here (backend routers/password_reset.py). Loading only GETs
// check, which is read-only; Set Password POSTs the token, which ends every session for the
// account. It doesn't sign in: the user does that fresh with the new password.

const route  = useRoute()
const router = useRouter()
const token  = typeof route.query.t === 'string' ? route.query.t : ''

const state      = ref('loading')     // loading | ready | invalid | done
const username   = ref('')
const password   = ref('')
const confirm_pw = ref('')
const err        = ref('')
const busy       = ref(false)

onMounted(async () => {
  if (!token) { state.value = 'invalid'; return }
  try {
    const r = await fetch(`/api/password_reset/check?t=${encodeURIComponent(token)}`)
    const b = r.ok ? await r.json() : { valid: false }
    if (!b.valid) { state.value = 'invalid'; return }
    username.value = b.username
    state.value    = 'ready'
  } catch { state.value = 'invalid' }
})

async function submit() {
  err.value = ''
  if (password.value.length < 8) { err.value = 'Password must be at least 8 characters.'; return }
  if (password.value !== confirm_pw.value) { err.value = 'Passwords do not match.'; return }
  busy.value = true
  try {
    const fd = new FormData()
    fd.append('t', token)
    fd.append('password', password.value)
    const r = await fetch('/api/password_reset/complete', { method: 'POST', body: fd })
    if (r.ok) {
      password.value = confirm_pw.value = ''
      // Every session for the account ended, this browser's included.
      localStorage.removeItem('bsc_username')
      localStorage.removeItem('bsc_role')
      state.value = 'done'
      router.replace({ query: {} })            // the token out of the address bar and history
      return
    }
    const b = await r.json().catch(() => null)
    err.value = b?.detail || 'Could not reset the password.'
    if (r.status === 400 && /expired|already used/.test(err.value)) state.value = 'invalid'
  } catch { err.value = 'Unable to reach the server.' }
  finally { busy.value = false }
}
</script>

<template>
  <div class="wrap">
    <div class="card">
      <h2>Reset password</h2>
      <p v-if="state === 'loading'" class="muted">Checking the link…</p>

      <template v-else-if="state === 'invalid'">
        <Message severity="warn" :closable="false">
          This reset link has expired or was already used. Request a new one from the
          sign-in page.
        </Message>
        <Button label="Go to sign in" severity="secondary" @click="router.replace('/admin')" fluid />
      </template>

      <template v-else-if="state === 'done'">
        <Message severity="success" :closable="false">
          Your password has been changed, and you've been signed out everywhere.
        </Message>
        <Button label="Sign in" @click="router.replace('/admin')" fluid />
      </template>

      <template v-else>
        <p class="muted">Choose a new password for <strong>{{ username }}</strong>.
          Every device signed in to this account will be signed out.</p>
        <Message v-if="err" severity="error" :closable="false">{{ err }}</Message>
        <!-- A hidden username field lets password managers file the new password correctly. -->
        <input type="text" :value="username" autocomplete="username" hidden readonly />
        <Password v-model="password" placeholder="New password" :feedback="true"
                  autocomplete="new-password" fluid />
        <Password v-model="confirm_pw" placeholder="Confirm new password" :feedback="false"
                  autocomplete="new-password" fluid @keyup.enter="submit" />
        <Button label="Set Password" :loading="busy" @click="submit" fluid />
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
</style>
