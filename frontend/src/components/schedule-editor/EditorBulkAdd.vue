<script setup>
import { ref, reactive, computed, watch } from 'vue'
import Select      from 'primevue/select'
import Checkbox    from 'primevue/checkbox'
import InputNumber from 'primevue/inputnumber'
import InputText   from 'primevue/inputtext'
import Button      from 'primevue/button'
import Message     from 'primevue/message'

const props = defineProps({
  days: Array, times: Array, dayTimes: Array, positions: Array, categories: Array,
})
const emit  = defineEmits(['reload'])

const cat_names = computed(() => props.categories.map(c => c.cat_name))

const KEY_RE = /^[a-z0-9][a-z0-9-]*$/

const err         = ref('')
const bulk_form   = reactive({
  cat: '', prefix: '', label: '', count: 3, start: 1, day_keys: [], time_keys: [],
})
const bulk_saving = ref(false)
const bulk_result = ref(null)

// prefix and label track the category until you edit them yourself.
const prefix_dirty = ref(false)
const label_dirty  = ref(false)
const start_dirty  = ref(false)

const show_new_cat = ref(false)
const new_cat_name = ref('')
const cat_adding   = ref(false)

function derive_prefix(cat) { return cat.toLowerCase().replace(/[^a-z0-9]/g, '') || 'pos' }

// Pre-select every day and time, and keep in step as new ones are defined.
watch(() => props.days, (days) => {
  if (bulk_form.day_keys.length === 0) bulk_form.day_keys = days.map(d => d.day_key)
}, { immediate: true })

watch(() => props.times, (times) => {
  if (bulk_form.time_keys.length === 0) bulk_form.time_keys = times.map(t => t.time_key)
}, { immediate: true })

watch(() => props.categories, (cats) => {
  if (!bulk_form.cat && cats.length) bulk_form.cat = cats[0].cat_name
}, { immediate: true })

watch(() => bulk_form.cat, (cat) => {
  if (!cat) return
  if (!prefix_dirty.value) bulk_form.prefix = derive_prefix(cat)
  if (!label_dirty.value)  bulk_form.label  = cat
}, { immediate: true })

// Continue an existing run rather than restarting at 1.
watch(() => bulk_form.prefix, (prefix) => {
  if (start_dirty.value || !prefix) return
  const re = new RegExp(`^${prefix.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(\\d+)$`)
  const used = (props.positions ?? [])
    .map(p => re.exec(p.pos_key)?.[1])
    .filter(Boolean)
    .map(Number)
  bulk_form.start = used.length ? Math.max(...used) + 1 : 1
}, { immediate: true })

const prefix_invalid = computed(() => !!bulk_form.prefix && !KEY_RE.test(bulk_form.prefix))

const all_days  = computed({
  get: () => props.days.length  > 0 && bulk_form.day_keys.length  === props.days.length,
  set: (v) => { bulk_form.day_keys  = v ? props.days.map(d => d.day_key)  : [] },
})
const all_times = computed({
  get: () => props.times.length > 0 && bulk_form.time_keys.length === props.times.length,
  set: (v) => { bulk_form.time_keys = v ? props.times.map(t => t.time_key) : [] },
})

