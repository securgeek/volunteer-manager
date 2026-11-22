<script setup>
import { ref, reactive, computed } from 'vue'
import Dialog    from 'primevue/dialog'
import Button    from 'primevue/button'
import InputText from 'primevue/inputtext'
import InputNumber from 'primevue/inputnumber'
import Checkbox  from 'primevue/checkbox'
import Message   from 'primevue/message'

// Admin → API Keys: keys for the read-only export API (backend/EXPORT_API.md), for other
// servers and for tools/vol_mail. A key is shown once, right after creation, with Copy; the
// server keeps only its hash. Keys are revoked, never deleted, so the list keeps saying which
// key did what.

const visible = ref(false)
const tokens  = ref([])
const scopes  = ref({})      // name → description, from the server
const err     = ref('')

const form    = reactive({ label: '', scopes: ['schedule:read', 'volunteers:read'],
                           expires_days: 30, allowed_ips: '' })
const saving  = ref(false)
const made    = ref(null)    // { key, label, expires_at } just created
const copied  = ref(false)
const confirm_revoke = ref(null)

const active   = computed(() => tokens.value.filter(t => t.active))
const inactive = computed(() => tokens.value.filter(t => !t.active))

async function load() {
  err.value = ''
  try {
    const r = await fetch('/api/api_tokens')
    if (!r.ok) { err.value = 'Could not load the keys.'; return }
    const body = await r.json()
    tokens.value = body.tokens
    scopes.value = body.scopes
  } catch { err.value = 'Unable to reach the server.' }
}

function open() {
  made.value = null
  confirm_revoke.value = null
  visible.value = true
  load()
}

async function create() {
  err.value = ''; made.value = null; copied.value = false
  saving.value = true
  try {
    const fd = new FormData()
    fd.append('label', form.label)
    fd.append('scopes', form.scopes.join(' '))
    fd.append('expires_days', String(form.expires_days ?? 30))
    fd.append('allowed_ips', form.allowed_ips)
    const r    = await fetch('/api/api_tokens', { method: 'POST', body: fd })
    const body = await r.json().catch(() => null)
    if (!r.ok) { err.value = body?.detail || 'Could not create the key.'; return }
    made.value = { ...body, label: form.label }
    form.label = ''; form.allowed_ips = ''
    await load()
  } finally {
    saving.value = false
  }
}

async function copy_key() {
  try {
    await navigator.clipboard.writeText(made.value.key)
    copied.value = true
  } catch { err.value = 'Copy failed; select the key and copy it by hand.' }
}

async function revoke(t) {
  confirm_revoke.value = null
  const fd = new FormData()
  fd.append('token_id', t.token_id)
  const r = await fetch('/api/api_tokens/revoke', { method: 'POST', body: fd })
  if (!r.ok) err.value = 'Could not revoke the key.'
  await load()
}

function when(iso) {
  return iso ? new Date(iso).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : '—'
}

defineExpose({ open })
</script>

