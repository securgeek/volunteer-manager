<script setup>
import { ref, computed, watch, useTemplateRef, nextTick } from 'vue'
import Dialog    from 'primevue/dialog'
import Button    from 'primevue/button'
import InputText from 'primevue/inputtext'
import Textarea  from 'primevue/textarea'
import Select    from 'primevue/select'
import Message   from 'primevue/message'
import Tabs      from 'primevue/tabs'
import TabList   from 'primevue/tablist'
import Tab       from 'primevue/tab'

// Call for Volunteers → Schedule Email: the template tools/vol_mail sends from.
//
// Markdown on the left; on the right, the email exactly as one volunteer would get it, with
// their real shifts and check-in codes, rendered by the server (backend email_render.py) from
// the text as it stands here, saved or not. The HTML is shown in a sandboxed iframe with no
// permissions, so nothing in it can run. Download package zips the *saved* template; this
// site never sends the mail itself.

const visible = ref(false)

const subject  = ref('')
const body_md  = ref('')
const shift_md = ref('')
const saved    = ref({ subject: '', body_md: '', shift_md: '' })
const meta     = ref({ updated_by: null, updated_at: null, is_default: true, placeholders: {} })
const assets   = ref([])
const missing  = ref([])
const problems = ref([])

const err  = ref('')
const msg  = ref('')
const busy = ref(false)

const recipients = ref([])
const vol_id     = ref(null)
const preview    = ref(null)    // { subject, html, text }
const view       = ref('html')
const prev_err   = ref('')

const dirty = computed(() => subject.value !== saved.value.subject ||
  body_md.value !== saved.value.body_md || shift_md.value !== saved.value.shift_md)
const without_email = computed(() => recipients.value.filter(r => !r.has_email).length)
const recipient_options = computed(() => recipients.value.map(r => ({
  ...r, label: `${r.name} · ${r.shift_count} shift${r.shift_count === 1 ? '' : 's'}`
                + (r.has_email ? '' : ' · no email, will be skipped') })))

async function load() {
  err.value = ''
  const [t, rc] = await Promise.all([fetch('/api/email_template'), fetch('/api/email_template/recipients')])
  if (!t.ok) { err.value = 'Could not load the template.'; return }
  const body = await t.json()
  subject.value = body.subject; body_md.value = body.body_md; shift_md.value = body.shift_md
  saved.value = { subject: body.subject, body_md: body.body_md, shift_md: body.shift_md }
  meta.value = body
  assets.value = body.assets
  missing.value = body.missing_assets
  problems.value = body.problems
  recipients.value = rc.ok ? await rc.json() : []
  if (!recipients.value.some(r => r.vol_id === vol_id.value))
    vol_id.value = (recipients.value.find(r => r.has_email) || recipients.value[0])?.vol_id ?? null
  refresh_preview()
}

function open() {
  msg.value = ''
  visible.value = true
  load()
}

// ── preview, refreshed a moment after typing stops ───────────────────────────
let timer = null
watch([subject, body_md, shift_md], () => {
  clearTimeout(timer)
  timer = setTimeout(refresh_preview, 700)
})
watch(vol_id, () => refresh_preview())

async function refresh_preview() {
  if (!visible.value || !vol_id.value) { preview.value = null; return }
  prev_err.value = ''
  const fd = new FormData()
  fd.append('vol_id', vol_id.value)
  fd.append('subject', subject.value)
  fd.append('body_md', body_md.value)
  fd.append('shift_md', shift_md.value)
  const r    = await fetch('/api/email_template/preview', { method: 'POST', body: fd })
  const body = await r.json().catch(() => null)
  if (!r.ok) { prev_err.value = body?.detail || 'Preview failed.'; return }
  preview.value = body
  problems.value = body.problems
  missing.value = body.missing_assets
}

// ── save / download ───────────────────────────────────────────────────────────
async function save() {
  err.value = ''; msg.value = ''
  busy.value = true
  try {
    const fd = new FormData()
    fd.append('subject', subject.value)
    fd.append('body_md', body_md.value)
    fd.append('shift_md', shift_md.value)
    const r    = await fetch('/api/email_template', { method: 'POST', body: fd })
    const body = await r.json().catch(() => null)
    if (!r.ok) { err.value = body?.detail || 'Could not save.'; return }
    saved.value = { subject: subject.value, body_md: body_md.value, shift_md: shift_md.value }
    missing.value = body.missing_assets
    msg.value = 'Saved.'
    meta.value = { ...meta.value, is_default: false }
  } finally {
    busy.value = false
  }
}