const bulk_preview = computed(() => {
  const { prefix, label, count, start, day_keys, time_keys } = bulk_form
  if (!bulk_form.cat || !prefix || prefix_invalid.value) return null
  if (!day_keys.length || !time_keys.length || count < 1 || start < 1) return null

  const nums  = Array.from({ length: count }, (_, i) => start + i)
  const keys  = nums.map(n => `${prefix}${n}`)
  const names = keys.length > 4
    ? `${keys[0]}, ${keys[1]} … ${keys[keys.length - 1]}`
    : keys.join(', ')

  // Only day/time pairs the schedule actually runs get slots.
  const declared = new Set(props.dayTimes.map(dt => `${dt.day_key}|${dt.time_key}`))
  let pairs = 0
  const gaps = []
  for (const dk of day_keys) {
    for (const tk of time_keys) {
      if (declared.has(`${dk}|${tk}`)) pairs++
      else gaps.push({ dk, tk })
    }
  }

  const day_lbl  = Object.fromEntries(props.days.map(d => [d.day_key, d.day_str]))
  const time_lbl = Object.fromEntries(props.times.map(t => [t.time_key, t.time_str]))
  const gap_note = gaps.length
    ? gaps.slice(0, 3).map(g => `${day_lbl[g.dk] ?? g.dk} has no ${time_lbl[g.tk] ?? g.tk}`).join('; ')
      + (gaps.length > 3 ? `; and ${gaps.length - 3} more` : '')
    : ''

  return {
    names, label_sample: `${label || bulk_form.cat} ${start}`,
    slots: pairs * count, skipped: gaps.length * count, gap_note,
  }
})

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

async function add_quick_cat() {
  const name = new_cat_name.value.trim()
  if (!name) return
  cat_adding.value = true
  const ok = await api_post('/api/schedule/upsert_category', { cat_name: name })
  cat_adding.value = false
  if (ok) {
    bulk_form.cat      = name
    new_cat_name.value = ''
    show_new_cat.value = false
    emit('reload')
  }
}

async function bulk_add() {
  if (!bulk_preview.value) return
  bulk_saving.value = true
  bulk_result.value = null
  const data = await api_post('/api/schedule/bulk_add_slots', {
    cat:       bulk_form.cat,
    prefix:    bulk_form.prefix,
    label:     bulk_form.label,
    count:     bulk_form.count,
    start:     bulk_form.start,
    day_keys:  bulk_form.day_keys.join(','),
    time_keys: bulk_form.time_keys.join(','),
  })
  bulk_saving.value = false
  if (data) {
    bulk_result.value = data
    // Advance past what was just created so a second Apply appends instead of no-opping.
    bulk_form.start  = bulk_form.start + bulk_form.count
    start_dirty.value = true
    emit('reload')
  }
}
</script>

