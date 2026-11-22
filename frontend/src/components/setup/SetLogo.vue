<script setup>
import { computed, ref, watch } from 'vue'
import Button  from 'primevue/button'
import Message from 'primevue/message'

// Two logos (backend routers/setup.py):
//   theme  — the year's theme logo, the primary one. The favicon is drawn from it, and the
//            volunteer pages show it large on the landing page and faintly behind each page.
//   header — the smaller logo in the volunteer pages' upper left. No favicon.
const props = defineProps({
  setup:       { type: Boolean, default: false },
  currentLogo: { type: String,  default: null  },
  variant:     { type: String,  default: 'theme',
                 validator: v => ['theme', 'header'].includes(v) },
})
const emit = defineEmits(['done', 'skipped'])

const is_theme = computed(() => props.variant === 'theme')
const endpoint = computed(() => (is_theme.value ? '/api/app_logo' : '/api/app_header_logo'))
const heading  = computed(() => (is_theme.value ? 'Theme Logo' : 'Header Logo'))
const help     = computed(() => (is_theme.value
  ? "This year's theme logo. It's shown in the app header and large on the volunteer pages, and it becomes the browser favicon."
  : 'The smaller logo in the upper left of the volunteer pages, as on most conference sites. Without one, they use the theme logo.'))

// The upload limit (backend routers/setup.py MAX_LOGO_BYTES). Checked here too, so an
// oversized pick is refused at once instead of after the upload.
const MAX_LOGO_BYTES = 2_000_000

const current_logo = ref(props.currentLogo)
const pending_logo = ref(null)   // data URL, for the preview only
const pending_file = ref(null)   // the File itself, which is what gets uploaded
const pending_fav  = ref(null)
const saving       = ref(false)
const error_msg    = ref('')
const saved        = ref(false)

const file_input = ref(null)

watch(() => props.currentLogo, (v) => {
  current_logo.value = v || null
  pending_logo.value = null
  pending_file.value = null
  pending_fav.value  = null
  error_msg.value    = ''
  saved.value        = false
})

function pick_file() {
  file_input.value?.click()
}

function on_file_change(event) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file) return
  error_msg.value = ''
  if (file.size > MAX_LOGO_BYTES) {
    const mb = (file.size / 1_000_000).toFixed(1)
    error_msg.value = `That image is ${mb} MB; the limit is 2 MB. Use a smaller one.`
    return
  }
  pending_file.value = file
  const reader = new FileReader()
  reader.onload = (e) => {
    pending_logo.value = e.target.result
    if (is_theme.value) generate_favicon(e.target.result)
  }
  reader.readAsDataURL(file)
}

function generate_favicon(data_url) {
  const img = new Image()
  img.onload = () => {
    const canvas = document.createElement('canvas')
    canvas.width  = 32
    canvas.height = 32
    canvas.getContext('2d').drawImage(img, 0, 0, 32, 32)
    pending_fav.value = canvas.toDataURL('image/png')
  }
  img.src = data_url
}

async function save_logo() {
  saving.value    = true
  error_msg.value = ''
  saved.value     = false
  // The image goes as a file part, not a data-URL field: the server caps plain fields at 1 MB.
  const form = new FormData()
  form.append('logo', pending_file.value)
  if (is_theme.value) form.append('favicon', pending_fav.value || '')
  try {
    const resp = await fetch(endpoint.value, { method: 'POST', body: form })
    if (resp.ok) {
      current_logo.value = pending_logo.value
      emit('done', { logo: pending_logo.value, favicon: pending_fav.value })
      pending_logo.value = null
      pending_file.value = null
      pending_fav.value  = null
      saved.value        = true
    } else {
      const body = await resp.json().catch(() => null)
      error_msg.value = body?.detail || 'Failed to save logo.'
    }
  } catch {
    error_msg.value = 'Unable to reach the server.'
  } finally {
    saving.value = false
  }
}

