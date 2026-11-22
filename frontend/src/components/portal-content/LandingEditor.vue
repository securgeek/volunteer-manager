<script setup>
// Volunteer Portal → Landing page: the public call for volunteers at "/". Three Markdown parts
// around the fixed buttons, plus the opportunity cards. The portal renders it; Preview shows
// the same render (backend portal_content.py is the portal's rules, copied).
import { ref, onMounted } from 'vue'
import Button    from 'primevue/button'
import InputText from 'primevue/inputtext'
import Textarea  from 'primevue/textarea'
import Select    from 'primevue/select'
import Message   from 'primevue/message'
import { usePortalContent } from '../../composables/usePortalContent.js'

const { data, load, current, save, preview } = usePortalContent()

const page     = ref(null)
const msg      = ref({ severity: '', text: '' })
const busy     = ref(false)
const rendered = ref(null)

onMounted(async () => { await load(); page.value = current('landing') })

const PARTS = [
  { key: 'intro',   label: 'Intro',   hint: 'Above the two buttons. The first paragraph is shown large.' },
  { key: 'main',    label: 'Main column', hint: 'Below the opportunity cards.' },
  { key: 'sidebar', label: 'Sidebar', hint: 'The box on the right. Leave it empty for none.' },
]

function add_role() {
  page.value.roles.push({ icon: 'users', name: '', text: '' })
}
function move(i, d) {
  const r = page.value.roles
  const j = i + d
  if (j < 0 || j >= r.length) return
  ;[r[i], r[j]] = [r[j], r[i]]
}

async function do_preview() {
  busy.value = true
  msg.value = { severity: '', text: '' }
  const r = await preview('landing', page.value)
  busy.value = false
  if (r.ok) rendered.value = r.body
  else { rendered.value = null; msg.value = { severity: 'error', text: r.body.detail || 'Preview failed.' } }
}

async function do_save() {
  busy.value = true
  const r = await save('landing', page.value)
  busy.value = false
  msg.value = r.ok ? { severity: 'success', text: 'Saved. The portal has it within a few seconds.' }
                   : { severity: 'error', text: r.body.detail || 'Saving failed.' }
}

async function do_reset() {
  busy.value = true
  const r = await save('landing', null)
  busy.value = false
  if (r.ok) {
    page.value = current('landing')
    rendered.value = null
    msg.value = { severity: 'success', text: 'Back to the default.' }
  } else msg.value = { severity: 'error', text: r.body.detail || 'Reset failed.' }
}
</script>

<template>
  <div v-if="page && data" class="editor">
    <p class="hint">
      Markdown, with these placeholders from App Settings:
      <code v-for="v in data.landing_vars" :key="v" v-text="`{{ ${v} }}`" />.
      Use <code v-text="'{% if contact_email %}…{% endif %}'" /> to leave a sentence out when a value
      is blank. No raw HTML or images; links to web pages and email addresses only.
    </p>

    <div v-for="p in PARTS" :key="p.key" class="field">
      <label :for="`landing-${p.key}`">{{ p.label }} <span class="muted">— {{ p.hint }}</span></label>
      <Textarea :id="`landing-${p.key}`" v-model="page[p.key]" rows="6" autoResize class="mono" fluid />
    </div>

    <div class="field">
      <label for="roles-heading">Opportunity cards <span class="muted">— above the main column</span></label>
      <InputText id="roles-heading" v-model="page.roles_heading" placeholder="Heading" fluid />
    </div>
    <div v-for="(r, i) in page.roles" :key="i" class="role">
      <Select v-model="r.icon" :options="data.icons" class="icon-pick" aria-label="Icon">
        <template #value="{ value }"><i :class="`pi pi-${value}`" /> </template>
        <template #option="{ option }"><i :class="`pi pi-${option}`" /> {{ option }}</template>
      </Select>
      <InputText v-model="r.name" placeholder="Name" class="role-name" />
      <InputText v-model="r.text" placeholder="One line about it" class="role-text" />
      <Button icon="pi pi-arrow-up" text size="small" aria-label="Up" @click="move(i, -1)" />
      <Button icon="pi pi-arrow-down" text size="small" aria-label="Down" @click="move(i, 1)" />
      <Button icon="pi pi-trash" text size="small" severity="danger" aria-label="Remove"
              @click="page.roles.splice(i, 1)" />
    </div>
    <div><Button label="Add a card" icon="pi pi-plus" size="small" text
                 :disabled="page.roles.length >= 12" @click="add_role" /></div>

    <Message v-if="msg.text" :severity="msg.severity" :closable="false">{{ msg.text }}</Message>
    <div class="actions">
      <Button label="Preview" icon="pi pi-eye" severity="secondary" :loading="busy" @click="do_preview" />
      <Button label="Save" icon="pi pi-check" :loading="busy" @click="do_save" />
      <span class="spacer" />
      <Button label="Reset to default" severity="secondary" text :disabled="busy"
              @click="do_reset" />
    </div>

    <!-- The backend's render of the organizer's own text: no raw HTML can survive it. -->
    <div v-if="rendered" class="preview">
      <!-- eslint-disable-next-line vue/no-v-html -->
      <div v-html="rendered.intro" />
      <h3 v-if="rendered.roles.length && rendered.roles_heading">{{ rendered.roles_heading }}</h3>
      <ul v-if="rendered.roles.length" class="preview-roles">
        <li v-for="r in rendered.roles" :key="r.name">
          <i :class="`pi pi-${r.icon}`" /> <strong>{{ r.name }}</strong> {{ r.text }}
        </li>
      </ul>
      <!-- eslint-disable-next-line vue/no-v-html -->
      <div v-html="rendered.main" />
      <!-- eslint-disable-next-line vue/no-v-html -->
      <aside v-if="rendered.sidebar.trim()" class="preview-side" v-html="rendered.sidebar" />
    </div>
  </div>
</template>

<style scoped>
.editor  { display: flex; flex-direction: column; gap: 0.85rem; }
.field   { display: flex; flex-direction: column; gap: 0.35rem; }
.field label { font-size: 0.875rem; font-weight: 600; }
.muted   { font-weight: 400; color: var(--p-text-muted-color); }
.mono    { font-family: ui-monospace, monospace; font-size: 0.85rem; }
.hint    { font-size: 0.8rem; color: var(--p-text-muted-color); line-height: 1.6; margin: 0; }
.hint code { margin-right: 0.3rem; }
.role    { display: flex; gap: 0.4rem; align-items: center; flex-wrap: wrap; }
.icon-pick { width: 4.5rem; }
.role-name { width: 11rem; }
.role-text { flex: 1; min-width: 12rem; }
.actions { display: flex; gap: 0.5rem; align-items: center; }
.spacer  { flex: 1; }
.preview { border: 1px dashed var(--p-content-border-color); border-radius: 4px; padding: 0.5rem 1rem;
           font-size: 0.9rem; line-height: 1.55; }
.preview-roles { list-style: none; padding: 0; display: grid; gap: 0.3rem; }
.preview-side  { border-left: 3px solid var(--p-primary-color); padding-left: 0.75rem; }
</style>
