<script setup>
import { ref, reactive, onMounted } from 'vue'
import InputText from 'primevue/inputtext'
import Button    from 'primevue/button'
import Message   from 'primevue/message'

// Add a volunteer by hand, inline at the top of the Volunteers tab. The email is encrypted
// in this browser (RSA-OAEP, the organizers' public key) before it's sent; the server never
// sees it. `vol-added` after a save, `close` for Cancel.
const emit = defineEmits(['vol-added', 'close'])

const pubkey   = ref(null)
const error_msg = ref('')
const saving   = ref(false)

const form = reactive({
  vol_id: '', first_name: '', last_name: '', nickname: '',
  time_pref: '', add_time: '', email: '',
})

async function load_pubkey() {
  try {
    const resp = await fetch('/api/email_pubkey')
    if (resp.ok) { pubkey.value = (await resp.json()).pubkey || null }
  } catch { pubkey.value = null }
}

onMounted(load_pubkey)

function pem_to_der(pem) {
  const b64 = pem.replace(/-----BEGIN [^-]+-----/, '').replace(/-----END [^-]+-----/, '').replace(/\s/g, '')
  return Uint8Array.from(atob(b64), c => c.charCodeAt(0)).buffer
}

async function encrypt_email(email) {
  const key = await crypto.subtle.importKey(
    'spki', pem_to_der(pubkey.value),
    { name: 'RSA-OAEP', hash: 'SHA-256' }, false, ['encrypt']
  )
  const enc = await crypto.subtle.encrypt({ name: 'RSA-OAEP' }, key, new TextEncoder().encode(email))
  return btoa(String.fromCharCode(...new Uint8Array(enc)))
}

async function save() {
  error_msg.value = ''
  if (!form.vol_id.trim() || !form.first_name.trim() || !form.last_name.trim()) {
    error_msg.value = 'ID, first name, and last name are required.'
    return
  }
  saving.value = true
  try {
    let email_enc = ''
    if (form.email.trim() && pubkey.value) {
      try {
        email_enc = await encrypt_email(form.email.trim())
      } catch (e) {
        error_msg.value = 'Email encryption failed. Verify the public key is a valid RSA SPKI PEM. (' + e.message + ')'
        return
      }
    }
    const fd = new FormData()
    fd.append('vol_id',      form.vol_id.trim())
    fd.append('first_name',  form.first_name.trim())
    fd.append('last_name',   form.last_name.trim())
    fd.append('nickname',    form.nickname.trim())
    fd.append('time_pref',   form.time_pref.trim())
    fd.append('add_time',    form.add_time.trim())
    // The plain address goes too, only so the server can compute email_hmac (duplicate
    // detection, as portal signups have); it's hashed and dropped there, never stored.
    if (email_enc) { fd.append('email_enc', email_enc); fd.append('email', form.email.trim()) }
    const resp = await fetch('/api/add_vol', { method: 'POST', body: fd })
    if (resp.ok) {
      const added = await resp.json()
      emit('vol-added', added)
      if (email_enc && !added.email_indexed)
        error_msg.value = `Added ${added.name}, but the scheduler has no EMAIL_HMAC_KEY, so ` +
                          'their address can\'t be checked for duplicates.'
      Object.assign(form, { vol_id: '', first_name: '', last_name: '', nickname: '',
                            time_pref: '', add_time: '', email: '' })
    } else {
      const body = await resp.json().catch(() => null)
      error_msg.value = body?.detail || 'Failed to add volunteer.'
    }
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="av-panel">
    <h3>Add a volunteer</h3>
    <Message v-if="error_msg" severity="error" :closable="false" style="margin-bottom:0.75rem;">
      {{ error_msg }}
    </Message>
    <div class="av-grid">
      <label>ID <span class="req">*</span></label>
      <InputText v-model="form.vol_id" placeholder="e.g. 1042" fluid />
      <label>First name <span class="req">*</span></label>
      <InputText v-model="form.first_name" fluid />
      <label>Last name <span class="req">*</span></label>
      <InputText v-model="form.last_name" fluid />
      <label>Nickname</label>
      <InputText v-model="form.nickname" fluid />
      <label>Time pref</label>
      <InputText v-model="form.time_pref" placeholder="Saturday AM; Sunday PM" fluid />
      <label>Additional</label>
      <InputText v-model="form.add_time" fluid />
      <label>Email</label>
      <div>
        <InputText v-model="form.email" :disabled="!pubkey"
                   placeholder="volunteer@example.com" fluid />
        <div class="hint" v-if="!pubkey">
          No public key configured — email will not be saved.
          Upload one via <em>Email Key</em>.
        </div>
        <div class="hint enc" v-else>
          <i class="pi pi-lock" style="font-size:0.75rem;" />
          Encrypted with RSA-OAEP in the browser before sending.
        </div>
      </div>
    </div>
    <div style="margin-top:1.25rem; display:flex; justify-content:flex-end; gap:0.5rem;">
      <Button label="Cancel" severity="secondary" @click="emit('close')" />
      <Button label="Add Volunteer" :loading="saving" @click="save" />
    </div>
  </div>
</template>

<style scoped>
.av-panel { border: 1px solid var(--p-content-border-color); border-radius: 6px;
            padding: 1rem; margin-bottom: 1.25rem; max-width: 36rem; }
.av-panel h3 { margin: 0 0 0.75rem; font-size: 1rem; }
.av-grid {
  display: grid;
  grid-template-columns: 8rem 1fr;
  gap: 0.55rem 0.75rem;
  align-items: center;
}
.req  { color: var(--p-red-400); }
.hint { font-size: 0.78rem; color: var(--p-text-muted-color); margin-top: 0.25rem; }
.hint.enc { color: var(--p-green-400); display: flex; align-items: center; gap: 0.3rem; }
</style>
