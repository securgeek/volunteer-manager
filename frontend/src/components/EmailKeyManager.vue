<script setup>
import { ref } from 'vue'
import Dialog    from 'primevue/dialog'
import Button    from 'primevue/button'
import Textarea  from 'primevue/textarea'
import InputText from 'primevue/inputtext'
import Message   from 'primevue/message'
import Tabs      from 'primevue/tabs'
import Tab       from 'primevue/tab'
import TabList   from 'primevue/tablist'
import TabPanels from 'primevue/tabpanels'
import TabPanel  from 'primevue/tabpanel'
import { pem_to_der, import_private_key, decrypt_email as decrypt_with } from '../lib/email_crypto.js'

const visible    = ref(false)
const active_tab = ref('manage')

// ── manage-key state ─────────────────────────────────────────────────────────
const cur_fingerprint = ref(null)
const new_pem         = ref('')
const key_error       = ref('')
const key_saving      = ref(false)
const key_ok          = ref(false)

// ── decrypt state ────────────────────────────────────────────────────────────
const dec_vol_id  = ref('')
const dec_privkey = ref('')
const dec_result  = ref(null)
const dec_error   = ref('')
const dec_loading = ref(false)

// ── helpers ──────────────────────────────────────────────────────────────────
async function fingerprint(pem) {
  try {
    const hash = await crypto.subtle.digest('SHA-256', pem_to_der(pem))
    const b64  = btoa(String.fromCharCode(...new Uint8Array(hash)))
    return 'SHA256:' + b64.replace(/=/g, '')
  } catch { return '(invalid key)' }
}

async function load_key() {
  try {
    const resp = await fetch('/api/email_pubkey')
    if (resp.ok) {
      const { pubkey } = await resp.json()
      cur_fingerprint.value = pubkey ? await fingerprint(pubkey) : null
    }
  } catch { cur_fingerprint.value = null }
}

function read_file_into(target_ref) {
  const input = document.createElement('input')
  input.type   = 'file'
  input.accept = '.pem,.key,.pub,text/plain'
  input.onchange = () => {
    const file = input.files?.[0]
    if (!file) return
    const reader = new FileReader()
    reader.onload = e => { target_ref.value = e.target.result }
    reader.readAsText(file)
  }
  input.click()
}

function load_pubkey_file()  { read_file_into(new_pem) }
function load_privkey_file() { read_file_into(dec_privkey) }

// ── open ─────────────────────────────────────────────────────────────────────
function open() {
  active_tab.value    = 'manage'
  new_pem.value       = ''
  key_error.value     = ''
  key_ok.value        = false
  dec_vol_id.value    = ''
  dec_privkey.value   = ''
  dec_result.value    = null
  dec_error.value     = ''
  load_key()
  visible.value = true
}
defineExpose({ open })

// ── save public key ───────────────────────────────────────────────────────────
async function save_key() {
  key_error.value = ''
  key_ok.value    = false
  const pem = new_pem.value.trim()
  if (!pem) { key_error.value = 'Paste a PEM public key above.'; return }
  try {
    await crypto.subtle.importKey(
      'spki', pem_to_der(pem),
      { name: 'RSA-OAEP', hash: 'SHA-256' }, false, ['encrypt']
    )
  } catch {
    key_error.value = 'Invalid key — must be an RSA public key in SPKI/PEM format.'
    return
  }
  key_saving.value = true
  try {
    const fd = new FormData()
    fd.append('pubkey', pem)
    const resp = await fetch('/api/email_pubkey', { method: 'POST', body: fd })
    if (resp.ok) {
      cur_fingerprint.value = await fingerprint(pem)
      new_pem.value = ''
      key_ok.value  = true
    } else {
      const body = await resp.json().catch(() => null)
      key_error.value = body?.detail || 'Failed to save key.'
    }
  } finally { key_saving.value = false }
}

// ── decrypt email ────────────────────────────────────────────────────────────
async function decrypt_email() {
  dec_error.value  = ''
  dec_result.value = null
  if (!dec_vol_id.value.trim() || !dec_privkey.value.trim()) {
    dec_error.value = 'Volunteer ID and private key are both required.'
    return
  }
  dec_loading.value = true
  try {
    const fd = new FormData()
    fd.append('vol_id', dec_vol_id.value.trim())
    const resp = await fetch('/api/get_vol_email', { method: 'POST', body: fd })
    if (!resp.ok) {
      const body = await resp.json().catch(() => null)
      dec_error.value = body?.detail || 'Failed to fetch email record.'
      return
    }
    const { email_enc } = await resp.json()
    if (!email_enc) {
      dec_error.value = 'No email address on record for this volunteer.'
      return
    }
    const key = await import_private_key(dec_privkey.value)
    dec_result.value = await decrypt_with(key, email_enc)
  } catch (e) {
    dec_error.value = 'Decryption failed — verify this is the correct private key. (' + e.message + ')'
  } finally { dec_loading.value = false }
}
</script>

