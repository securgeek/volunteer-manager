<script setup>
// The conference's name and public contact details: the setup wizard's step, and the same
// form inside App Settings. The contact address, website and code-of-conduct link are pushed
// to the portal with the branding and shown on the volunteer pages; each is optional, and a
// blank one is left off the pages. The server checks them too (routers/setup.py).
import { ref, watch, onMounted } from 'vue'
import InputText from 'primevue/inputtext'
import Button    from 'primevue/button'
import Message   from 'primevue/message'

const props = defineProps({
  currentTitle: String,
  setup: { type: Boolean, default: true },
})
const emit = defineEmits(['done', 'skipped'])

const title     = ref(props.currentTitle || 'Conference Scheduler')
const contact   = ref({ contact_email: '', conference_url: '', code_of_conduct_url: '' })
const error_msg = ref('')
const loading   = ref(false)
const saved     = ref(false)

watch(() => props.currentTitle, (v) => {
  title.value     = v || 'Conference Scheduler'
  error_msg.value = ''
  saved.value     = false
})

// The stored values, so App Settings edits what's there rather than blanking it.
onMounted(async () => {
  try {
    const resp = await fetch('/api/app_config')
    if (!resp.ok) return
    const cfg = await resp.json()
    for (const key of Object.keys(contact.value)) contact.value[key] = cfg[key] || ''
  } catch { /* leave the fields empty */ }
})

// with_contact false (Skip) sends the title alone, which leaves the contact fields as they are.
async function save(value, with_contact) {
  error_msg.value = ''
  loading.value   = true
  saved.value     = false
  const form = new FormData()
  form.append('title', value)
  if (with_contact) {
    form.append('with_contact', 'true')
    for (const [key, val] of Object.entries(contact.value)) form.append(key, val.trim())
  }
  try {
    const resp = await fetch('/api/setup/set_title', { method: 'POST', body: form })
    if (resp.ok) {
      const data = await resp.json()
      for (const key of Object.keys(contact.value)) contact.value[key] = data[key] || ''
      saved.value = true
      emit('done', data.title)
    } else {
      const body = await resp.json().catch(() => null)
      error_msg.value = body?.detail || 'Failed to save.'
    }
  } catch {
    error_msg.value = 'Unable to reach the server.'
  } finally {
    loading.value = false
  }
}

function submit() {
  save(title.value.trim() || 'Conference Scheduler', true)
}

// Skip saves the default title so has_title becomes true and the wizard advances.
function skip() {
  save('Conference Scheduler', false)
}
</script>

<template>
  <!-- setup wizard: full-page centered layout -->
  <div v-if="setup"
       style="display:flex; justify-content:center; align-items:center; min-height:80vh;">
    <div style="width:24rem; display:flex; flex-direction:column; gap:1rem;">
      <div style="text-align:center; margin-bottom:0.25rem;">
        <h2 style="margin:0;">Your Conference</h2>
        <p style="color:var(--p-text-muted-color); margin:0.5rem 0 0;">
          The name shown throughout the site, and how volunteers can reach you. Everything but
          the name is optional and can be changed later in App Settings.
        </p>
      </div>
      <Message v-if="error_msg" severity="error" :closable="false">{{ error_msg }}</Message>
      <div class="field">
        <label for="conf-title">Conference name</label>
        <InputText id="conf-title" v-model="title" placeholder="Conference Scheduler"
                   @keyup.enter="submit" fluid />
      </div>
      <div class="field">
        <label for="conf-email">Contact email for volunteers</label>
        <InputText id="conf-email" v-model="contact.contact_email" type="email"
                   placeholder="volunteers@example.org" fluid />
      </div>
      <div class="field">
        <label for="conf-url">Conference website</label>
        <InputText id="conf-url" v-model="contact.conference_url" type="url"
                   placeholder="https://example.org/" fluid />
      </div>
      <div class="field">
        <label for="conf-coc">Code of conduct link</label>
        <InputText id="conf-coc" v-model="contact.code_of_conduct_url" type="url"
                   placeholder="https://example.org/code-of-conduct" fluid />
      </div>
      <Button label="Save & Continue" :loading="loading" @click="submit" fluid />
      <Button label="Skip for now"    severity="secondary" :disabled="loading" @click="skip" />
    </div>
  </div>

  <!-- inline: embedded inside a parent component (e.g. AppSettings dialog) -->
  <div v-else style="display:flex; flex-direction:column; gap:0.75rem;">
    <Message v-if="error_msg" severity="error"   :closable="false">{{ error_msg }}</Message>
    <Message v-if="saved"     severity="success" :closable="false">Saved.</Message>
    <div class="field">
      <label for="app-title">Conference name</label>
      <InputText id="app-title" v-model="title" placeholder="Conference Scheduler" fluid
                 @keyup.enter="submit" />
    </div>
    <div class="field">
      <label for="app-email">Contact email for volunteers</label>
      <InputText id="app-email" v-model="contact.contact_email" type="email"
                 placeholder="volunteers@example.org" fluid />
    </div>
    <div class="field">
      <label for="app-url">Conference website</label>
      <InputText id="app-url" v-model="contact.conference_url" type="url"
                 placeholder="https://example.org/" fluid />
    </div>
    <div class="field">
      <label for="app-coc">Code of conduct link</label>
      <InputText id="app-coc" v-model="contact.code_of_conduct_url" type="url"
                 placeholder="https://example.org/code-of-conduct" fluid />
    </div>
    <small style="color:var(--p-text-muted-color);">
      Shown on the volunteer pages. Leave one blank to leave it off.
    </small>
    <Button label="Save" :loading="loading" @click="submit" fluid />
  </div>
</template>

<style scoped>
.field       { display: flex; flex-direction: column; gap: 0.35rem; }
.field label { font-size: 0.875rem; color: var(--p-text-muted-color); }
</style>
