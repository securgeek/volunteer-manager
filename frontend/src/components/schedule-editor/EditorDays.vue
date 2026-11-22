<script setup>
import { ref, reactive, computed, watch } from 'vue'
import DatePicker from 'primevue/datepicker'
import Checkbox   from 'primevue/checkbox'
import Button     from 'primevue/button'
import Message    from 'primevue/message'

const props = defineProps({ days: Array, times: Array, dayTimes: Array })
const emit  = defineEmits(['reload'])

const err        = ref('')
const note       = ref('')
const day_edit   = ref(null)
const day_form   = reactive({ date: null, time_keys: [], fill_all: true })
const day_saving = ref(false)

// How many times each day runs — edited on the Times tab, shown here for orientation.
const time_count = computed(() => {
  const counts = {}
  for (const dt of props.dayTimes) counts[dt.day_key] = (counts[dt.day_key] ?? 0) + 1
  return counts
})

function fmt_date(iso) {
  if (!iso) return '—'
  const [y, m, d] = iso.split('-').map(Number)
  return m + '/' + d + '/' + y
}
function date_to_iso(dt) {
  const y = dt.getFullYear()
  const m = String(dt.getMonth() + 1).padStart(2, '0')
  const d = String(dt.getDate()).padStart(2, '0')
  return `${y}-${m}-${d}`
}
function iso_to_date(iso) {
  if (!iso) return null
  const [y, m, d] = iso.split('-').map(Number)
  return new Date(y, m - 1, d)
}
function next_day_default() {
  const dates = props.days.map(d => d.event_date).filter(Boolean).map(iso_to_date)
  if (!dates.length) return null
  const latest = new Date(Math.max(...dates.map(d => d.getTime())))
  const next = new Date(latest)
  next.setDate(next.getDate() + 1)
  return next
}

// Pre-check every time on the add row, and keep it in step as times are added.
watch(() => props.times, (times) => {
  if (day_edit.value === null && day_form.time_keys.length === 0)
    day_form.time_keys = times.map(t => t.time_key)
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

function day_start_edit(d) {
  day_edit.value = d.day_key
  day_form.date  = iso_to_date(d.event_date)
}
function day_cancel() {
  day_edit.value      = null
  day_form.date       = next_day_default()
  day_form.time_keys  = props.times.map(t => t.time_key)
  day_form.fill_all   = true
}
async function day_save() {
  if (!day_form.date) return
  const adding     = day_edit.value === null
  const dt         = day_form.date
  const day_key    = day_edit.value ?? dt.toLocaleDateString('en-US', { weekday: 'short' }).toLowerCase()
  const day_str    = dt.toLocaleDateString('en-US', { weekday: 'long' })
  const event_date = date_to_iso(dt)
  day_saving.value = true
  note.value = ''
  // time_keys and fill_all apply to new days only — editing a date must not create slots.
  const d = await api_post('/api/schedule/upsert_day', adding
    ? { day_key, day_str, event_date, time_keys: day_form.time_keys.join(','), fill_all: day_form.fill_all }
    : { day_key, day_str, event_date })
  day_saving.value = false
  if (d) {
    if (d.slots_created)
      note.value = `Created ${d.slots_created} slot${d.slots_created !== 1 ? 's' : ''}.`
    day_cancel()
    emit('reload')
  }
}
async function day_delete(day_key) {
  if (await api_post('/api/schedule/delete_day', { day_key })) emit('reload')
}
</script>

<template>
  <Message v-if="err" severity="error" :closable="true" @close="err = ''"
           style="margin-bottom:0.75rem;">{{ err }}</Message>
  <Message v-if="note" severity="success" :closable="true" @close="note = ''"
           style="margin-bottom:0.75rem;">{{ note }}</Message>

  <table class="se-table">
    <thead><tr><th>Key</th><th>Display name</th><th>Date</th><th>Times</th><th></th></tr></thead>
    <tbody>
      <tr v-for="d in days" :key="d.day_key">
        <template v-if="day_edit === d.day_key">
          <td colspan="4">
            <div style="display:flex; gap:0.75rem; align-items:center;">
              <code class="key-badge">{{ d.day_key }}</code>
              <DatePicker v-model="day_form.date" dateFormat="m/d/yy"
                          showIcon manualInput placeholder="Pick a date" />
            </div>
          </td>
          <td class="actions">
            <Button size="small" label="Save" :loading="day_saving" @click="day_save" />
            <Button size="small" label="Cancel" severity="secondary" @click="day_cancel" />
          </td>
        </template>
        <template v-else>
          <td><code>{{ d.day_key }}</code></td>
          <td>{{ d.day_str }}</td>
          <td>{{ fmt_date(d.event_date) }}</td>
          <td class="muted" v-tooltip.top="'Edit on the Times tab'">
            {{ time_count[d.day_key] ?? 0 }} time{{ (time_count[d.day_key] ?? 0) !== 1 ? 's' : '' }}
          </td>
          <td class="actions">
            <Button size="small" icon="pi pi-pencil" severity="secondary" text
                    @click="day_start_edit(d)" />
            <Button size="small" icon="pi pi-trash" severity="danger" text
                    @click="day_delete(d.day_key)" />
          </td>
        </template>
      </tr>
      <tr v-if="days.length === 0">
        <td colspan="5" class="empty">No days defined yet.</td>
      </tr>
    </tbody>
  </table>

  <template v-if="day_edit === null">
    <div class="add-row">
      <DatePicker v-model="day_form.date" dateFormat="m/d/yy"
                  showIcon placeholder="Pick a date" />
      <Button label="Add Day" :loading="day_saving"
              :disabled="!day_form.date" @click="day_save" />
    </div>
    <div class="add-opts">
      <span class="add-opts-label">Times</span>
      <span v-for="t in times" :key="t.time_key" class="opt">
        <Checkbox v-model="day_form.time_keys" :value="t.time_key" :inputId="`dtime_${t.time_key}`" />
        <label :for="`dtime_${t.time_key}`">{{ t.time_str }}</label>
      </span>
      <span v-if="times.length === 0" class="add-opts-label">No times defined yet.</span>
      <span class="opt" style="margin-left:auto;">
        <Checkbox v-model="day_form.fill_all" binary inputId="day_fill_all" />
        <label for="day_fill_all">Add all positions</label>
      </span>
    </div>
  </template>
</template>

<style scoped>
@import './editor.css';
.muted { color: var(--p-text-muted-color); font-size: 0.85em; }
</style>