async function download() {
  err.value = ''; msg.value = ''
  const r = await fetch('/api/email_template/package')
  if (!r.ok) {
    err.value = (await r.json().catch(() => null))?.detail || 'Could not build the package.'
    return
  }
  const name = /filename="([^"]+)"/.exec(r.headers.get('content-disposition') || '')?.[1]
               || 'schedule-email.zip'
  const url = URL.createObjectURL(await r.blob())
  const a = document.createElement('a')
  a.href = url; a.download = name
  document.body.appendChild(a); a.click(); a.remove()
  URL.revokeObjectURL(url)
  msg.value = `Downloaded ${name}. Use it with tools/vol_mail.`
}

// ── images ────────────────────────────────────────────────────────────────────
const file_input = useTemplateRef('file_input')
const body_area  = useTemplateRef('body_area')
let body_cursor  = null

function remember_cursor(ev) { body_cursor = ev.target.selectionStart }

async function upload(ev) {
  const f = ev.target.files?.[0]
  ev.target.value = ''
  if (!f) return
  err.value = ''
  const fd = new FormData()
  fd.append('file', f)
  const r    = await fetch('/api/email_template/assets', { method: 'POST', body: fd })
  const body = await r.json().catch(() => null)
  if (!r.ok) { err.value = body?.detail || 'Upload failed.'; return }
  await reload_assets()
  insert_image(body.name)
}

async function add_site_logo() {
  err.value = ''
  const r    = await fetch('/api/email_template/assets/site_logo', { method: 'POST' })
  const body = await r.json().catch(() => null)
  if (!r.ok) { err.value = body?.detail || 'Could not add the site logo.'; return }
  await reload_assets()
  insert_image(body.name)
}

async function remove_asset(name) {
  const fd = new FormData()
  fd.append('name', name)
  await fetch('/api/email_template/assets/delete', { method: 'POST', body: fd })
  await reload_assets()
}

async function reload_assets() {
  const r = await fetch('/api/email_template')
  if (r.ok) assets.value = (await r.json()).assets
  refresh_preview()
}

function insert_image(name) {
  if (body_md.value.includes(`asset:${name})`)) return
  const snippet = `![${name.replace(/\.[a-z]+$/, '')}](asset:${name})`
  const at = body_cursor ?? 0
  body_md.value = body_md.value.slice(0, at) + snippet + '\n\n' + body_md.value.slice(at)
  nextTick(() => body_area.value?.$el?.focus?.())
}

function when(iso) {
  return iso ? new Date(iso).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : ''
}

const tags = (list) => (list || []).map(p => `{{${p}}}`).join('  ')

defineExpose({ open })
</script>

