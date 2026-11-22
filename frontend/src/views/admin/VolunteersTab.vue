<script setup>
import { ref, computed, useTemplateRef, nextTick } from 'vue'
import Button   from 'primevue/button'
import Textarea from 'primevue/textarea'
import Message  from 'primevue/message'
import AddVolunteerForm  from '../../components/AddVolunteerForm.vue'
import PendingVolunteers from '../../components/PendingVolunteers.vue'
import VolunteerList     from '../../components/VolunteerList.vue'
import { import_private_key, decrypt_all } from '../../lib/email_crypto.js'

// The Volunteers tab (organizers): add a volunteer, approve signups, and the full list.
//
// Bulk decrypt: the organizer gives the private key (pasted, or read from a file in this
// browser), and every volunteer's email_enc from GET /api/admin/volunteers is decrypted here
// with Web Crypto. The key is kept as a non-extractable CryptoKey in this component's memory —
// never stored, never sent — and dropped by Forget key or by leaving the page. With a key in
// hand, reload() decrypts again, so addresses appear for volunteers added meanwhile.

const emit = defineEmits(['pending-changed'])

const list    = useTemplateRef('list')
const file_input = useTemplateRef('file_input')
const pending = useTemplateRef('pending')

const adding = ref(false)

// ── decrypt ───────────────────────────────────────────────────────────────────
const dec_open = ref(false)
const pem      = ref('')
const emails   = ref({})
const dec_msg  = ref('')
const dec_err  = ref('')
const dec_busy = ref(false)
let key = null

function read_file(ev) {
  const f = ev.target.files?.[0]
  if (!f) return
  const reader = new FileReader()
  reader.onload = () => { pem.value = String(reader.result || '') }
  reader.readAsText(f)
  ev.target.value = ''
}

async function decrypt() {
  dec_err.value = ''; dec_msg.value = ''
  dec_busy.value = true
  try {
    key = await import_private_key(pem.value)
  } catch {
    dec_err.value = "That isn't an RSA private key in PKCS#8 PEM (-----BEGIN PRIVATE KEY-----)."
    dec_busy.value = false
    return
  }
  pem.value = ''          // the CryptoKey is all that's kept
  await run_decrypt()
  dec_busy.value = false
  if (Object.keys(emails.value).length) dec_open.value = false
}

async function run_decrypt() {
  if (!key) return
  const vols = list.value?.vols || []
  const { emails: out, failed } = await decrypt_all(key, vols)
  const with_enc = vols.filter(v => v.email_enc).length
  if (with_enc && !Object.keys(out).length) {
    key = null
    emails.value = {}
    dec_err.value = 'None of the addresses decrypt with that key. Is it the right one?'
    return
  }
  emails.value = out
  dec_msg.value = `Decrypted ${Object.keys(out).length} of ${with_enc} addresses` +
    (failed ? ` (${failed} wouldn't decrypt)` : '') + '. The key stays in this page only.'
}

// ── fill in missing email hashes ──────────────────────────────────────────────
// Volunteers added by hand before add_vol computed email_hmac have none. With the addresses
// decrypted here, each is sent once to be hashed server-side (and dropped there).
const unindexed = computed(() => (list.value?.vols || [])
  .filter(v => v.email_enc && !v.email_indexed && emails.value[v.vol_id]))
const idx_busy = ref(false)
const idx_msg  = ref('')

async function fill_hashes() {
  idx_busy.value = true
  idx_msg.value  = ''
  let ok = 0
  const failed = []
  for (const v of unindexed.value) {
    const fd = new FormData()
    fd.append('vol_id', v.vol_id)
    fd.append('email', emails.value[v.vol_id])
    const r = await fetch('/api/admin/volunteers/email_hmac', { method: 'POST', body: fd })
    if (r.ok) ok++
    else failed.push(`${v.name}: ${(await r.json().catch(() => null))?.detail || r.status}`)
  }
  idx_busy.value = false
  idx_msg.value = `Indexed ${ok}.` + (failed.length ? ` Not indexed: ${failed.join('; ')}` : '')
  await list.value?.reload()
}

