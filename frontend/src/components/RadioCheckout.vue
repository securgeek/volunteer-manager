<script setup>
import { ref, computed, onMounted } from 'vue'
import Button    from 'primevue/button'
import Select    from 'primevue/select'
import InputText from 'primevue/inputtext'
import Message   from 'primevue/message'

import BarcodeScanner from './BarcodeScanner.vue'

// Inline at the top of the Radios tab (RadiosTab.vue), mounted only while open, so the camera
// runs only then. Stays open after a checkout: the same person often takes several radios.
const emit = defineEmits(['changed', 'close'])

const barcode  = ref('')
const vols     = ref([])
const vol_id   = ref(null)
const free_name = ref('')
const saving   = ref(false)
const err      = ref('')
const done     = ref(null)    // last successful checkout, shown until the next scan

const recipient = computed(() => {
  if (vol_id.value) return vols.value.find(v => v.key === vol_id.value)?.name || ''
  return free_name.value.trim()
})
const can_save = computed(() => !!barcode.value.trim() && !!recipient.value && !saving.value)

async function load_vols() {
  try {
    const r = await fetch('/api/get_vols')
    if (r.ok) vols.value = await r.json()
  } catch { /* the free-form field still works */ }
}

onMounted(load_vols)

function on_scanned(code) {
  barcode.value = code
  err.value     = ''
  done.value    = null
}

async function save() {
  if (!can_save.value) return
  saving.value = true
  err.value    = ''
  const form = new FormData()
  form.append('barcode', barcode.value.trim())
  if (vol_id.value) form.append('vol_id', vol_id.value)
  else              form.append('person_name', free_name.value.trim())

  try {
    const r = await fetch('/api/radios/checkout', { method: 'POST', body: form })
    const body = await r.json().catch(() => null)
    if (r.ok) {
      done.value = body
      // Ready for the next radio; the same person often takes several.
      barcode.value = ''
      emit('changed')
    } else {
      err.value = body?.detail || 'Check-out failed.'
    }
  } catch {
    err.value = 'Unable to reach the server.'
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="radio-panel">
    <h3>Check out a radio</h3>
    <div style="display:flex; flex-direction:column; gap:1rem;">
      <BarcodeScanner @scanned="on_scanned" />

      <div v-if="barcode" class="barcode-line">
        <span class="lbl">Radio</span>
        <code>{{ barcode }}</code>
        <Button icon="pi pi-times" text size="small" severity="secondary"
                aria-label="Clear barcode" @click="barcode = ''" />
      </div>

      <div class="field">
        <label for="rc_vol">Volunteer</label>
        <Select id="rc_vol" v-model="vol_id" :options="vols" option-label="name"
                option-value="key" filter show-clear
                placeholder="Search volunteers…"
                :disabled="!!free_name.trim()" />
      </div>

      <div class="field">
        <label for="rc_name">Or a name</label>
        <InputText id="rc_name" v-model="free_name" placeholder="Anyone not on the roster"
                   :disabled="!!vol_id" autocomplete="off" />
      </div>

      <Message v-if="err" severity="error" :closable="false" style="margin:0;">{{ err }}</Message>

      <Message v-if="done" :severity="done.already_held ? 'info' : 'success'"
               :closable="false" style="margin:0;">
        <code>{{ done.barcode }}</code>
        <template v-if="done.already_held"> is already with {{ done.person_name }}</template>
        <template v-else> → {{ done.person_name }}</template>
        <span v-if="done.transferred_from"> (handed over from {{ done.transferred_from }})</span>
      </Message>
    </div>

    <div class="panel-actions">
      <Button label="Done" severity="secondary" @click="emit('close')" />
      <Button label="Check Out" icon="pi pi-arrow-right" :disabled="!can_save"
              :loading="saving" @click="save" />
    </div>
  </div>
</template>

<style scoped>
.radio-panel { border: 1px solid var(--p-content-border-color); border-radius: 6px;
               padding: 1rem; max-width: 34rem; }
.radio-panel h3 { margin: 0 0 0.75rem; font-size: 1rem; }
.panel-actions { display: flex; justify-content: flex-end; gap: 0.5rem; margin-top: 1rem; }
.field { display: flex; flex-direction: column; gap: 0.35rem; }
.field label { font-size: 0.85rem; color: var(--p-text-muted-color); }
.barcode-line { display: flex; align-items: center; gap: 0.5rem; }
.barcode-line .lbl { font-size: 0.85rem; color: var(--p-text-muted-color); }
.barcode-line code { font-size: 1.05rem; }
</style>