async function remove_logo() {
  saving.value    = true
  error_msg.value = ''
  const form = new FormData()
  form.append('remove', 'true')
  try {
    const resp = await fetch(endpoint.value, { method: 'POST', body: form })
    if (resp.ok) {
      current_logo.value = null
      pending_logo.value = null
      pending_file.value = null
      pending_fav.value  = null
      emit('done', { logo: null, favicon: null })
    } else {
      const body = await resp.json().catch(() => null)
      error_msg.value = body?.detail || 'Failed to remove logo.'
    }
  } catch {
    error_msg.value = 'Unable to reach the server.'
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <!-- setup wizard: full-page layout -->
  <div v-if="setup"
       style="display:flex; justify-content:center; align-items:flex-start;
              min-height:80vh; padding-top:6rem;">
    <div style="width:38rem; display:flex; flex-direction:column; gap:1rem;">
      <div>
        <h2 style="margin:0;">{{ heading }}</h2>
        <p style="color:var(--p-text-muted-color); margin:0.5rem 0 0;">{{ help }}</p>
      </div>

      <Message v-if="error_msg" severity="error" :closable="false">{{ error_msg }}</Message>

      <div v-if="pending_logo" style="display:flex; align-items:center; gap:1rem; flex-wrap:wrap;">
        <div style="display:flex; flex-direction:column; align-items:center; gap:0.25rem;">
          <img :src="pending_logo"
               style="max-height:80px; max-width:180px; object-fit:contain;
                      border:1px solid var(--p-content-border-color); border-radius:4px; padding:4px;" />
          <span style="font-size:0.75rem; color:var(--p-text-muted-color);">Logo</span>
        </div>
        <div v-if="pending_fav"
             style="display:flex; flex-direction:column; align-items:center; gap:0.25rem;">
          <img :src="pending_fav"
               style="width:32px; height:32px; object-fit:contain;
                      border:1px solid var(--p-content-border-color); border-radius:4px; padding:2px;" />
          <span style="font-size:0.75rem; color:var(--p-text-muted-color);">Favicon</span>
        </div>
      </div>

      <input ref="file_input" type="file" accept="image/*"
             style="display:none;" @change="on_file_change" />

      <div style="display:flex; gap:0.5rem; flex-wrap:wrap;">
        <template v-if="pending_logo">
          <Button label="Save & Continue" icon="pi pi-check"
                  :loading="saving" @click="save_logo" />
          <Button label="Change Image" icon="pi pi-image"
                  severity="secondary" :disabled="saving" @click="pick_file" />
        </template>
        <Button v-else label="Choose Image" icon="pi pi-image" @click="pick_file" />
        <Button label="Skip for now" severity="secondary" :disabled="saving" @click="emit('skipped')" />
      </div>
    </div>
  </div>

  <!-- inline: embedded inside a parent component (e.g. AppSettings dialog) -->
  <div v-else style="display:flex; flex-direction:column; gap:0.5rem;">
    <label style="font-size:0.875rem; color:var(--p-text-muted-color);">{{ heading }}</label>
    <small style="color:var(--p-text-muted-color);">{{ help }}</small>
    <Message v-if="error_msg" severity="error"   :closable="false">{{ error_msg }}</Message>
    <Message v-if="saved"     severity="success" :closable="false">Logo saved.</Message>

    <div v-if="pending_logo || current_logo"
         style="display:flex; align-items:center; gap:1rem; flex-wrap:wrap;">
      <div style="display:flex; flex-direction:column; align-items:center; gap:0.25rem;">
        <img :src="pending_logo || current_logo"
             style="max-height:80px; max-width:180px; object-fit:contain;
                    border:1px solid var(--p-content-border-color); border-radius:4px; padding:4px;" />
        <span style="font-size:0.75rem; color:var(--p-text-muted-color);">Logo</span>
      </div>
      <div v-if="is_theme && (pending_fav || current_logo)"
           style="display:flex; flex-direction:column; align-items:center; gap:0.25rem;">
        <img :src="pending_fav || current_logo"
             style="width:32px; height:32px; object-fit:contain;
                    border:1px solid var(--p-content-border-color); border-radius:4px; padding:2px;" />
        <span style="font-size:0.75rem; color:var(--p-text-muted-color);">Favicon</span>
      </div>
    </div>

    <input ref="file_input" type="file" accept="image/*"
           style="display:none;" @change="on_file_change" />

    <div style="display:flex; gap:0.5rem; flex-wrap:wrap;">
      <Button :label="current_logo && !pending_logo ? 'Replace Image' : 'Choose Image'"
              severity="secondary" icon="pi pi-image" @click="pick_file" :disabled="saving" />
      <Button v-if="pending_logo"
              label="Save Logo" icon="pi pi-check"
              :loading="saving" @click="save_logo" />
      <Button v-if="current_logo && !pending_logo"
              label="Remove" icon="pi pi-trash"
              severity="danger" :loading="saving" @click="remove_logo" />
    </div>
  </div>
</template>
