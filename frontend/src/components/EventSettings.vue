<script setup>
import { ref, onMounted } from 'vue'
import Button    from 'primevue/button'
import InputText from 'primevue/inputtext'
import Message   from 'primevue/message'

// In App Settings: the event timezone and the check-in key.
//
// Shift times are wall-clock times at the venue. Check-in compares now with a shift's start
// (its day's date plus the start of its time range) in this zone, and the export API gives
// shift times with its offset. Rotating the key voids every check-in code already emailed.

const tz       = ref('')
const tz_msg   = ref('')
const tz_err   = ref('')
const key_msg  = ref('')
const key_err  = ref('')
const confirm_rotate = ref(false)

// What the browser thinks, offered as a one-click fill when it differs.
const browser_tz = Intl.DateTimeFormat().resolvedOptions().timeZone

onMounted(async () => {
  try {
    const r = await fetch('/api/app_config')
    if (r.ok) tz.value = (await r.json()).event_tz || ''
  } catch { /* leave blank */ }
})

async function save_tz() {
  tz_msg.value = ''; tz_err.value = ''
  const fd = new FormData()
  fd.append('event_tz', tz.value)
  const r    = await fetch('/api/app_settings/event_tz', { method: 'POST', body: fd })
  const body = await r.json().catch(() => null)
  if (r.ok) { tz.value = body.event_tz; tz_msg.value = 'Saved.' }
  else      tz_err.value = body?.detail || 'Could not save the timezone.'
}

async function rotate() {
  key_msg.value = ''; key_err.value = ''
  confirm_rotate.value = false
  const r = await fetch('/api/checkin/rotate_key', { method: 'POST' })
  if (r.ok) key_msg.value = 'New key made. Every code sent before now no longer works.'
  else      key_err.value = 'Could not rotate the key.'
}
</script>

<template>
  <div class="event-settings">
    <div>
      <label for="event-tz" class="lbl">Event timezone</label>
      <div class="row">
        <InputText id="event-tz" v-model="tz" placeholder="America/New_York" style="flex:1;" />
        <Button label="Save" severity="secondary" @click="save_tz()" />
      </div>
      <small class="hint">
        Shift times are read in this zone to decide whether a check-in is early or late.
        <a v-if="browser_tz && browser_tz !== tz" href="#" @click.prevent="tz = browser_tz">
          Use {{ browser_tz }}</a>
      </small>
      <Message v-if="tz_msg" severity="success" :closable="false" class="msg">{{ tz_msg }}</Message>
      <Message v-if="tz_err" severity="error"   :closable="false" class="msg">{{ tz_err }}</Message>
    </div>

    <div>
      <span class="lbl">Check-in codes</span>
      <div class="row">
        <Button v-if="!confirm_rotate" label="Rotate key" icon="pi pi-refresh" severity="secondary"
                @click="confirm_rotate = true" />
        <template v-else>
          <Button label="Void every code" severity="danger" @click="rotate()" />
          <Button label="Cancel" severity="secondary" @click="confirm_rotate = false" />
        </template>
      </div>
      <small class="hint">
        Voids every check-in code already emailed. Re-send schedule emails afterwards.
      </small>
      <Message v-if="key_msg" severity="success" :closable="false" class="msg">{{ key_msg }}</Message>
      <Message v-if="key_err" severity="error"   :closable="false" class="msg">{{ key_err }}</Message>
    </div>
  </div>
</template>

<style scoped>
.event-settings { display: flex; flex-direction: column; gap: 1rem; }
.lbl  { display: block; font-weight: 600; margin-bottom: 0.35rem; }
.row  { display: flex; gap: 0.5rem; flex-wrap: wrap; }
.hint { display: block; color: var(--p-text-muted-color); margin-top: 0.3rem; }
.msg  { margin: 0.4rem 0 0; }
</style>
