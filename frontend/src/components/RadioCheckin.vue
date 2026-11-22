<script setup>
import { ref } from 'vue'
import Button  from 'primevue/button'
import Message from 'primevue/message'

import BarcodeScanner from './BarcodeScanner.vue'
import { format_elapsed } from './radio_time.js'

// Inline at the top of the Radios tab (RadiosTab.vue), mounted only while open, so the camera
// runs only then. Stays open for a run of returns.
const emit = defineEmits(['changed', 'close'])

const busy    = ref(false)
const err     = ref('')
// Kept as a list so a run of returns reads like a receipt rather than one line
// that flickers between radios.
const returned = ref([])


async function check_in(code) {
  if (busy.value) return
  busy.value = true
  err.value  = ''
  const form = new FormData()
  form.append('barcode', code)
  try {
    const r    = await fetch('/api/radios/checkin', { method: 'POST', body: form })
    const body = await r.json().catch(() => null)
    if (r.ok) {
      returned.value.unshift(body)
      emit('changed')
    } else {
      err.value = body?.detail || 'Check-in failed.'
    }
  } catch {
    err.value = 'Unable to reach the server.'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="radio-panel">
    <h3>Check in radios</h3>
    <div style="display:flex; flex-direction:column; gap:1rem;">
      <p style="margin:0; color:var(--p-text-muted-color); font-size:0.9rem;">
        Scan a radio to return it. The barcode is all that is needed — keep scanning to
        return several.
      </p>

      <BarcodeScanner @scanned="check_in" />

      <Message v-if="err" severity="error" :closable="false" style="margin:0;">{{ err }}</Message>

      <div v-if="returned.length" class="returns">
        <div v-for="r in returned" :key="r.barcode + r.checked_in_at" class="return-row">
          <i class="pi pi-check-circle" style="color:var(--p-green-500);" />
          <code>{{ r.barcode }}</code>
          <span>back from {{ r.person_name }}</span>
          <span class="elapsed">after {{ format_elapsed(r.checked_out_at, r.checked_in_at) }}</span>
        </div>
      </div>
    </div>

    <div class="panel-actions">
      <Button label="Done" severity="secondary" @click="emit('close')" />
    </div>
  </div>
</template>

<style scoped>
.radio-panel { border: 1px solid var(--p-content-border-color); border-radius: 6px;
               padding: 1rem; max-width: 34rem; }
.radio-panel h3 { margin: 0 0 0.75rem; font-size: 1rem; }
.panel-actions { display: flex; justify-content: flex-end; gap: 0.5rem; margin-top: 1rem; }
.returns { display: flex; flex-direction: column; gap: 0.4rem; }
.return-row {
  display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap;
  font-size: 0.9rem;
  padding: 0.4rem 0.6rem;
  background: var(--p-content-background);
  border: 1px solid var(--p-content-border-color);
  border-radius: 6px;
}
.elapsed { margin-left: auto; color: var(--p-text-muted-color); font-size: 0.8rem; }
</style>
