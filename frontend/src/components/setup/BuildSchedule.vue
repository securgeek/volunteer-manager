<script setup>
import { ref, computed, watch } from 'vue'
import Stepper      from 'primevue/stepper'
import StepList     from 'primevue/steplist'
import Step         from 'primevue/step'
import StepPanels   from 'primevue/steppanels'
import StepPanel    from 'primevue/steppanel'
import DatePicker   from 'primevue/datepicker'
import InputText    from 'primevue/inputtext'
import Textarea     from 'primevue/textarea'
import Checkbox     from 'primevue/checkbox'
import SelectButton from 'primevue/selectbutton'
import ColorPicker  from 'primevue/colorpicker'
import Button       from 'primevue/button'
import Message      from 'primevue/message'

import { derive_time_key, format_time_str } from '../schedule-editor/time_keys.js'
import { norm_color, text_on } from '../../lib/colors.js'

const emit = defineEmits(['done', 'back'])

// Nothing is written until Create Schedule on the last step, so stepping back and
// reworking an earlier answer never leaves half a schedule in the database.
const step = ref('1')
const err  = ref('')

// ── step 1: days ──────────────────────────────────────────────────────────────
const days     = ref([])     // { day_key, day_str, date: Date }
const new_date = ref(null)

function date_to_iso(dt) {
  const m = String(dt.getMonth() + 1).padStart(2, '0')
  const d = String(dt.getDate()).padStart(2, '0')
  return `${dt.getFullYear()}-${m}-${d}`
}
function short_md(dt) { return `${dt.getMonth() + 1}/${dt.getDate()}` }
function fmt_date(dt) { return dt ? dt.toLocaleDateString('en-US') : '—' }

function add_day() {
  if (!new_date.value) return
  const dt   = new_date.value
  const base = dt.toLocaleDateString('en-US', { weekday: 'short' }).toLowerCase()
  const long = dt.toLocaleDateString('en-US', { weekday: 'long' })

  // A conference spanning two weekends has two Saturdays; keep both distinguishable.
  const taken = new Set(days.value.map(d => d.day_key))
  let day_key = base
  for (let n = 2; taken.has(day_key); n++) day_key = `${base}${n}`
  const day_str = days.value.some(d => d.day_str === long) ? `${long} ${short_md(dt)}` : long

  days.value.push({ day_key, day_str, date: dt })
  days.value.sort((a, b) => a.date - b.date)
  new_date.value = null
}

function remove_day(day_key) {
  days.value = days.value.filter(d => d.day_key !== day_key)
  for (const t of times.value) t.day_keys = t.day_keys.filter(k => k !== day_key)
}

// The next day of the event is nearly always the one after the last.
function suggest_next_day() {
  if (!days.value.length) return
  const next = new Date(days.value[days.value.length - 1].date)
  next.setDate(next.getDate() + 1)
  new_date.value = next
}

// ── step 2: categories ────────────────────────────────────────────────────────
const cats     = ref([])     // { name, prefix, label, mode, names_text, counts, color }
const new_cat  = ref('')

// Mirrors _DEFAULT_CAT_COLORS in routers/schedule.py, so what the builder previews is what
// the backend would have assigned anyway. ColorPicker works in hex without the '#'.
const CAT_COLORS = ['ff4500', 'ffa500', '3cb371', '87ceeb',
                    'a78bfa', 'f472b6', 'facc15', '2dd4bf']

const MODES = [
  { label: 'Numbered', value: 'numbered' },
  { label: 'Named',    value: 'named' },
]

function derive_prefix(name) {
  return name.toLowerCase().replace(/[^a-z0-9]/g, '') || 'pos'
}

function add_cat() {
  const name = new_cat.value.trim()
  if (!name || cats.value.some(c => c.name === name)) return
  cats.value.push({
    name, prefix: derive_prefix(name), label: name,
    mode: 'numbered', names_text: '', counts: {},
    color: CAT_COLORS[cats.value.length % CAT_COLORS.length],
  })
  new_cat.value = ''
}

function remove_cat(name) { cats.value = cats.value.filter(c => c.name !== name) }

