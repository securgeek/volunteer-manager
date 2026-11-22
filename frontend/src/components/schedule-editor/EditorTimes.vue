<script setup>
import { ref, reactive, computed, watch } from 'vue'
import Checkbox from 'primevue/checkbox'
import Button   from 'primevue/button'
import Message  from 'primevue/message'
import { derive_time_key, format_time_str } from './time_keys.js'

const props = defineProps({ times: Array, days: Array, dayTimes: Array })
const emit  = defineEmits(['reload'])

const err         = ref('')
const note        = ref('')
const time_edit   = ref(null)
const time_form   = reactive({ start: '', end: '', day_keys: [], fill_all: true })
const time_saving = ref(false)

// "sat|0800" membership set, so a cell lookup is O(1)
const enabled = computed(() => new Set(props.dayTimes.map(dt => `${dt.day_key}|${dt.time_key}`)))
function has_day(day_key, time_key) { return enabled.value.has(`${day_key}|${time_key}`) }

// Short column header: "Saturday" → "Sat"
function short_day(d) { return d.day_str.length > 4 ? d.day_str.slice(0, 3) : d.day_str }

const time_duration = computed(() => {
  if (!time_form.start || !time_form.end) return ''
  const [sh, sm] = time_form.start.split(':').map(Number)
  const [eh, em] = time_form.end.split(':').map(Number)
  const mins = (eh * 60 + em) - (sh * 60 + sm)
  if (mins <= 0) return ''
  const h = mins / 60
  return (Number.isInteger(h) ? h : h.toFixed(1)) + 'h'
})

function hhmm_to_input(hhmm) {
  if (!hhmm || hhmm.length < 4) return ''
  return hhmm.slice(0, 2) + ':' + hhmm.slice(2, 4)
}

// What key the add row will claim, and whether it had to be disambiguated.
const new_key = computed(() =>
  time_form.start && time_form.end
    ? derive_time_key(time_form.start, time_form.end, props.times)
    : '')
const key_taken = computed(() =>
  !!new_key.value && new_key.value !== time_form.start.replace(':', ''))

// Pre-check every day on the add row, and keep it in step as days are added.
watch(() => props.days, (days) => {
  if (time_edit.value === null && time_form.day_keys.length === 0)
    time_form.day_keys = days.map(d => d.day_key)
}, { immediate: true })

async function api_post(url, fields) {
  err.value = ''
  const form = new FormData()
  for (const [k, v] of Object.entries(fields)) if (v != null) form.append(k, String(v))
  const r = await fetch(url, { method: 'POST', body: form })
  if (!r.ok) {
    const b = await r.json().catch(() => null)
    err.value = b?.detail || 'Request failed'
    return null
  }
  return r.json().catch(() => ({}))
}

async function toggle_day(day_key, time_key) {
  note.value = ''
  const d = await api_post('/api/schedule/set_day_time', {
    day_key, time_key, enabled: !has_day(day_key, time_key),
  })
  if (d?.slots_removed)
    note.value = `Removed ${d.slots_removed} empty slot${d.slots_removed !== 1 ? 's' : ''}.`
  // Reload either way: on failure this restores the checkbox to its true state.
  emit('reload')
}

function time_start_edit(t) {
  time_edit.value = t.time_key
  const parts = t.time_str.split('-')
  time_form.start = hhmm_to_input(t.time_key)
  time_form.end   = hhmm_to_input((parts[1] || '').trim())
}
function time_cancel() {
  time_edit.value  = null
  time_form.start  = ''
  time_form.end    = ''
  time_form.day_keys = props.days.map(d => d.day_key)
  time_form.fill_all = true
}
async function time_save() {
  if (!time_form.start || !time_form.end) return
  const adding   = time_edit.value === null
  const time_key = time_edit.value ?? derive_time_key(time_form.start, time_form.end, props.times)
  const time_str = format_time_str(time_form.start, time_form.end)
  time_saving.value = true
  note.value = ''
  // day_keys and fill_all apply to new times only — editing a range must not create slots.
  const d = await api_post('/api/schedule/upsert_time', adding
    ? { time_key, time_str, day_keys: time_form.day_keys.join(','),
        fill_all: time_form.fill_all, create: true }
    : { time_key, time_str })
  time_saving.value = false
  if (d) {
    if (d.slots_created)
      note.value = `Created ${d.slots_created} slot${d.slots_created !== 1 ? 's' : ''}.`
    time_cancel()
    emit('reload')
  }
}
async function time_delete(time_key) {
  if (await api_post('/api/schedule/delete_time', { time_key })) emit('reload')
}
</script>

