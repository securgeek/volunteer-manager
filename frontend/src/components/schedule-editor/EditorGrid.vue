<script setup>
import { ref, computed, watch } from 'vue'
import Button       from 'primevue/button'
import Message      from 'primevue/message'
import Textarea     from 'primevue/textarea'
import SelectButton from 'primevue/selectbutton'

const props = defineProps({
  days:       Array,
  times:      Array,
  dayTimes:   Array,
  positions:  Array,
  slots:      Array,
  categories: Array,
})
const emit = defineEmits(['reload'])

const err     = ref('')
const note    = ref('')
const blocked = ref([])
const saving  = ref(false)

const MODES = [
  { label: 'Numbered', value: 'numbered' },
  { label: 'Named',    value: 'named' },
]

// ── the grid's shape: one column per day/time pair that actually runs ─────────
const columns = computed(() => {
  const runs = new Set((props.dayTimes ?? []).map(dt => `${dt.day_key}|${dt.time_key}`))
  const out  = []
  for (const d of props.days) {
    for (const t of props.times) {
      if (runs.has(`${d.day_key}|${t.time_key}`))
        out.push({ key: `${d.day_key}|${t.time_key}`, day_key: d.day_key,
                   day_str: d.day_str, time_key: t.time_key, time_str: t.time_str })
    }
  }
  return out
})

const day_spans = computed(() =>
  props.days
    .map(d => ({ ...d, span: columns.value.filter(c => c.day_key === d.day_key).length }))
    .filter(d => d.span > 0))

function col_label(key) {
  const c = columns.value.find(x => x.key === key)
  return c ? `${c.day_str} ${c.time_str}` : key
}

/**
 * Decide which mode a category's row should open in, from the names it already has.
 *
 * Numbered only when every position is called exactly "{category} {n}", in order — those
 * names say nothing the count does not, so the name list stays out of the way. Anything
 * else is treated as named and shown, deliberately strictly: a real name must never be
 * hidden behind a toggle.
 */
function detect_mode(cat_name, labels) {
  if (!labels.length) return 'numbered'
  return labels.every((l, i) => l === `${cat_name} ${i + 1}`) ? 'numbered' : 'named'
}

// A row the organizer flipped by hand this session. Without this, applying changes
// reloads the props and snaps the row back to whatever detect_mode says.
const mode_override = ref({})
function set_mode(row, mode) {
  row.mode = mode
  mode_override.value[row.name] = mode
}

// ── current state, read straight off the live schedule ───────────────────────
// A cell's count is simply how many of that category's positions are scheduled there.
function read_rows() {
  const pos_cat = Object.fromEntries(props.positions.map(p => [p.pos_key, p.pos_cat]))

  const counted = {}          // cat -> cell -> n
  const assigned = {}         // cat -> cell -> n assigned
  for (const s of props.slots) {
    const cat  = pos_cat[s.pos_key]
    if (!cat) continue
    const cell = `${s.day_key}|${s.time_key}`
    counted[cat]  = counted[cat]  || {}
    assigned[cat] = assigned[cat] || {}
    counted[cat][cell]  = (counted[cat][cell] || 0) + 1
    if (s.assigned) assigned[cat][cell] = (assigned[cat][cell] || 0) + 1
  }

  return props.categories.map((c) => {
    const base = {}
    for (const col of columns.value) base[col.key] = counted[c.cat_name]?.[col.key] ?? 0
    // Existing display names, in the order the backend hands positions back — the same
    // order apply_grid maps counts onto.
    const labels = props.positions.filter(p => p.pos_cat === c.cat_name).map(p => p.pos_str)
    return {
      name:       c.cat_name,
      mode:       mode_override.value[c.cat_name] ?? detect_mode(c.cat_name, labels),
      names_text: labels.join('\n'),
      base_names: labels.join('\n'),
      counts:     { ...base },
      base:       { ...base },
      assigned:   assigned[c.cat_name] ?? {},
    }
  })
}

const rows = ref([])
watch([() => props.categories, () => props.positions, () => props.slots, columns],
      () => { rows.value = read_rows() },
      { immediate: true, deep: false })

function names_of(row) {
  return row.names_text.split('\n').map(s => s.trim()).filter(Boolean)
}