function chip_style(cat) {
  const bg = norm_color(cat.color)
  return { background: bg, color: text_on(bg) }
}

function move_cat(i, delta) {
  const j = i + delta
  if (j < 0 || j >= cats.value.length) return
  const arr = cats.value.slice()
  ;[arr[i], arr[j]] = [arr[j], arr[i]]
  cats.value = arr
}

/** A named category's list, one name per line. */
function cat_names(cat) {
  return cat.names_text.split('\n').map(s => s.trim()).filter(Boolean)
}

/** How many positions a category can offer — its name list, or its largest count. */
function cat_max(cat) {
  if (cat.mode === 'named') return cat_names(cat).length
  return Math.max(0, ...Object.values(cat.counts).map(Number).filter(Number.isFinite))
}

const PREFIX_RE = /^[a-z0-9][a-z0-9-]*$/
const bad_prefix = computed(() =>
  cats.value.filter(c => c.prefix && !PREFIX_RE.test(c.prefix)).map(c => c.name))
const empty_named = computed(() =>
  cats.value.filter(c => c.mode === 'named' && !cat_names(c).length).map(c => c.name))
const dup_prefix = computed(() => {
  const seen = new Set(), dups = new Set()
  for (const c of cats.value) {
    if (seen.has(c.prefix)) dups.add(c.prefix)
    seen.add(c.prefix)
  }
  return [...dups]
})

// ── step 3: times ─────────────────────────────────────────────────────────────
const times     = ref([])    // { time_key, time_str, start, end, day_keys }
const new_start = ref('')
const new_end   = ref('')
const new_days  = ref([])

const new_time_key = computed(() =>
  new_start.value && new_end.value
    ? derive_time_key(new_start.value, new_end.value, times.value)
    : '')
const new_key_taken = computed(() =>
  !!new_time_key.value && new_time_key.value !== new_start.value.replace(':', ''))

function add_time() {
  if (!new_start.value || !new_end.value || !new_days.value.length) return
  times.value.push({
    time_key: new_time_key.value,
    time_str: format_time_str(new_start.value, new_end.value),
    start:    new_start.value,
    end:      new_end.value,
    day_keys: [...new_days.value],
  })
  times.value.sort((a, b) => a.time_key.localeCompare(b.time_key))
  new_start.value = ''
  new_end.value   = ''
}

function remove_time(time_key) {
  times.value = times.value.filter(t => t.time_key !== time_key)
}

function toggle_time_day(t, day_key) {
  t.day_keys = t.day_keys.includes(day_key)
    ? t.day_keys.filter(k => k !== day_key)
    : [...t.day_keys, day_key]
}

// ── step 4: the counts grid ───────────────────────────────────────────────────
// One column per day/time pair the schedule actually runs.
const columns = computed(() => {
  const out = []
  for (const d of days.value) {
    for (const t of times.value) {
      if (t.day_keys.includes(d.day_key))
        out.push({ key: `${d.day_key}|${t.time_key}`, day_key: d.day_key,
                   day_str: d.day_str, time_key: t.time_key, time_str: t.time_str })
    }
  }
  return out
})

// Column count per day, for the grouped header.
const day_spans = computed(() =>
  days.value
    .map(d => ({ ...d, span: columns.value.filter(c => c.day_key === d.day_key).length }))
    .filter(d => d.span > 0))

/** Give every category a value in every current column, dropping cells that no longer exist. */
function sync_counts() {
  const keys = columns.value.map(c => c.key)
  for (const cat of cats.value) {
    const next = {}
    for (const k of keys) next[k] = Number(cat.counts[k]) || 0
    cat.counts = next
  }
}

function fill_row(cat, raw) {
  const n = Math.max(0, Number(raw) || 0)
  for (const c of columns.value) cat.counts[c.key] = n
}

const totals = computed(() => {
  let positions = 0, slots = 0, over = false
  for (const cat of cats.value) {
    const max = cat_max(cat)
    positions += max
    for (const c of columns.value) {
      const want = Number(cat.counts[c.key]) || 0
      if (want > max) over = true
      slots += Math.min(want, max)
    }
  }
  return { positions, slots, over }
})

