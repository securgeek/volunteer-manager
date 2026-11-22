<script setup>
import { ref } from 'vue'
import Dialog    from 'primevue/dialog'
import Button    from 'primevue/button'
import InputText from 'primevue/inputtext'
import Password  from 'primevue/password'
import Message   from 'primevue/message'
import Tag       from 'primevue/tag'
import { passkeys_enabled, passkey_register } from '../lib/passkey.js'

// The signed-in user's own passkeys. Any role; organizers clear other people's in UserManager.

const visible = ref(false)
const items   = ref([])
const err     = ref('')
const enabled = ref(true)

const show_add   = ref(false)
const add_name   = ref('')
const add_pw     = ref('')
const add_err    = ref('')
const add_saving = ref(false)

// Inline rename, one row at a time (same shape as UserManager's password reset).
const rename_id     = ref(null)
const rename_val    = ref('')
const rename_err    = ref('')
const rename_saving = ref(false)

async function load() {
  err.value = ''
  try {
    const resp = await fetch('/api/passkeys')
    if (resp.ok) items.value = await resp.json()
    else err.value = 'Failed to load passkeys.'
  } catch { err.value = 'Failed to load passkeys.' }
}

async function open() {
  err.value      = ''
  add_err.value  = ''
  show_add.value = false
  add_pw.value   = ''
  rename_id.value = null
  visible.value  = true
  enabled.value  = await passkeys_enabled()
  load()
}
defineExpose({ open })

function fmt_date(iso) {
  return iso ? new Date(iso).toLocaleString() : 'Never'
}

function toggle_add() {
  show_add.value = !show_add.value
  add_err.value  = ''
  add_pw.value   = ''
  add_name.value = ''
}

async function add() {
  add_err.value = ''
  if (!add_pw.value) { add_err.value = 'Enter your current password.'; return }
  add_saving.value = true
  try {
    items.value.unshift(await passkey_register(add_pw.value, add_name.value.trim()))
    show_add.value = false
  } catch (e) {
    if (!e.cancelled) add_err.value = e.message
  } finally {
    add_pw.value     = ''
    add_saving.value = false
  }
}

function toggle_rename(item) {
  rename_err.value = ''
  if (rename_id.value === item.id) { rename_id.value = null; return }
  rename_id.value  = item.id
  rename_val.value = item.name
}

async function do_rename() {
  rename_err.value = ''
  if (!rename_val.value.trim()) { rename_err.value = 'Name can\'t be blank.'; return }
  rename_saving.value = true
  try {
    const fd = new FormData()
    fd.append('credential_id', rename_id.value)
    fd.append('name', rename_val.value)
    const resp = await fetch('/api/passkeys/rename', { method: 'POST', body: fd })
    const body = await resp.json().catch(() => null)
    if (resp.ok) {
      items.value = items.value.map(p => p.id === body.id ? body : p)
      rename_id.value = null
    } else {
      rename_err.value = body?.detail || 'Failed to rename passkey.'
    }
  } catch {
    rename_err.value = 'Unable to reach the server.'
  } finally { rename_saving.value = false }
}

async function remove(item) {
  if (!confirm(`Remove passkey "${item.name}"?\n\nYour device will still offer it, but it will ` +
               `no longer sign you in here. You can remove it from the device separately.`)) return
  const fd = new FormData()
  fd.append('credential_id', item.id)
  const resp = await fetch('/api/passkeys/delete', { method: 'POST', body: fd })
  if (resp.ok) {
    items.value = items.value.filter(p => p.id !== item.id)
  } else {
    const body = await resp.json().catch(() => null)
    err.value = body?.detail || 'Failed to remove passkey.'
  }
}
</script>