function forget() {
  key = null
  emails.value = {}
  pem.value = ''
  dec_msg.value = ''
  dec_err.value = ''
}

// ── wiring ────────────────────────────────────────────────────────────────────
async function reload() {
  await Promise.all([pending.value?.reload(), list.value?.reload()])
  await run_decrypt()
}

function on_pending_changed() {
  list.value?.reload()
  emit('pending-changed')
}

async function review(vol_id) {
  await nextTick()
  const el = document.getElementById(`pending-${vol_id}`)
  if (el) {
    el.scrollIntoView({ behavior: 'smooth', block: 'center' })
    el.classList.add('flash')
    setTimeout(() => el.classList.remove('flash'), 1500)
  }
}

defineExpose({ reload })
</script>

<template>
  <div class="tab">
    <div class="top">
      <Button :label="adding ? 'Cancel' : 'Add Volunteer'" :icon="adding ? 'pi pi-times' : 'pi pi-user-plus'"
              severity="secondary" @click="adding = !adding" />
      <Button v-if="!Object.keys(emails).length" label="Decrypt emails" icon="pi pi-lock-open"
              severity="secondary" @click="dec_open = !dec_open" />
      <Button v-else label="Forget key" icon="pi pi-lock" severity="secondary" @click="forget" />
      <Button v-if="unindexed.length" :label="`Index ${unindexed.length} hand-added email(s)`"
              icon="pi pi-hashtag" severity="secondary" :loading="idx_busy" @click="fill_hashes" />
      <span v-if="dec_msg" class="muted small">{{ dec_msg }}</span>
      <span v-if="idx_msg" class="muted small">{{ idx_msg }}</span>
    </div>

    <div v-if="dec_open && !Object.keys(emails).length" class="decrypt">
      <p class="muted small">
        Paste the organizers' private key, or choose the file. It's used in this browser only:
        never uploaded, never saved, and forgotten when you leave this page.
      </p>
      <Textarea v-model="pem" rows="4" autoResize placeholder="-----BEGIN PRIVATE KEY-----"
                spellcheck="false" class="pem" />
      <div class="row">
        <input ref="file_input" type="file" accept=".pem,.key,.txt" class="hidden-file"
               @change="read_file" />
        <Button size="small" severity="secondary" text label="Choose file…"
                @click="file_input?.click()" />
        <Button size="small" label="Decrypt" :loading="dec_busy" :disabled="!pem.trim()" @click="decrypt" />
      </div>
      <Message v-if="dec_err" severity="error" :closable="false">{{ dec_err }}</Message>
    </div>

    <AddVolunteerForm v-if="adding" @vol-added="list?.reload()" @close="adding = false" />
    <PendingVolunteers ref="pending" @changed="on_pending_changed" />
    <VolunteerList ref="list" :emails="emails" @review="review" />
  </div>
</template>

<style scoped>
.tab     { display: flex; flex-direction: column; gap: 1rem; }
.top     { display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: center; }
.muted   { color: var(--p-text-muted-color); }
.small   { font-size: 0.85rem; }
.decrypt { border: 1px solid var(--p-content-border-color); border-radius: 6px; padding: 1rem;
           max-width: 40rem; display: flex; flex-direction: column; gap: 0.5rem; }
.decrypt p { margin: 0; }
.pem     { font-family: monospace; font-size: 0.8rem; width: 100%; }
.row     { display: flex; gap: 0.5rem; align-items: center; }
.hidden-file { display: none; }
:global(.flash) { animation: flash 1.5s ease-out; }
@keyframes flash { 0% { box-shadow: 0 0 0 3px var(--p-primary-color); } 100% { box-shadow: none; } }
</style>
