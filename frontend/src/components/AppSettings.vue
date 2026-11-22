<script setup>
import { ref } from 'vue'
import Dialog   from 'primevue/dialog'
import SetTitle from './setup/SetTitle.vue'
import SetLogo  from './setup/SetLogo.vue'
import EventSettings from './EventSettings.vue'

const emit = defineEmits(['title-changed', 'logo-changed'])

const visible       = ref(false)
const current_title = ref('')
const current_logo  = ref(null)
const current_header_logo = ref(null)

function open({ title, logo } = {}) {
  current_title.value = title || ''
  current_logo.value  = logo  || null
  visible.value       = true
  // The header logo only matters to the volunteer pages, so the admin shell doesn't keep it;
  // read it fresh each time the dialog opens.
  fetch('/api/app_config')
    .then(r => (r.ok ? r.json() : null))
    .then(cfg => { current_header_logo.value = cfg?.header_logo || null })
    .catch(() => { /* leave the section empty; uploading still works */ })
}

function on_title_done(new_title) {
  emit('title-changed', new_title)
}

function on_logo_done({ logo, favicon }) {
  current_logo.value = logo || null
  emit('logo-changed', { logo, favicon })
}

function on_header_logo_done({ logo }) {
  current_header_logo.value = logo || null
}

defineExpose({ open })
</script>

<template>
  <Dialog v-model:visible="visible" header="App Settings" modal :style="{ width: '26rem' }">
    <div style="display:flex; flex-direction:column; gap:1.25rem; padding-top:0.25rem;">
      <SetTitle :setup="false" :current-title="current_title" @done="on_title_done" />
      <hr style="border:none; border-top:1px solid var(--p-content-border-color); margin:0;">
      <SetLogo  :setup="false" :current-logo="current_logo"   @done="on_logo_done" />
      <SetLogo  :setup="false" variant="header" :current-logo="current_header_logo"
                @done="on_header_logo_done" />
      <hr style="border:none; border-top:1px solid var(--p-content-border-color); margin:0;">
      <EventSettings />
    </div>
  </Dialog>
</template>