<template>
  <Dialog v-model:visible="visible" header="Schedule Email" modal maximizable
          :style="{ width: '92vw', maxWidth: '84rem' }">
    <div class="status">
      <span v-if="meta.is_default" class="muted">The built-in starting template; not saved yet.</span>
      <span v-else-if="meta.updated_at" class="muted">Saved by {{ meta.updated_by }}, {{ when(meta.updated_at) }}.</span>
      <span v-if="dirty" class="unsaved">Unsaved changes</span>
      <span style="flex:1;" />
      <Button label="Save" icon="pi pi-save" :loading="busy" :disabled="!dirty" @click="save()" />
      <Button label="Download package" icon="pi pi-download" severity="secondary"
              :disabled="dirty" v-tooltip.bottom="dirty ? 'Save first: the package is the saved template' : null"
              @click="download()" />
    </div>
    <Message v-if="err" severity="error" :closable="false" class="msg">{{ err }}</Message>
    <Message v-if="msg" severity="success" :closable="false" class="msg">{{ msg }}</Message>
    <Message v-if="problems.length" severity="warn" :closable="false" class="msg">
      <div v-for="p in problems" :key="p">{{ p }}</div>
    </Message>
    <Message v-if="missing.length" severity="warn" :closable="false" class="msg">
      Images not uploaded yet: {{ missing.join(', ') }}
    </Message>

    <div class="cols">
      <!-- Editor ───────────────────────────────────────────── -->
      <div class="editor">
        <label for="et-subject" class="lbl">Subject</label>
        <InputText id="et-subject" v-model="subject" fluid />
        <small class="muted">{{ tags(meta.placeholders.subject) }}</small>

        <label for="et-body" class="lbl">Body <span class="muted">(Markdown)</span></label>
        <Textarea id="et-body" ref="body_area" v-model="body_md" rows="16" autoResize fluid class="mono"
                  @click="remember_cursor" @keyup="remember_cursor" />
        <small class="muted">{{ tags(meta.placeholders.body) }}</small>

        <label for="et-shift" class="lbl">Each shift <span class="muted">(Markdown, repeated for every shift where the body says <code v-pre>{{shifts}}</code>)</span></label>
        <Textarea id="et-shift" v-model="shift_md" rows="5" autoResize fluid class="mono" />
        <small class="muted">{{ tags(meta.placeholders.shift) }}</small>

        <div class="lbl">Images</div>
        <div class="assets">
          <div v-for="a in assets" :key="a.name" class="asset">
            <img :src="`/api/email_template/assets/${encodeURIComponent(a.name)}?v=${a.sha256.slice(0, 8)}`" :alt="a.name" />
            <code>{{ a.name }}</code>
            <span class="muted">{{ Math.ceil(a.size / 1024) }} KB</span>
            <Button size="small" text icon="pi pi-plus" v-tooltip="'Insert into the body'" @click="insert_image(a.name)" />
            <Button size="small" text severity="danger" icon="pi pi-trash" v-tooltip="'Delete'" @click="remove_asset(a.name)" />
          </div>
        </div>
        <div class="row">
          <input ref="file_input" type="file" accept="image/png,image/jpeg,image/gif" class="hidden-file"
                 @change="upload" />
          <Button size="small" severity="secondary" icon="pi pi-upload" label="Upload image…"
                  @click="file_input?.click()" />
          <Button size="small" severity="secondary" icon="pi pi-image" label="Add site logo"
                  @click="add_site_logo()" />
        </div>
        <small class="muted">PNG, JPEG or GIF, up to 1 MB each. Write <code>![alt text](asset:name.png)</code> to place one.</small>
      </div>

      <!-- Preview ──────────────────────────────────────────── -->
      <div class="preview">
        <div class="row">
          <Select v-model="vol_id" :options="recipient_options" option-label="label" option-value="vol_id"
                  filter placeholder="Preview as…" style="flex:1; min-width:0;" />
        </div>
        <small class="muted">
          {{ recipients.length }} volunteer(s) with shifts<template v-if="without_email">,
          {{ without_email }} without an email (the mailer skips them)</template>.
        </small>
        <Message v-if="prev_err" severity="error" :closable="false" class="msg">{{ prev_err }}</Message>
        <template v-if="preview">
          <div class="subject"><span class="muted">Subject:</span> {{ preview.subject }}</div>
          <Tabs v-model:value="view">
            <TabList>
              <Tab value="html">HTML</Tab>
              <Tab value="text">Plain text</Tab>
            </TabList>
          </Tabs>
          <iframe v-if="view === 'html'" class="frame" sandbox="" :srcdoc="preview.html"
                  title="Email preview" referrerpolicy="no-referrer" />
          <pre v-else class="text">{{ preview.text }}</pre>
        </template>
        <p v-else-if="!recipients.length" class="muted">
          Nobody has a shift yet, so there's no one to preview as.
        </p>
      </div>
    </div>
  </Dialog>
</template>

<style scoped>
.status  { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; margin-bottom: 0.75rem; }
.unsaved { color: var(--p-orange-400); font-weight: 600; font-size: 0.9rem; }
.msg     { margin: 0 0 0.75rem; }
.muted   { color: var(--p-text-muted-color); }
.cols    { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 1.25rem; }
@media (max-width: 900px) { .cols { grid-template-columns: minmax(0, 1fr); } }
.editor, .preview { display: flex; flex-direction: column; gap: 0.4rem; min-width: 0; }
.lbl     { font-weight: 600; margin-top: 0.6rem; }
.mono    { font-family: monospace; font-size: 0.85rem; }
.row     { display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: center; }
.hidden-file { display: none; }
.assets  { display: flex; flex-direction: column; gap: 0.3rem; }
.asset   { display: flex; align-items: center; gap: 0.5rem; font-size: 0.85rem; }
.asset img { width: 3rem; height: 2rem; object-fit: contain; background: #fff;
             border: 1px solid var(--p-content-border-color); border-radius: 4px; }
.subject { font-size: 0.95rem; margin-top: 0.5rem; }
/* White like most mail apps, whatever the site theme, so the preview reads as the email will. */
.frame   { width: 100%; height: 70vh; border: 1px solid var(--p-content-border-color);
           border-radius: 6px; background: #fff; }
.text    { white-space: pre-wrap; font-size: 0.85rem; padding: 0.75rem; margin: 0; height: 70vh;
           overflow: auto; border: 1px solid var(--p-content-border-color); border-radius: 6px; }
</style>
