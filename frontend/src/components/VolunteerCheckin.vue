<script setup>
import { ref } from 'vue'
import Button  from 'primevue/button'
import Message from 'primevue/message'

import BarcodeScanner from './BarcodeScanner.vue'
import { timing_label, confirm_text } from '../lib/checkin_timing.js'

// Inline at the top of the Schedule tab (Admin.vue), mounted only while open, so the camera
// runs only then. Scans the code from a volunteer's schedule email (POST /api/checkin/scan)
// and stays open for a run of check-ins.
//
// More than 45 min early or 10 min late comes back as needs_confirm with nothing written.
// The scan is held as `pending` until the operator picks Check in anyway (resent with force)
// or Skip; further scans are ignored meanwhile, so the question can't be lost under a new one.
const emit = defineEmits(['close'])

const busy    = ref(false)
const err     = ref('')
const pending = ref(null)   // { code, result } awaiting Check in anyway / Skip
const done    = ref([])     // newest first, like a receipt

async function send(code, force) {
  busy.value = true
  err.value  = ''
  const form = new FormData()
  form.append('code', code)
  if (force) form.append('force', 'true')
  try {
    const r    = await fetch('/api/checkin/scan', { method: 'POST', body: form })
    const body = await r.json().catch(() => null)
    if (!r.ok) {
      err.value = body?.detail || 'Check-in failed.'
    } else if (body.needs_confirm) {
      pending.value = { code, result: body }
    } else {
      done.value.unshift(body)
    }
  } catch {
    err.value = 'Unable to reach the server.'
  } finally {
    busy.value = false
  }
}

function scanned(code) {
  if (busy.value || pending.value) return
  send(code, false)
}

async function check_in_anyway() {
  const { code } = pending.value
  pending.value = null
  await send(code, true)
}

function skip() {
  pending.value = null
}
</script>

<template>
  <div class="checkin-panel">
    <h3>Check in volunteers</h3>
    <div style="display:flex; flex-direction:column; gap:1rem;">
      <p style="margin:0; color:var(--p-text-muted-color); font-size:0.9rem;">
        Scan the code from the volunteer's schedule email. Each shift has its own code.
      </p>

      <BarcodeScanner @scanned="scanned" />

      <Message v-if="err" severity="error" :closable="false" style="margin:0;">{{ err }}</Message>

      <div v-if="pending" class="confirm">
        <div class="confirm-who">
          <i class="pi pi-exclamation-triangle" />
          <strong>{{ pending.result.name }}</strong>
          <span>{{ pending.result.category }} / {{ pending.result.position }}</span>
        </div>
        <div>{{ confirm_text(pending.result) }}</div>
        <div class="confirm-actions">
          <Button size="small" label="Check in anyway" icon="pi pi-check" severity="warn"
                  :loading="busy" @click="check_in_anyway()" />
          <Button size="small" label="Skip" severity="secondary" @click="skip()" />
        </div>
      </div>

      <div v-if="done.length" class="results">
        <div v-for="r in done" :key="r.slot_key + r.vol_id" class="result-row">
          <i :class="r.already ? 'pi pi-info-circle' : 'pi pi-check-circle'"
             :style="{ color: r.already ? 'var(--p-text-muted-color)' : 'var(--p-green-500)' }" />
          <strong>{{ r.name }}</strong>
          <span>{{ r.day }} {{ r.time }} · {{ r.position }}</span>
          <span class="tag">
            <template v-if="r.already">already checked in</template>
            <template v-else-if="timing_label(r)">{{ timing_label(r) }}</template>
            <template v-else-if="r.timing === 'unknown'">no shift date set</template>
          </span>
        </div>
      </div>
    </div>

    <div class="panel-actions">
      <Button label="Done" severity="secondary" @click="emit('close')" />
    </div>
  </div>
</template>

<style scoped>
.checkin-panel { border: 1px solid var(--p-content-border-color); border-radius: 6px;
                 padding: 1rem; max-width: 34rem; margin-bottom: 0.75rem; }
.checkin-panel h3 { margin: 0 0 0.75rem; font-size: 1rem; }
.panel-actions { display: flex; justify-content: flex-end; gap: 0.5rem; margin-top: 1rem; }
.confirm {
  display: flex; flex-direction: column; gap: 0.5rem;
  padding: 0.75rem;
  border: 1px solid var(--p-orange-400);
  border-radius: 6px;
  font-size: 0.9rem;
}
.confirm-who { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
.confirm-who .pi { color: var(--p-orange-400); }
.confirm-actions { display: flex; gap: 0.5rem; flex-wrap: wrap; }
.results { display: flex; flex-direction: column; gap: 0.4rem; }
.result-row {
  display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap;
  font-size: 0.9rem;
  padding: 0.4rem 0.6rem;
  background: var(--p-content-background);
  border: 1px solid var(--p-content-border-color);
  border-radius: 6px;
}
.tag { margin-left: auto; color: var(--p-text-muted-color); font-size: 0.8rem; }
</style>