<template>
  <Dialog v-model:visible="visible" header="API Keys" modal :style="{ width: '46rem' }">
    <p class="muted" style="margin-top:0;">
      Keys for the read-only export API, for another server or the mailing tool. Each key is
      shown once. Give each app or person its own, so one can be revoked without the others.
    </p>

    <Message v-if="err" severity="error" :closable="false" style="margin:0 0 0.75rem;">{{ err }}</Message>

    <!-- New key ─────────────────────────────────────────────── -->
    <div class="new-key">
      <div class="field">
        <label for="key-label">Label</label>
        <InputText id="key-label" v-model="form.label" placeholder="Badge app – Sam" fluid />
      </div>
      <div class="field">
        <span class="lbl">Scopes</span>
        <div v-for="(desc, name) in scopes" :key="name" class="scope">
          <Checkbox v-model="form.scopes" :input-id="'scope-' + name" :value="name" />
          <label :for="'scope-' + name"><code>{{ name }}</code> <span class="muted">{{ desc }}</span></label>
        </div>
      </div>
      <div class="row">
        <div class="field" style="width:9rem;">
          <label for="key-days">Expires in (days)</label>
          <InputNumber input-id="key-days" v-model="form.expires_days" :min="1" :max="365" fluid />
        </div>
        <div class="field" style="flex:1;">
          <label for="key-ips">Allowed IPs <span class="muted">(optional)</span></label>
          <InputText id="key-ips" v-model="form.allowed_ips" placeholder="203.0.113.10, 198.51.100.0/24" fluid />
        </div>
      </div>
      <div>
        <Button label="Create Key" icon="pi pi-key" :loading="saving"
                :disabled="!form.label.trim() || !form.scopes.length" @click="create()" />
      </div>
    </div>

    <div v-if="made" class="made">
      <strong>{{ made.label }}</strong>
      <div class="key-row">
        <code class="key">{{ made.key }}</code>
        <Button size="small" :icon="copied ? 'pi pi-check' : 'pi pi-copy'"
                :label="copied ? 'Copied' : 'Copy'" severity="secondary" @click="copy_key()" />
      </div>
      <p class="muted" style="margin:0;">
        This key won't be shown again. Send it as <code>Authorization: Bearer &lt;key&gt;</code>.
        Expires {{ when(made.expires_at) }}.
      </p>
    </div>

    <!-- Existing keys ───────────────────────────────────────── -->
    <h4>Active keys</h4>
    <p v-if="!active.length" class="muted">None.</p>
    <div v-for="t in active" :key="t.token_id" class="key-item">
      <div class="key-main">
        <strong>{{ t.label }}</strong>
        <span class="muted">{{ t.scopes.join(', ') }}</span>
        <span v-if="t.allowed_ips.length" class="muted">from {{ t.allowed_ips.join(', ') }}</span>
      </div>
      <div class="key-meta muted">
        made by {{ t.created_by }} · expires {{ when(t.expires_at) }} ·
        last used {{ t.last_used_at ? `${when(t.last_used_at)} from ${t.last_used_ip}` : 'never' }}
      </div>
      <div class="key-actions">
        <Button v-if="confirm_revoke !== t.token_id" size="small" label="Revoke" severity="danger"
                variant="outlined" @click="confirm_revoke = t.token_id" />
        <template v-else>
          <Button size="small" label="Revoke now" severity="danger" @click="revoke(t)" />
          <Button size="small" label="Cancel" severity="secondary" @click="confirm_revoke = null" />
        </template>
      </div>
    </div>

    <template v-if="inactive.length">
      <h4>Revoked or expired</h4>
      <div v-for="t in inactive" :key="t.token_id" class="key-item dim">
        <div class="key-main">
          <strong>{{ t.label }}</strong>
          <span class="muted">{{ t.revoked_at ? `revoked ${when(t.revoked_at)}` : `expired ${when(t.expires_at)}` }}</span>
        </div>
      </div>
    </template>
  </Dialog>
</template>

<style scoped>
.muted    { color: var(--p-text-muted-color); }
.new-key  { display: flex; flex-direction: column; gap: 0.75rem;
            padding: 0.75rem; border: 1px solid var(--p-content-border-color); border-radius: 6px; }
.field    { display: flex; flex-direction: column; gap: 0.3rem; }
.field label, .lbl { font-weight: 600; font-size: 0.9rem; }
.row      { display: flex; gap: 0.75rem; flex-wrap: wrap; }
.scope    { display: flex; align-items: center; gap: 0.5rem; font-size: 0.9rem; }
.scope label { font-weight: 400; }
.made     { margin-top: 0.75rem; display: flex; flex-direction: column; gap: 0.5rem;
            padding: 0.75rem; border: 1px solid var(--p-green-400); border-radius: 6px; }
.key-row  { display: flex; gap: 0.5rem; align-items: center; }
.key      { flex: 1; overflow-wrap: anywhere; font-size: 0.85rem; user-select: all; }
h4        { margin: 1.25rem 0 0.5rem; }
.key-item { display: grid; grid-template-columns: 1fr auto; gap: 0.2rem 0.75rem; align-items: center;
            padding: 0.5rem 0.25rem; border-bottom: 1px solid var(--p-content-border-color); }
.key-main { display: flex; gap: 0.6rem; flex-wrap: wrap; align-items: baseline; }
.key-meta { grid-column: 1; font-size: 0.8rem; }
.key-actions { grid-column: 2; grid-row: 1 / span 2; display: flex; gap: 0.4rem; }
.dim      { opacity: 0.65; }
</style>
