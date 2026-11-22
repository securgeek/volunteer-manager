<script setup>
import { ref, onMounted } from 'vue'
import InputText from 'primevue/inputtext'
import Password  from 'primevue/password'
import Button    from 'primevue/button'
import Message   from 'primevue/message'
import { passkeys_enabled, passkey_sign_in } from '../../lib/passkey.js'

const props = defineProps({ appTitle: String, appLogo: String })
const emit  = defineEmits(['logged-in'])

const username  = ref('')
const password  = ref('')
const error_msg = ref('')
const loading   = ref(false)

// Shown only when the browser has WebAuthn and the server has a relying party configured.
const passkey_ok      = ref(false)
const passkey_loading = ref(false)
onMounted(async () => { passkey_ok.value = await passkeys_enabled() })

function signed_in(data) {
  localStorage.setItem('bsc_username', data.username)
  localStorage.setItem('bsc_role',     data.role)
  password.value = ''
  emit('logged-in', { username: data.username, role: data.role })
}

async function passkey() {
  error_msg.value       = ''
  passkey_loading.value = true
  try {
    signed_in(await passkey_sign_in())
  } catch (e) {
    if (!e.cancelled) error_msg.value = e.message
  } finally {
    passkey_loading.value = false
  }
}

// "Forgot password?": the server's reply is the same whether or not the account exists or has
// an email address, so it's shown as given (backend routers/password_reset.py).
const forgot      = ref(false)
const forgot_msg  = ref('')
const forgot_busy = ref(false)

async function request_reset() {
  error_msg.value = ''
  forgot_msg.value = ''
  if (!username.value.trim()) { error_msg.value = 'Enter your username.'; return }
  forgot_busy.value = true
  const form = new FormData()
  form.append('username', username.value.trim())
  try {
    const resp = await fetch('/api/password_reset/request', { method: 'POST', body: form })
    const body = await resp.json().catch(() => null)
    if (resp.ok) forgot_msg.value = body.detail
    else error_msg.value = body?.detail || 'Could not request a reset.'
  } catch {
    error_msg.value = 'Unable to reach the server.'
  } finally {
    forgot_busy.value = false
  }
}

async function submit() {
  error_msg.value = ''
  loading.value   = true
  const form = new FormData()
  form.append('username', username.value)
  form.append('password', password.value)
  try {
    const resp = await fetch('/api/login', { method: 'POST', body: form })
    if (resp.ok) {
      signed_in(await resp.json())
    } else {
      error_msg.value = 'Invalid username or password.'
    }
  } catch {
    error_msg.value = 'Unable to reach the server.'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div style="display:flex; justify-content:center; align-items:center; min-height:80vh;">
    <div style="width:22rem; display:flex; flex-direction:column; gap:1rem;">
      <div style="text-align:center;">
        <img v-if="appLogo" :src="appLogo"
             style="height:33vh; max-width:100%; object-fit:contain; margin-bottom:0.75rem;" />
        <h2 style="margin:0;">{{ appTitle }}</h2>
      </div>
      <Message v-if="error_msg" severity="error" :closable="false">{{ error_msg }}</Message>
      <template v-if="forgot">
        <p class="note">Enter your username. If the account has an email address, we'll send
          it a link to set a new password.</p>
        <Message v-if="forgot_msg" severity="success" :closable="false">{{ forgot_msg }}</Message>
        <InputText v-model="username" placeholder="Username" @keyup.enter="request_reset" fluid />
        <Button label="Send Reset Link" :loading="forgot_busy" @click="request_reset" fluid />
        <a href="#" class="link" @click.prevent="forgot = false; forgot_msg = ''; error_msg = ''">
          Back to sign in</a>
      </template>
      <template v-else>
      <InputText v-model="username" placeholder="Username" @keyup.enter="submit" fluid />
      <Password  v-model="password" placeholder="Password" :feedback="false" @keyup.enter="submit" fluid />
      <Button label="Sign In" :loading="loading" @click="submit" fluid />
      <a href="#" class="link" @click.prevent="forgot = true; error_msg = ''">Forgot password?</a>
      </template>
      <template v-if="passkey_ok && !forgot">
        <div class="or">or</div>
        <Button label="Sign in with a passkey" icon="pi pi-shield" severity="secondary"
                :loading="passkey_loading" @click="passkey" fluid />
      </template>
    </div>
  </div>
</template>

<style scoped>
.or   { text-align: center; font-size: 0.85rem; color: var(--p-text-muted-color); }
.link { text-align: center; font-size: 0.85rem; color: var(--p-primary-color); }
.note { margin: 0; font-size: 0.9rem; color: var(--p-text-muted-color); line-height: 1.5; }
</style>