// ── navigation ────────────────────────────────────────────────────────────────
const can_next = computed(() => {
  if (step.value === '1') return days.value.length > 0
  if (step.value === '2') return cats.value.length > 0 && !bad_prefix.value.length
                              && !dup_prefix.value.length && !empty_named.value.length
  if (step.value === '3') return times.value.some(t => t.day_keys.length > 0)
  return true
})

watch(step, (v) => { if (v === '4') sync_counts() })

function go(n) {
  err.value = ''
  step.value = n
}

// ── submit ────────────────────────────────────────────────────────────────────
const saving = ref(false)
const result = ref(null)

function build_config() {
  const live = new Set(columns.value.map(c => c.key))
  return {
    days: days.value.map(d => ({ key: d.day_key, title: d.day_str, date: date_to_iso(d.date) })),
    times: times.value
      .filter(t => t.day_keys.length)
      .map(t => ({ key: t.time_key, title: t.time_str, day_keys: t.day_keys })),
    categories: cats.value.map(c => {
      const max    = cat_max(c)
      // Only send cells that still map to a declared day/time, capped at what exists.
      const counts = {}
      for (const [k, v] of Object.entries(c.counts)) {
        const n = Math.min(Number(v) || 0, max)
        if (live.has(k) && n > 0) counts[k] = n
      }
      return {
        name:   c.name,
        prefix: c.prefix || derive_prefix(c.name),
        label:  c.label || c.name,
        names:  c.mode === 'named' ? cat_names(c) : null,
        color:  norm_color(c.color),
        counts,
      }
    }),
  }
}