<template>
  <Dialog v-model:visible="visible" header="My Passkeys" :style="{ width: '40rem' }" modal>

    <Message v-if="!enabled" severity="warn" :closable="false" style="margin-bottom:0.75rem;">
      Passkeys aren't available on this address. Open the site at its https address to add or
      use one.
    </Message>
    <Message v-if="err" severity="error" :closable="false" style="margin-bottom:0.75rem;">
      {{ err }}
    </Message>

    <p class="muted intro">
      A passkey signs you in with your fingerprint, face, or device PIN instead of your
      password. Your password keeps working.
    </p>

    <div class="pk-list">
      <div v-if="!items.length" class="muted empty">No passkeys yet.</div>
      <div v-for="item in items" :key="item.id" class="pk-block">
        <div class="pk-row">
          <div class="pk-main">
            <span class="pk-name">{{ item.name }}</span>
            <span class="pk-tags">
              <Tag v-if="item.provider" severity="contrast" :value="item.provider" />
              <Tag v-if="item.post_quantum" severity="success" :value="`Post-quantum · ${item.alg}`" />
              <Tag v-else severity="secondary" :value="item.alg" />
              <Tag v-if="item.backed_up" severity="info" value="Synced" />
            </span>
            <span class="muted small">
              Added {{ fmt_date(item.created_at) }} · Last used {{ fmt_date(item.last_used_at) }}
            </span>
          </div>
          <Button size="small" icon="pi pi-pencil" severity="secondary" variant="text"
                  v-tooltip.top="'Rename'"
                  :class="{ 'active-action': rename_id === item.id }"
                  @click="toggle_rename(item)" />
          <Button size="small" icon="pi pi-trash" severity="danger" variant="text"
                  v-tooltip.top="'Remove passkey'" @click="remove(item)" />
        </div>

        <div v-if="rename_id === item.id" class="rename-form">
          <InputText v-model="rename_val" size="small" maxlength="60" style="width:14rem;"
                     @keyup.enter="do_rename" @keyup.escape="rename_id = null" />
          <Button size="small" label="Save" :loading="rename_saving" @click="do_rename" />
          <Button size="small" label="Cancel" severity="secondary" @click="rename_id = null" />
          <span v-if="rename_err" class="rename-err">{{ rename_err }}</span>
        </div>
      </div>
    </div>

    <div class="add-section">
      <Button size="small" :icon="show_add ? 'pi pi-minus' : 'pi pi-plus'"
              :label="show_add ? 'Cancel' : 'Add Passkey'" severity="secondary"
              :disabled="!enabled" @click="toggle_add" />

      <div v-if="show_add" class="add-form">
        <Message v-if="add_err" severity="error" :closable="false" style="margin-bottom:0.5rem;">
          {{ add_err }}
        </Message>
        <div class="add-grid">
          <InputText v-model="add_name" placeholder="Name (optional)" size="small"
                     maxlength="60" style="width:12rem;" />
          <Password v-model="add_pw" placeholder="Current password" :feedback="false"
                    size="small" @keyup.enter="add" />
          <Button size="small" label="Create Passkey" icon="pi pi-shield"
                  :loading="add_saving" @click="add" />
        </div>
        <p class="muted small">
          Your password is needed to add a passkey. Your browser or password manager then
          asks where to save it. Leave the name blank to name it after the passkey manager;
          you can rename it later.
        </p>
      </div>
    </div>

  </Dialog>
</template>

<style scoped>
.muted   { color: var(--p-text-muted-color); }
.small   { font-size: 0.8rem; }
.intro   { margin: 0 0 0.75rem; font-size: 0.88rem; }
.empty   { padding: 0.5rem 0.25rem; font-size: 0.88rem; }
.pk-list { display: flex; flex-direction: column; margin-bottom: 1rem; }
.pk-block { border-bottom: 1px solid var(--p-content-border-color); }
.pk-row  { display: flex; align-items: center; gap: 0.25rem; padding: 0.5rem 0.25rem; }
.pk-main { display: flex; flex-direction: column; gap: 0.25rem; flex: 1; min-width: 0; }
.pk-name { font-weight: 500; }
.pk-tags { display: flex; gap: 0.35rem; flex-wrap: wrap; }
.add-form { margin-top: 0.75rem; }
.rename-form {
  display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap;
  padding: 0 0.25rem 0.6rem 0.25rem;
}
.rename-err    { font-size: 0.82rem; color: var(--p-red-400); }
.active-action { color: var(--p-primary-color) !important; }
.add-grid { display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: flex-start; }
</style>