<template>
  <Message v-if="err" severity="error" :closable="true" @close="err = ''"
           style="margin-bottom:0.75rem;">{{ err }}</Message>
  <Message v-if="note" severity="success" :closable="true" @close="note = ''"
           style="margin-bottom:0.75rem;">{{ note }}</Message>

  <table class="se-table">
    <thead>
      <tr>
        <th>Key</th><th>Range</th>
        <th v-for="d in days" :key="d.day_key" class="day-col"
            v-tooltip.top="d.day_str">{{ short_day(d) }}</th>
        <th></th>
      </tr>
    </thead>
    <tbody>
      <tr v-for="t in times" :key="t.time_key">
        <template v-if="time_edit === t.time_key">
          <td :colspan="2 + days.length">
            <div style="display:flex; gap:0.5rem; align-items:center;">
              <code class="key-badge">{{ t.time_key }}</code>
              <input type="time" v-model="time_form.start" class="time-input" />
              <span class="sep">–</span>
              <input type="time" v-model="time_form.end" class="time-input" />
              <span v-if="time_duration" class="duration">{{ time_duration }}</span>
            </div>
          </td>
          <td class="actions">
            <Button size="small" label="Save" :loading="time_saving" @click="time_save" />
            <Button size="small" label="Cancel" severity="secondary" @click="time_cancel" />
          </td>
        </template>
        <template v-else>
          <td><code>{{ t.time_key }}</code></td>
          <td>{{ t.time_str }}</td>
          <td v-for="d in days" :key="d.day_key" class="day-col">
            <Checkbox :model-value="has_day(d.day_key, t.time_key)" binary
                      @update:model-value="toggle_day(d.day_key, t.time_key)" />
          </td>
          <td class="actions">
            <Button size="small" icon="pi pi-pencil" severity="secondary" text
                    @click="time_start_edit(t)" />
            <Button size="small" icon="pi pi-trash" severity="danger" text
                    @click="time_delete(t.time_key)" />
          </td>
        </template>
      </tr>
      <tr v-if="times.length === 0">
        <td :colspan="3 + days.length" class="empty">No times defined yet.</td>
      </tr>
    </tbody>
  </table>

  <template v-if="time_edit === null">
    <div class="add-row">
      <input type="time" v-model="time_form.start" class="time-input" />
      <span class="sep">–</span>
      <input type="time" v-model="time_form.end" class="time-input" />
      <span v-if="time_duration" class="duration">{{ time_duration }}</span>
      <code v-if="new_key" class="key-badge" :class="{ 'key-alt': key_taken }"
            v-tooltip.top="key_taken
              ? 'A different range already starts at this hour, so the end time is part of the key'
              : 'Key this time will be stored under'">{{ new_key }}</code>
      <Button label="Add Time" :loading="time_saving"
              :disabled="!time_form.start || !time_form.end" @click="time_save" />
    </div>
    <div class="add-opts">
      <span class="add-opts-label">Days</span>
      <span v-for="d in days" :key="d.day_key" class="opt">
        <Checkbox v-model="time_form.day_keys" :value="d.day_key" :inputId="`tday_${d.day_key}`" />
        <label :for="`tday_${d.day_key}`">{{ d.day_str }}</label>
      </span>
      <span v-if="days.length === 0" class="add-opts-label">No days defined yet.</span>
      <span class="opt" style="margin-left:auto;">
        <Checkbox v-model="time_form.fill_all" binary inputId="time_fill_all" />
        <label for="time_fill_all">Add all positions</label>
      </span>
    </div>
  </template>
</template>

<style scoped>
@import './editor.css';
.time-input {
  background: var(--p-inputtext-background);
  color: var(--p-inputtext-color);
  border: 1px solid var(--p-inputtext-border-color);
  border-radius: var(--p-inputtext-border-radius);
  padding: 0.5rem 0.75rem;
  font-size: 1rem;
  font-family: inherit;
  outline: none;
  color-scheme: dark;
}
.time-input:focus {
  border-color: var(--p-inputtext-focus-border-color);
  box-shadow: 0 0 0 var(--p-focus-ring-width) var(--p-focus-ring-color);
}
.duration { font-size: 0.8rem; color: var(--p-text-muted-color); white-space: nowrap; }
/* A key that had to take the end time on to stay distinct from another shift. */
.key-alt  { color: var(--p-orange-400); }
.day-col { text-align: center; width: 1%; white-space: nowrap; }
</style>
