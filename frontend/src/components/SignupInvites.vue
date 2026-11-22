<script setup>
// Early-access invites (backend routers/portal_admin.py): single-use links that let one person
// sign up on the portal while the call for volunteers is closed. They still land in Pending
// Volunteers. The link is shown once, when it's made; only its hash is kept anywhere.
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useInviteBatch, GAP_SEC, BATCH_MAX } from '../composables/useInviteBatch.js'
import Button    from 'primevue/button'
import InputText from 'primevue/inputtext'
import Message   from 'primevue/message'
import Tag       from 'primevue/tag'
import Textarea  from 'primevue/textarea'
import ProgressBar from 'primevue/progressbar'

const invites   = ref([])
const label     = ref('')
const days      = ref('14')
const busy      = ref(false)
const error_msg = ref('')
const made      = ref(null)     // { link, label, expires_at } — the one time the link exists
const copied    = ref(false)

const STATE_TAG = {
  unused:  { severity: 'info',      value: 'Unused' },
  used:    { severity: 'success',   value: 'Used' },
  expired: { severity: 'secondary', value: 'Expired' },
  revoked: { severity: 'secondary', value: 'Revoked' },
}

async function load() {
  try {
    const resp = await fetch('/api/admin/portal/invites')
    if (resp.ok) invites.value = await resp.json()
  } catch { /* the list just stays as it was */ }
}

async function create() {
  busy.value = true; error_msg.value = ''; made.value = null; copied.value = false
  const fd = new FormData()
  fd.append('label', label.value)
  fd.append('days', days.value)
  try {
    const resp = await fetch('/api/admin/portal/invites', { method: 'POST', body: fd })
    const body = await resp.json().catch(() => null)
    if (resp.ok) {
      made.value = { ...body, label: label.value.trim() }
      label.value = ''
      await load()
    } else {
      error_msg.value = body?.detail || 'Failed to create the invite.'
    }
  } catch {
    error_msg.value = 'Unable to reach the server.'
  } finally {
    busy.value = false
  }
}

async function copy() {
  try {
    await navigator.clipboard.writeText(made.value.link)
    copied.value = true
  } catch {
    error_msg.value = "Couldn't copy. Select the link and copy it by hand."
  }
}

async function revoke(inv) {
  error_msg.value = ''
  const fd = new FormData()
  fd.append('invite_id', inv.invite_id)
  try {
    const resp = await fetch('/api/admin/portal/invites/revoke', { method: 'POST', body: fd })
    if (!resp.ok) {
      const body = await resp.json().catch(() => null)
      error_msg.value = body?.detail || 'Failed to revoke the invite.'
    }
    await load()
  } catch {
    error_msg.value = 'Unable to reach the server.'
  }
}

const day = (iso) => (iso ? new Date(iso).toLocaleDateString() : '—')

// ── Send invites: one email per person, paced ────────────────────────────────
// The sending loop lives in composables/useInviteBatch.js, outside this component, so it
// survives the dialog closing. This side parses the box and shows the progress.
const ADDR = /^[^@\s]+@[^@\s]+\.[^@\s]+$/
const batch_text = ref('')
const { rows, running, paused, stopping, countdown, done, sent_n, failed_n, percent,
        start, stop, toggle_pause, clear, unsent_lines, set_finished } = useInviteBatch()

// "Jane Smith, jane@example.org", or the address first, or "Jane Smith <jane@…>".
function parse_line(line) {
  const parts = line.replace(/[<>]/g, ' ').split(/[,;\t]|\s+/).map(x => x.trim()).filter(Boolean)
  const email = parts.find(x => x.includes('@')) || ''
  const name  = parts.filter(x => x !== email).join(' ')
  return { name, email: email.toLowerCase() }
}

const parsed = computed(() => {
  const seen = new Set()
  return batch_text.value.split('\n').map(l => l.trim()).filter(Boolean).map(line => {
    const { name, email } = parse_line(line)
    let problem = ''
    if (!name) problem = 'no name'
    else if (!ADDR.test(email)) problem = 'no valid email address'
    else if (seen.has(email)) problem = 'address listed twice'
    seen.add(email)
    return { line, name, email, problem }
  })
})
const bad_lines = computed(() => parsed.value.filter(r => r.problem))
const can_send  = computed(() => parsed.value.length > 0 && !bad_lines.value.length
                                 && parsed.value.length <= BATCH_MAX && !running.value)
const minutes   = computed(() => Math.ceil(Math.max(0, parsed.value.length - 1) * GAP_SEC / 60))

async function finished() {
  if (!rows.value.some(r => r.state !== 'sent')) batch_text.value = ''
  await load()
}

function start_batch() {
  start(parsed.value.map(({ name, email }) => ({ name, email })), days.value, finished)
}

function keep_unsent() {
  batch_text.value = unsent_lines()
  clear()
}

onMounted(() => set_finished(finished))
onUnmounted(() => set_finished(null))

onMounted(load)
</script>