<template>
  <Dialog v-model:visible="visible" header="Email Key Management"
          :style="{ width: '42rem' }" modal>
    <Tabs v-model:value="active_tab">
      <TabList>
        <Tab value="manage">Public Key</Tab>
        <Tab value="decrypt">Decrypt Email</Tab>
      </TabList>
      <TabPanels>

        <!-- ── Public Key tab ─────────────────────────────────────────────── -->
        <TabPanel value="manage">
          <div class="section">
            <div class="fp-row">
              <span class="label">Active key</span>
              <span v-if="cur_fingerprint" class="fingerprint">{{ cur_fingerprint }}</span>
              <span v-else class="none">None configured</span>
            </div>

            <div class="hint">
              Generate a 4096-bit RSA key pair (OpenSSL):<br>
              <code>openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:4096 -out private.pem</code>
              <code>openssl pkey -in private.pem -pubout -out public.pem</code>
              Upload <strong>public.pem</strong> here; keep <strong>private.pem</strong> offline.
            </div>

            <Textarea v-model="new_pem" rows="6"
                      placeholder="-----BEGIN PUBLIC KEY-----&#10;...&#10;-----END PUBLIC KEY-----"
                      class="pem-area" />

            <Message v-if="key_error" severity="error"   :closable="false" style="margin-top:0.5rem;">{{ key_error }}</Message>
            <Message v-if="key_ok"    severity="success" :closable="false" style="margin-top:0.5rem;">Key saved. New encryptions will use this key.</Message>

            <div class="btn-row">
              <Button size="small" label="Load from file" severity="secondary"
                      icon="pi pi-upload" @click="load_pubkey_file" />
              <Button size="small" label="Save Key" :loading="key_saving" @click="save_key" />
            </div>
          </div>
        </TabPanel>

        <!-- ── Decrypt Email tab ──────────────────────────────────────────── -->
        <TabPanel value="decrypt">
          <div class="section">
            <div class="vol-row">
              <label>Volunteer ID</label>
              <InputText v-model="dec_vol_id" placeholder="e.g. 1042" style="width:12rem;" />
            </div>

            <div class="field-label">Private key (PKCS#8 PEM):</div>
            <Textarea v-model="dec_privkey" rows="7"
                      placeholder="-----BEGIN PRIVATE KEY-----&#10;...&#10;-----END PRIVATE KEY-----"
                      class="pem-area" />

            <div class="btn-row">
              <Button size="small" label="Load from file" severity="secondary"
                      icon="pi pi-upload" @click="load_privkey_file" />
              <Button size="small" label="Decrypt" :loading="dec_loading" @click="decrypt_email" />
            </div>

            <Message v-if="dec_error" severity="error" :closable="false" style="margin-top:0.75rem;">
              {{ dec_error }}
            </Message>

            <div v-if="dec_result" class="result-box">
              {{ dec_result }}
            </div>

            <div class="hint" style="margin-top:0.75rem;">
              <i class="pi pi-lock" style="font-size:0.75rem;" />
              The private key is used locally in your browser and is never sent to the server.
            </div>
          </div>
        </TabPanel>

      </TabPanels>
    </Tabs>
  </Dialog>
</template>

<style scoped>
.section     { padding: 0.75rem 0; }
.fp-row      { display: flex; align-items: center; gap: 0.75rem; margin-bottom: 0.85rem; }
.label       { font-size: 0.85rem; color: var(--p-text-muted-color); min-width: 6rem; }
.fingerprint { font-family: monospace; font-size: 0.8rem; word-break: break-all; }
.none        { color: var(--p-text-muted-color); font-style: italic; font-size: 0.85rem; }
.vol-row     { display: flex; align-items: center; gap: 0.75rem; margin-bottom: 0.75rem; }
.vol-row label { font-size: 0.85rem; min-width: 8rem; }
.field-label { font-size: 0.85rem; margin-bottom: 0.4rem; }
.btn-row     { margin-top: 0.6rem; display: flex; gap: 0.5rem; }
.pem-area    { width: 100%; font-family: monospace; font-size: 0.78rem; }
.hint {
  font-size: 0.78rem;
  color: var(--p-text-muted-color);
  line-height: 1.55;
  margin-bottom: 0.75rem;
  display: flex;
  flex-direction: column;
  gap: 0.1rem;
}
.hint code {
  background: var(--p-content-background);
  border: 1px solid var(--p-content-border-color);
  border-radius: 3px;
  padding: 0.2rem 0.5rem;
  font-size: 0.74rem;
}
.result-box {
  margin-top: 0.75rem;
  padding: 0.75rem 1rem;
  background: var(--p-content-background);
  border: 1px solid var(--p-green-500);
  border-radius: 6px;
  font-size: 1rem;
  word-break: break-all;
}
</style>