<template>
  <Message v-if="err" severity="error" :closable="true" @close="err = ''"
           style="margin-bottom:0.75rem;">{{ err }}</Message>

  <div style="display:flex; flex-direction:column; gap:1.25rem; max-width:40rem;">

    <div class="bulk-field">
      <label class="bulk-label">Category</label>
      <Select v-model="bulk_form.cat" :options="cat_names"
              placeholder="Pick a category" style="width:14rem;" />
      <Button icon="pi pi-plus" size="small" severity="secondary" text
              title="Add new category" @click="show_new_cat = !show_new_cat" />
    </div>

    <div v-if="show_new_cat"
         style="display:flex; gap:0.5rem; align-items:center; margin-top:-0.75rem;">
      <InputText v-model="new_cat_name" placeholder="New category name"
                 style="flex:1;" @keyup.enter="add_quick_cat" />
      <Button size="small" label="Add" :loading="cat_adding"
              :disabled="!new_cat_name.trim()" @click="add_quick_cat" />
      <Button size="small" icon="pi pi-times" severity="secondary" text
              @click="show_new_cat = false; new_cat_name = ''" />
    </div>

    <div class="bulk-field">
      <label class="bulk-label">Key prefix</label>
      <InputText v-model="bulk_form.prefix" :invalid="prefix_invalid"
                 placeholder="e.g. sec" style="width:14rem;"
                 @input="prefix_dirty = true" />
      <span v-if="prefix_invalid" class="key-err">
        Lowercase letters, numbers, or hyphens only.
      </span>
    </div>

    <div class="bulk-field">
      <label class="bulk-label">Display label</label>
      <InputText v-model="bulk_form.label" placeholder="e.g. Security"
                 style="width:14rem;" @input="label_dirty = true" />
    </div>

    <div class="bulk-field">
      <label class="bulk-label">Number of slots</label>
      <InputNumber v-model="bulk_form.count" :min="1" :max="50"
                   showButtons style="width:8rem;" />
      <label class="bulk-label" style="width:auto;">Start at</label>
      <InputNumber v-model="bulk_form.start" :min="1" :max="999"
                   showButtons style="width:8rem;"
                   @input="start_dirty = true" />
    </div>

    <div class="bulk-field" style="align-items:flex-start;">
      <label class="bulk-label" style="padding-top:0.25rem;">Days</label>
      <div style="display:flex; flex-direction:column; gap:0.5rem;">
        <div class="opt">
          <Checkbox v-model="all_days" binary inputId="ball_days" />
          <label for="ball_days" class="all-label">All</label>
        </div>
        <div v-for="d in days" :key="d.day_key" class="opt">
          <Checkbox v-model="bulk_form.day_keys" :value="d.day_key"
                    :inputId="`bday_${d.day_key}`" />
          <label :for="`bday_${d.day_key}`">{{ d.day_str }}</label>
        </div>
        <span v-if="days.length === 0" class="bulk-label">No days defined yet.</span>
      </div>
    </div>

    <div class="bulk-field" style="align-items:flex-start;">
      <label class="bulk-label" style="padding-top:0.25rem;">Times</label>
      <div style="display:flex; flex-direction:column; gap:0.5rem;">
        <div class="opt">
          <Checkbox v-model="all_times" binary inputId="ball_times" />
          <label for="ball_times" class="all-label">All</label>
        </div>
        <div v-for="t in times" :key="t.time_key" class="opt">
          <Checkbox v-model="bulk_form.time_keys" :value="t.time_key"
                    :inputId="`btime_${t.time_key}`" />
          <label :for="`btime_${t.time_key}`">{{ t.time_str }}</label>
        </div>
        <span v-if="times.length === 0" class="bulk-label">No times defined yet.</span>
      </div>
    </div>

    <div v-if="bulk_preview" class="bulk-preview">
      Positions: <strong>{{ bulk_preview.names }}</strong>
      &nbsp;·&nbsp; labelled <strong>{{ bulk_preview.label_sample }}</strong>, …<br>
      Creates up to <strong>{{ bulk_preview.slots }}</strong>
      schedule slot{{ bulk_preview.slots !== 1 ? 's' : '' }}. Existing slots are skipped.
      <template v-if="bulk_preview.skipped">
        <br><span class="gap-note">
          {{ bulk_preview.skipped }} not created — {{ bulk_preview.gap_note }}.
        </span>
      </template>
    </div>

    <Message v-if="bulk_result" severity="success" :closable="true"
             @close="bulk_result = null">
      Created {{ bulk_result.positions_created }} new position{{ bulk_result.positions_created !== 1 ? 's' : '' }},
      {{ bulk_result.slots_created }} new slot{{ bulk_result.slots_created !== 1 ? 's' : '' }}.
    </Message>

    <div>
      <Button label="Apply" :loading="bulk_saving"
              :disabled="!bulk_preview" @click="bulk_add" />
    </div>

  </div>
</template>

<style scoped>
@import './editor.css';
.bulk-field {
  display: flex;
  align-items: center;
  gap: 1rem;
}
.bulk-label {
  width: 9rem;
  flex-shrink: 0;
  font-size: 0.875rem;
  color: var(--p-text-muted-color);
}
.bulk-preview {
  font-size: 0.875rem;
  color: var(--p-text-muted-color);
  background: var(--p-content-hover-background);
  border: 1px solid var(--p-content-border-color);
  border-radius: 6px;
  padding: 0.75rem 1rem;
  line-height: 1.7;
}
.all-label { color: var(--p-text-muted-color); font-size: 0.8rem; text-transform: uppercase; }
.gap-note  { color: var(--p-orange-400); }
.key-err   { color: var(--p-red-400); font-size: 0.8rem; }
</style>
