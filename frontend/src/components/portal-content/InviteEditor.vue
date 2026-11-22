<script setup>
// Volunteer Portal → Invite email: the early-access invite (Send invites by email). Jinja, in
// the portal's sandboxed subset: variables, {% if %} / {% else %}, == and !=, and a few text
// filters. The body must include {{ link }}. Preview fills in invented volunteer details.
import { ref, onMounted } from 'vue'
import Button    from 'primevue/button'
import InputText from 'primevue/inputtext'
import Textarea  from 'primevue/textarea'
import Message   from 'primevue/message'
import { usePortalContent } from '../../composables/usePortalContent.js'

const { data, load, current, save, preview } = usePortalContent()

const invite   = ref(null)
const msg      = ref({ severity: '', text: '' })
const busy     = ref(false)
const rendered = ref(null)

onMounted(async () => { await load(); invite.value = current('invite') })

async function run(fn) {
  busy.value = true
  msg.value = { severity: '', text: '' }
  try { return await fn() } finally { busy.value = false }
}

async function do_preview() {
  const r = await run(() => preview('invite', invite.value))
  if (r.ok) rendered.value = r.body
  else { rendered.value = null; msg.value = { severity: 'error', text: r.body.detail || 'Preview failed.' } }
}

async function do_save() {
  const r = await run(() => save('invite', invite.value))
  msg.value = r.ok ? { severity: 'success', text: 'Saved. Invites sent from now on use it.' }
                   : { severity: 'error', text: r.body.detail || 'Saving failed.' }
}

async function do_reset() {
  const r = await run(() => save('invite', null))
  if (r.ok) {
    invite.value = current('invite')
    rendered.value = null
    msg.value = { severity: 'success', text: 'Back to the default.' }
  } else msg.value = { severity: 'error', text: r.body.detail || 'Reset failed.' }
}
</script>

<template>
  <div v-if="invite && data" class="editor">
    <p class="hint">
      Plain text, as a Jinja template. Variables:
      <code v-for="v in data.invite_vars" :key="v" v-text="`{{ ${v} }}`" />.
      Also <code v-text="'{% if … %}…{% else %}…{% endif %}'" />, <code>==</code> and <code>!=</code>,
      and the filters <code v-for="f in data.filters" :key="f">{{ f }}</code>.
      Loops, <code>set</code>, includes and anything else are refused. The body must include
      <code v-text="'{{ link }}'" />.
    </p>
    <div class="field">
      <label for="invite-subject">Subject</label>
      <InputText id="invite-subject" v-model="invite.subject" fluid />
    </div>
    <div class="field">
      <label for="invite-body">Body</label>
      <Textarea id="invite-body" v-model="invite.body" rows="14" autoResize class="mono" fluid />
    </div>

    <Message v-if="msg.text" :severity="msg.severity" :closable="false">{{ msg.text }}</Message>
    <div class="actions">
      <Button label="Preview" icon="pi pi-eye" severity="secondary" :loading="busy" @click="do_preview" />
      <Button label="Save" icon="pi pi-check" :loading="busy" @click="do_save" />
      <span class="spacer" />
      <Button label="Reset to default" severity="secondary" text :disabled="busy" @click="do_reset" />
    </div>

    <div v-if="rendered" class="preview">
      <div><strong>Subject:</strong> {{ rendered.subject }}</div>
      <pre>{{ rendered.body }}</pre>
    </div>
  </div>
</template>

<style scoped>
.editor  { display: flex; flex-direction: column; gap: 0.85rem; }
.field   { display: flex; flex-direction: column; gap: 0.35rem; }
.field label { font-size: 0.875rem; font-weight: 600; }
.mono    { font-family: ui-monospace, monospace; font-size: 0.85rem; }
.hint    { font-size: 0.8rem; color: var(--p-text-muted-color); line-height: 1.7; margin: 0; }
.hint code { margin-right: 0.25rem; }
.actions { display: flex; gap: 0.5rem; align-items: center; }
.spacer  { flex: 1; }
.preview { border: 1px dashed var(--p-content-border-color); border-radius: 4px; padding: 0.75rem 1rem;
           font-size: 0.9rem; }
.preview pre { white-space: pre-wrap; font-family: inherit; margin: 0.75rem 0 0; }
</style>