// A named category offers exactly as many positions as it has names; a numbered one grows.
function cat_max(row) {
  return row.mode === 'named' ? names_of(row).length : Infinity
}

function fill_row(row, raw) {
  const n = Math.max(0, Number(raw) || 0)
  for (const c of columns.value) row.counts[c.key] = n
}

// ── what applying would do ───────────────────────────────────────────────────
const pending = computed(() => {
  let added = 0, removed = 0, at_risk = 0, renames = 0
  for (const row of rows.value) {
    const cap = cat_max(row)
    for (const c of columns.value) {
      const want = Math.min(Number(row.counts[c.key]) || 0, cap)
      const have = row.base[c.key] ?? 0
      if (want > have) added += want - have
      if (want < have) {
        removed += have - want
        // Slots with volunteers are never taken away, so flag the overlap up front.
        const free = have - (row.assigned[c.key] ?? 0)
        if (have - want > free) at_risk += (have - want) - free
      }
    }
    if (row.mode === 'named' && row.names_text !== row.base_names) renames += 1
  }
  return { added, removed, at_risk, renames, dirty: added + removed + renames > 0 }
})

const over_cap = computed(() =>
  rows.value.some(r => columns.value.some(c => (Number(r.counts[c.key]) || 0) > cat_max(r))))

function reset() {
  rows.value = read_rows()
  note.value = ''
  blocked.value = []
}

async function apply() {
  err.value = ''
  note.value = ''
  blocked.value = []
  saving.value = true

  const payload = {
    cells: columns.value.map(c => c.key),
    categories: rows.value.map(r => ({
      name:   r.name,
      mode:   r.mode,
      names:  r.mode === 'named' ? names_of(r) : null,
      counts: Object.fromEntries(
        columns.value.map(c => [c.key, Math.max(0, Number(r.counts[c.key]) || 0)])),
    })),
  }

  const form = new FormData()
  form.append('config', JSON.stringify(payload))
  const resp = await fetch('/api/schedule/apply_grid', { method: 'POST', body: form })
  saving.value = false

  if (!resp.ok) {
    const b = await resp.json().catch(() => null)
    err.value = b?.detail || 'Failed to apply the grid.'
    return
  }
  const d = await resp.json()
  const bits = []
  if (d.slots_created)     bits.push(`added ${d.slots_created} slot${d.slots_created === 1 ? '' : 's'}`)
  if (d.slots_removed)     bits.push(`removed ${d.slots_removed} slot${d.slots_removed === 1 ? '' : 's'}`)
  if (d.positions_created) bits.push(`created ${d.positions_created} position${d.positions_created === 1 ? '' : 's'}`)
  if (d.positions_deleted) bits.push(`retired ${d.positions_deleted} unused position${d.positions_deleted === 1 ? '' : 's'}`)
  note.value    = bits.length ? bits.join(', ').replace(/^./, s => s.toUpperCase()) + '.' : 'No changes to apply.'
  blocked.value = d.blocked ?? []
  emit('reload')
}
</script>