<template>
  <div class="invites">
    <div class="hdr"><strong>Early access invites</strong></div>
    <div class="muted">
      A single-use link that lets one person sign up while signup is closed. They still wait
      in Pending Volunteers for approval. The link is shown once: copy it and send it to them.
    </div>

    <Message v-if="error_msg" severity="error" :closable="false">{{ error_msg }}</Message>

    <div class="row">
      <InputText v-model="label" placeholder="Who it's for, e.g. Jane, room lead" maxlength="80"
                 size="small" class="label" @keyup.enter="create" />
      <label for="inv-days" class="muted">Expires in</label>
      <InputText id="inv-days" v-model="days" size="small" inputmode="numeric" style="width:3.5rem;" />
      <span class="muted">days</span>
      <Button size="small" label="Create" icon="pi pi-plus" :loading="busy"
              :disabled="!label.trim()" @click="create" />
    </div>

    <Message v-if="made" severity="success" :closable="false">
      <div>Invite for <strong>{{ made.label }}</strong>, until {{ day(made.expires_at) }}. This link
        won't be shown again:</div>
      <div class="link-row">
        <InputText :modelValue="made.link" readonly size="small" class="link"
                   @focus="e => e.target.select()" />
        <Button size="small" :label="copied ? 'Copied' : 'Copy'"
                :icon="copied ? 'pi pi-check' : 'pi pi-copy'" @click="copy" />
      </div>
    </Message>

    <details class="batch" :open="running || rows.length > 0">
      <summary><strong>Send invites by email</strong></summary>
      <div class="muted">
        One person per line: <code>Name, email@address</code>. Each gets their own invite by
        email, one every {{ GAP_SEC }} seconds<template v-if="parsed.length > 1"> ({{ parsed.length }}
        take about {{ minutes }} min)</template>. It keeps sending if you close this dialog;
        closing the browser tab stops it. Addresses are used to send and not kept. Up to {{ BATCH_MAX }} at a time.
      </div>
      <Textarea v-model="batch_text" rows="5" autoResize :disabled="running" class="batch-box"
                placeholder="Jane Smith, jane@example.org&#10;Sam Lee, sam@example.com" />
      <div v-if="bad_lines.length" class="bad">
        <div v-for="b in bad_lines" :key="b.line">“{{ b.line }}”: {{ b.problem }}</div>
      </div>
      <div v-if="parsed.length > BATCH_MAX" class="bad">
        {{ parsed.length }} lines; send at most {{ BATCH_MAX }} at a time.
      </div>
      <div class="row">
        <span class="muted">Invites expire in {{ days }} days (set above).</span>
        <span class="spacer" />
        <Button v-if="!running" size="small" icon="pi pi-send"
                :label="`Send ${parsed.length || ''} invite${parsed.length === 1 ? '' : 's'}`"
                :disabled="!can_send" @click="start_batch" />
        <template v-else>
          <Button size="small" severity="secondary" :icon="paused ? 'pi pi-play' : 'pi pi-pause'"
                  :label="paused ? 'Resume' : 'Pause'" @click="toggle_pause" />
          <Button size="small" severity="danger" variant="outlined" icon="pi pi-stop"
                  label="Stop" :disabled="stopping" @click="stop" />
        </template>
      </div>

      <template v-if="rows.length">
        <ProgressBar :value="percent" :showValue="false" style="height:0.6rem;" />
        <div class="muted">
          {{ sent_n }} sent<template v-if="failed_n">, {{ failed_n }} failed</template>
          of {{ rows.length }}.
          <template v-if="running && paused"> Paused.</template>
          <template v-else-if="running && countdown"> Next in {{ countdown }}s.</template>
          <template v-else-if="!running && done < rows.length"> Stopped.</template>
          <template v-else-if="!running"> Done.</template>
        </div>
        <table class="list">
          <tbody>
            <tr v-for="r in rows" :key="r.email">
              <td>{{ r.name }}</td>
              <td class="muted">{{ r.email }}</td>
              <td>
                <Tag v-if="r.state === 'sent'" severity="success" value="Sent" />
                <Tag v-else-if="r.state === 'failed'" severity="danger" value="Failed" />
                <Tag v-else-if="r.state === 'sending'" severity="info" value="Sending" />
                <Tag v-else severity="secondary" value="Waiting" />
              </td>
              <td class="muted">{{ r.note }}</td>
            </tr>
          </tbody>
        </table>
        <div v-if="!running && (failed_n || done < rows.length)" class="row">
          <Button size="small" severity="secondary" label="Put the unsent back to try again"
                  @click="keep_unsent" />
        </div>
      </template>
    </details>

    <table v-if="invites.length" class="list">
      <tbody>
        <tr v-for="inv in invites" :key="inv.invite_id">
          <td>{{ inv.label }}</td>
          <td><Tag v-bind="STATE_TAG[inv.state]" /></td>
          <td class="muted">
            <i v-if="inv.emailed_at" class="pi pi-envelope" title="Emailed" />
            <template v-if="inv.state === 'used'">by {{ inv.used_by }}, {{ day(inv.used_at) }}</template>
            <template v-else-if="inv.state === 'unused'">until {{ day(inv.expires_at) }}</template>
          </td>
          <td class="act">
            <Button v-if="inv.state === 'unused'" size="small" variant="text" severity="danger"
                    label="Revoke" @click="revoke(inv)" />
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.invites { display: flex; flex-direction: column; gap: 0.5rem; font-size: 0.85rem; }
.row, .link-row { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
.label   { flex: 1; min-width: 12rem; }
.link    { flex: 1; min-width: 16rem; font-family: monospace; font-size: 0.75rem; }
.link-row { margin-top: 0.4rem; }
.muted   { color: var(--p-text-muted-color); font-size: 0.8rem; }
.list    { width: 100%; border-collapse: collapse; }
.list td { padding: 0.3rem 0.4rem; border-bottom: 1px solid var(--p-content-border-color); }
.act     { text-align: right; }
.batch   { display: flex; flex-direction: column; gap: 0.5rem; border: 1px solid var(--p-content-border-color);
           border-radius: 6px; padding: 0.5rem 0.75rem; }
.batch summary { cursor: pointer; }
.batch[open] > * + * { margin-top: 0.5rem; }
.batch-box { width: 100%; font-family: monospace; font-size: 0.8rem; }
.bad     { color: var(--p-red-400); font-size: 0.8rem; }
.spacer  { flex: 1; }
</style>