async function create_schedule() {
  err.value    = ''
  result.value = null
  saving.value = true
  const form = new FormData()
  form.append('config', JSON.stringify(build_config()))
  try {
    const resp = await fetch('/api/schedule/build', { method: 'POST', body: form })
    if (resp.ok) {
      result.value = await resp.json()
      setTimeout(() => emit('done'), 1500)
    } else {
      const body = await resp.json().catch(() => null)
      err.value = body?.detail || 'Failed to create the schedule.'
    }
  } catch {
    err.value = 'Unable to reach the server.'
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="build-wrap">
    <div class="build-head">
      <h2 style="margin:0;">Build Schedule</h2>
      <p style="color:var(--p-text-muted-color); margin:0.4rem 0 0;">
        Set your days, position categories and time slots, then fill in how many of each
        position you need at each time. Nothing is saved until the last step.
      </p>
    </div>

    <Message v-if="err" severity="error" :closable="true" @close="err = ''"
             style="margin-bottom:1rem;">{{ err }}</Message>

    <Stepper v-model:value="step">
      <StepList>
        <Step value="1">Days</Step>
        <Step value="2">Categories</Step>
        <Step value="3">Times</Step>
        <Step value="4">Positions</Step>
      </StepList>

      <StepPanels>
        <!-- ── days ────────────────────────────────────────────────────────── -->
        <StepPanel value="1">
          <p class="hint">Which dates does the event run?</p>
          <table class="se-table">
            <thead><tr><th>Key</th><th>Day</th><th>Date</th><th></th></tr></thead>
            <tbody>
              <tr v-for="d in days" :key="d.day_key">
                <td><code>{{ d.day_key }}</code></td>
                <td>{{ d.day_str }}</td>
                <td>{{ fmt_date(d.date) }}</td>
                <td class="actions">
                  <Button size="small" icon="pi pi-trash" severity="danger" text
                          @click="remove_day(d.day_key)" />
                </td>
              </tr>
              <tr v-if="!days.length"><td colspan="4" class="empty">No days yet.</td></tr>
            </tbody>
          </table>
          <div class="add-row">
            <DatePicker v-model="new_date" dateFormat="m/d/yy" showIcon manualInput
                        placeholder="Pick a date" @focus="!new_date && suggest_next_day()" />
            <Button label="Add Day" :disabled="!new_date" @click="add_day" />
          </div>
          <div class="nav">
            <Button label="Back" severity="secondary" @click="emit('back')" />
            <Button label="Next" icon="pi pi-arrow-right" iconPos="right"
                    :disabled="!can_next" @click="go('2')" />
          </div>
        </StepPanel>

        <!-- ── categories ──────────────────────────────────────────────────── -->
        <StepPanel value="2">
          <p class="hint">
            Group your positions — Security, Registration, Room Monitor. <strong>Numbered</strong>
            categories are named for you (Security 1, Security 2 …); a <strong>Named</strong>
            one takes an explicit list, so a count of 4 later means the first 4 on that list.
          </p>
          <table class="se-table">
            <thead>
              <tr><th>Order</th><th>Category</th><th>Chip color</th><th>Key prefix</th><th>Naming</th><th></th></tr>
            </thead>
            <tbody>
              <template v-for="(c, i) in cats" :key="c.name">
                <tr>
                  <td style="width:1%; white-space:nowrap;">
                    <Button size="small" icon="pi pi-arrow-up" severity="secondary" text
                            :disabled="i === 0" @click="move_cat(i, -1)" />
                    <Button size="small" icon="pi pi-arrow-down" severity="secondary" text
                            :disabled="i === cats.length - 1" @click="move_cat(i, 1)" />
                  </td>
                  <td>{{ c.name }}</td>
                  <td style="width:1%; white-space:nowrap;">
                    <div class="color-cell">
                      <ColorPicker v-model="c.color" format="hex" />
                      <span class="chip-preview" :style="chip_style(c)">{{ c.name }}</span>
                    </div>
                  </td>
                  <td>
                    <InputText v-model="c.prefix" style="width:8rem;"
                               :invalid="!!c.prefix && !PREFIX_RE.test(c.prefix)" />
                  </td>
                  <td>
                    <SelectButton v-model="c.mode" :options="MODES" option-label="label"
                                  option-value="value" :allow-empty="false" size="small" />
                  </td>
                  <td class="actions">
                    <Button size="small" icon="pi pi-trash" severity="danger" text
                            @click="remove_cat(c.name)" />
                  </td>
                </tr>
                <tr v-if="c.mode === 'named'">
                  <td></td>
                  <td colspan="5" style="padding-top:0;">
                    <Textarea v-model="c.names_text" rows="4" style="width:100%;"
                              placeholder="One name per line — e.g.&#10;Staff Room&#10;Track 1&#10;Track 2" />
                    <div class="hint" style="margin:0.25rem 0 0;">
                      {{ cat_names(c).length }} name{{ cat_names(c).length === 1 ? '' : 's' }}
                      → <code>{{ c.prefix }}1</code>…<code>{{ c.prefix }}{{ Math.max(1, cat_names(c).length) }}</code>
                    </div>
                  </td>
                </tr>
              </template>
              <tr v-if="!cats.length"><td colspan="6" class="empty">No categories yet.</td></tr>
            </tbody>
          </table>
          <div class="add-row">
            <InputText v-model="new_cat" placeholder="Category name (e.g. Security)"
                       style="flex:1;" @keyup.enter="add_cat" />
            <Button label="Add Category" :disabled="!new_cat.trim()" @click="add_cat" />
          </div>
          <Message v-if="bad_prefix.length" severity="error" :closable="false" class="warn">
            Key prefix must be lowercase letters, numbers or hyphens: {{ bad_prefix.join(', ') }}
          </Message>
          <Message v-if="dup_prefix.length" severity="error" :closable="false" class="warn">
            Two categories share the key prefix "{{ dup_prefix.join('", "') }}" — give each its own.
          </Message>
          <Message v-if="empty_named.length" severity="error" :closable="false" class="warn">
            Add at least one name to {{ empty_named.join(', ') }}, or switch it to Numbered.
          </Message>
          <div class="nav">
            <Button label="Back" severity="secondary" icon="pi pi-arrow-left" @click="go('1')" />
            <Button label="Next" icon="pi pi-arrow-right" iconPos="right"
                    :disabled="!can_next" @click="go('3')" />
          </div>
        </StepPanel>

        <!-- ── times ───────────────────────────────────────────────────────── -->
        <StepPanel value="3">
          <p class="hint">
            Add each shift and tick the days that run it. Two days can start at the same
            hour with different end times — they just get their own key.
          </p>
          <table class="se-table">
            <thead>
              <tr>
                <th>Key</th><th>Range</th>
                <th v-for="d in days" :key="d.day_key" class="day-col"
                    v-tooltip.top="d.day_str">{{ d.day_str.slice(0, 3) }}</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="t in times" :key="t.time_key">
                <td><code>{{ t.time_key }}</code></td>
                <td>{{ t.time_str }}</td>
                <td v-for="d in days" :key="d.day_key" class="day-col">
                  <Checkbox :model-value="t.day_keys.includes(d.day_key)" binary
                            @update:model-value="toggle_time_day(t, d.day_key)" />
                </td>
                <td class="actions">
                  <Button size="small" icon="pi pi-trash" severity="danger" text
                          @click="remove_time(t.time_key)" />
                </td>
              </tr>
              <tr v-if="!times.length">
                <td :colspan="3 + days.length" class="empty">No times yet.</td>
              </tr>
            </tbody>
          </table>
          <div class="add-row">
            <input type="time" v-model="new_start" class="time-input" />
            <span class="sep">–</span>
            <input type="time" v-model="new_end" class="time-input" />
            <code v-if="new_time_key" class="key-badge" :class="{ 'key-alt': new_key_taken }"
                  v-tooltip.top="new_key_taken
                    ? 'Another shift already starts at this hour, so the end time is part of the key'
                    : 'Key this time will be stored under'">{{ new_time_key }}</code>
            <Button label="Add Time" :disabled="!new_start || !new_end || !new_days.length"
                    @click="add_time" />
          </div>
          <div class="add-opts">
            <span class="add-opts-label">Days</span>
            <span v-for="d in days" :key="d.day_key" class="opt">
              <Checkbox v-model="new_days" :value="d.day_key" :inputId="`nt_${d.day_key}`" />
              <label :for="`nt_${d.day_key}`">{{ d.day_str }}</label>
            </span>
          </div>
          <div class="nav">
            <Button label="Back" severity="secondary" icon="pi pi-arrow-left" @click="go('2')" />
            <Button label="Next" icon="pi pi-arrow-right" iconPos="right"
                    :disabled="!can_next" @click="go('4')" />
          </div>
        </StepPanel>

        <!-- ── positions grid ──────────────────────────────────────────────── -->
        <StepPanel value="4">
          <p class="hint">
            How many of each position you need at each time. Leave a cell at 0 where that
            category doesn't work that shift.
          </p>
          <div class="grid-scroll">
            <table class="se-table grid">
              <thead>
                <tr>
                  <th rowspan="2">Category</th>
                  <th rowspan="2" class="fill-col">All</th>
                  <th v-for="d in day_spans" :key="d.day_key" :colspan="d.span"
                      class="day-group">{{ d.day_str }}</th>
                </tr>
                <tr>
                  <th v-for="c in columns" :key="c.key" class="time-col">{{ c.time_str }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="cat in cats" :key="cat.name">
                  <td class="cat-cell">
                    {{ cat.name }}
                    <span class="cap">{{ cat.mode === 'named' ? `max ${cat_max(cat)}` : 'auto' }}</span>
                  </td>
                  <td class="fill-col">
                    <input type="number" min="0" class="grid-input fill"
                           placeholder="—" @input="fill_row(cat, $event.target.value)" />
                  </td>
                  <td v-for="c in columns" :key="c.key">
                    <input type="number" min="0" class="grid-input"
                           :class="{ over: (Number(cat.counts[c.key]) || 0) > cat_max(cat) }"
                           v-model.number="cat.counts[c.key]" />
                  </td>
                </tr>
                <tr v-if="!columns.length">
                  <td colspan="2" class="empty">
                    No day runs any time yet — go back and tick some days on the Times step.
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <Message v-if="totals.over" severity="warn" :closable="false" class="warn">
            A count is higher than its category offers. Numbered categories grow to fit;
            named ones are capped at their list, so extra slots there are dropped.
          </Message>

          <div class="summary">
            Creates <strong>{{ totals.positions }}</strong>
            position{{ totals.positions === 1 ? '' : 's' }} and
            <strong>{{ totals.slots }}</strong> schedule slot{{ totals.slots === 1 ? '' : 's' }}
            across {{ columns.length }} day/time combination{{ columns.length === 1 ? '' : 's' }}.
          </div>

          <Message v-if="result" severity="success" :closable="false">
            Created {{ result.positions_created }} position{{ result.positions_created === 1 ? '' : 's' }}
            and {{ result.slots_created }} slot{{ result.slots_created === 1 ? '' : 's' }}
            across {{ result.days }} day{{ result.days === 1 ? '' : 's' }}.
          </Message>

          <div class="nav">
            <Button label="Back" severity="secondary" icon="pi pi-arrow-left" @click="go('3')" />
            <Button label="Create Schedule" icon="pi pi-check" :loading="saving"
                    :disabled="!totals.slots || !!result" @click="create_schedule" />
          </div>
        </StepPanel>
      </StepPanels>
    </Stepper>
  </div>
</template>

<style scoped>
@import '../schedule-editor/editor.css';

.build-wrap { max-width: 62rem; margin: 0 auto; padding: 2.5rem 1.5rem; }
.build-head { margin-bottom: 1.25rem; }
.hint  { color: var(--p-text-muted-color); font-size: 0.875rem; margin: 0 0 0.9rem; line-height: 1.6; }

.color-cell { display: flex; align-items: center; gap: 0.5rem; }

/* Miniature of the schedule chip, so the picked color is judged as it will be seen. */
.chip-preview {
  border-radius: 20px;
  padding: 3px 12px;
  font-size: 0.8rem;
  font-weight: 600;
  white-space: nowrap;
}
.warn  { margin-top: 0.75rem; }
.nav   { display: flex; justify-content: space-between; gap: 0.5rem; margin-top: 1.5rem; }

.time-input {
  background: var(--p-inputtext-background);
  color: var(--p-inputtext-color);
  border: 1px solid var(--p-inputtext-border-color);
  border-radius: var(--p-inputtext-border-radius);
  padding: 0.5rem 0.75rem;
  font: inherit;
  outline: none;
  color-scheme: dark;
}
.time-input:focus {
  border-color: var(--p-inputtext-focus-border-color);
  box-shadow: 0 0 0 var(--p-focus-ring-width) var(--p-focus-ring-color);
}
.day-col { text-align: center; width: 1%; white-space: nowrap; }
.key-alt { color: var(--p-orange-400); }

/* counts grid */
.grid-scroll { overflow-x: auto; }
.grid { min-width: max-content; }
.grid th, .grid td { text-align: center; padding: 0.35rem 0.5rem; }
.grid .cat-cell { text-align: left; white-space: nowrap; }
.day-group {
  text-align: center !important;
  border-left: 1px solid var(--p-content-border-color);
  border-bottom: 1px solid var(--p-content-border-color);
}
.time-col { text-align: center !important; font-weight: 500; }
.fill-col { border-right: 1px solid var(--p-content-border-color); }
.cap { display: block; font-size: 0.7rem; color: var(--p-text-muted-color); font-weight: 400; }
.grid-input {
  width: 3.4rem;
  text-align: center;
  background: var(--p-inputtext-background);
  color: var(--p-inputtext-color);
  border: 1px solid var(--p-inputtext-border-color);
  border-radius: var(--p-inputtext-border-radius);
  padding: 0.3rem 0.25rem;
  font: inherit;
  outline: none;
  color-scheme: dark;
}
.grid-input:focus {
  border-color: var(--p-inputtext-focus-border-color);
  box-shadow: 0 0 0 var(--p-focus-ring-width) var(--p-focus-ring-color);
}
.grid-input.fill { border-style: dashed; }
.grid-input.over { border-color: var(--p-orange-400); }
</style>