<template>
  <Message v-if="err" severity="error" :closable="true" @close="err = ''"
           style="margin-bottom:0.75rem;">{{ err }}</Message>
  <Message v-if="note" severity="success" :closable="true" @close="note = ''"
           style="margin-bottom:0.75rem;">{{ note }}</Message>

  <Message v-if="blocked.length" severity="warn" :closable="true" @close="blocked = []"
           style="margin-bottom:0.75rem;">
    Some slots were kept because a volunteer is assigned to them — clear the assignment first
    if you really want them gone:
    <ul style="margin:0.4rem 0 0; padding-left:1.1rem;">
      <li v-for="b in blocked" :key="b.category + b.cell">
        {{ b.category }} at {{ col_label(b.cell) }} — {{ b.kept }} kept
      </li>
    </ul>
  </Message>

  <p class="hint">
    How many of each position you need at each time. Lowering a number removes empty slots;
    slots with a volunteer assigned are never taken away. Nothing is written until you press
    <strong>Apply Changes</strong>.
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
        <template v-for="row in rows" :key="row.name">
          <tr>
            <td class="cat-cell">
              {{ row.name }}
              <SelectButton :model-value="row.mode" :options="MODES" option-label="label"
                            option-value="value" :allow-empty="false" size="small"
                            style="margin-top:0.25rem;"
                            @update:model-value="set_mode(row, $event)" />
            </td>
            <td class="fill-col">
              <input type="number" min="0" class="grid-input fill"
                     placeholder="—" @input="fill_row(row, $event.target.value)" />
            </td>
            <td v-for="c in columns" :key="c.key">
              <input type="number" min="0" class="grid-input"
                     :class="{ over:    (Number(row.counts[c.key]) || 0) > cat_max(row),
                               changed: (Number(row.counts[c.key]) || 0) !== row.base[c.key] }"
                     v-model.number="row.counts[c.key]" />
            </td>
          </tr>
          <tr v-if="row.mode === 'named'">
            <td></td>
            <td :colspan="1 + columns.length" style="padding-top:0; text-align:left;">
              <Textarea v-model="row.names_text" rows="4" style="width:100%; max-width:32rem;"
                        placeholder="One name per line — e.g.&#10;Staff Room&#10;Track 1&#10;Track 2" />
              <div class="hint" style="margin:0.25rem 0 0;">
                {{ names_of(row).length }} name{{ names_of(row).length === 1 ? '' : 's' }} —
                these become the display names of this category's positions, in order.
              </div>
            </td>
          </tr>
        </template>
        <tr v-if="!rows.length">
          <td :colspan="2 + columns.length" class="empty">
            No categories yet — add some on the Categories tab.
          </td>
        </tr>
        <tr v-if="!columns.length">
          <td :colspan="2" class="empty">
            No day runs any time yet — set that up on the Times tab.
          </td>
        </tr>
      </tbody>
    </table>
  </div>

  <Message v-if="over_cap" severity="warn" :closable="false" style="margin-top:0.75rem;">
    A count is higher than its category offers. Numbered categories grow to fit; named ones
    are capped at their list, so the extra slots there are not created.
  </Message>

  <div class="grid-foot">
    <div class="summary">
      <template v-if="pending.dirty">
        <strong v-if="pending.added">+{{ pending.added }}</strong>
        <span v-if="pending.added"> slot{{ pending.added === 1 ? '' : 's' }}</span>
        <span v-if="pending.added && pending.removed">, </span>
        <strong v-if="pending.removed">−{{ pending.removed }}</strong>
        <span v-if="pending.removed"> slot{{ pending.removed === 1 ? '' : 's' }}</span>
        <span v-if="pending.renames">
          {{ pending.added || pending.removed ? ', ' : '' }}renaming
          {{ pending.renames }} categor{{ pending.renames === 1 ? 'y' : 'ies' }}
        </span>
        <span v-if="pending.at_risk" class="at-risk">
          — {{ pending.at_risk }} of those {{ pending.at_risk === 1 ? 'is' : 'are' }} assigned
          and will be kept
        </span>
      </template>
      <span v-else class="muted">No pending changes.</span>
    </div>
    <div style="display:flex; gap:0.5rem;">
      <Button v-if="pending.dirty" label="Reset" severity="secondary" @click="reset" />
      <Button label="Apply Changes" icon="pi pi-check" :loading="saving"
              :disabled="!pending.dirty" @click="apply" />
    </div>
  </div>
</template>

<style scoped>
@import './editor.css';

.hint { color: var(--p-text-muted-color); font-size: 0.875rem; margin: 0 0 0.9rem; line-height: 1.6; }
.muted { color: var(--p-text-muted-color); }

.grid-scroll { overflow-x: auto; }
.grid { min-width: max-content; }
.grid th, .grid td { text-align: center; padding: 0.35rem 0.5rem; }
.grid .cat-cell { text-align: left; white-space: nowrap; vertical-align: top; }
.day-group {
  text-align: center !important;
  border-left: 1px solid var(--p-content-border-color);
  border-bottom: 1px solid var(--p-content-border-color);
}
.time-col { text-align: center !important; font-weight: 500; }
.fill-col { border-right: 1px solid var(--p-content-border-color); }

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
/* An edited cell, so what is about to be written is visible before pressing Apply. */
.grid-input.changed { border-color: var(--p-primary-color); font-weight: 600; }

.grid-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  margin-top: 1rem;
  flex-wrap: wrap;
}
.summary { font-size: 0.875rem; }
.at-risk { color: var(--p-orange-400); }
</style>
