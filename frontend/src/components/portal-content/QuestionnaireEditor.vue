<script setup>
// Volunteer Portal → Questionnaire: the signup form's questions. Each question's key, and each
// option's value, is made from its label when it's added and never changes afterwards, so
// stored answers stay attached to it. Removing a question keeps the answers already given;
// the editor says how many there are first. The server checks everything again
// (portal_content.validate_fields), and so does the portal.
import { ref, computed, onMounted } from 'vue'
import Button    from 'primevue/button'
import InputText from 'primevue/inputtext'
import InputNumber from 'primevue/inputnumber'
import Textarea  from 'primevue/textarea'
import Select    from 'primevue/select'
import Checkbox  from 'primevue/checkbox'
import Message   from 'primevue/message'
import Tag       from 'primevue/tag'
import { usePortalContent } from '../../composables/usePortalContent.js'

const { data, load, current, save } = usePortalContent()

const fields    = ref(null)
const msg       = ref({ severity: '', text: '' })
const busy      = ref(false)
const new_label = ref('')
const removing  = ref(null)    // index awaiting "Remove? Yes"

onMounted(async () => { await load(); fields.value = current('questionnaire') })

const TYPES = [
  { value: 'choice', label: 'Choice (pick one)' },
  { value: 'select', label: 'Dropdown' },
  { value: 'check',  label: 'Checkbox (optional)' },
  { value: 'ack',    label: 'Acknowledgement (must tick)' },
  { value: 'text',   label: 'Text' },
]
const SECTIONS = [
  { value: 'shifts',     label: 'With the shifts' },
  { value: 'additional', label: 'Additional questions' },
]
const counts = computed(() => data.value?.answer_counts || {})

function slug(text, fallback) {
  const s = (text || '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '')
    .slice(0, 36)
  return /^[a-z]/.test(s) ? s : `${fallback}_${s}`.slice(0, 36)
}
function all_keys() {
  const out = new Set()
  for (const f of fields.value) {
    out.add(f.key)
    for (const o of f.options || []) if (o.detail) out.add(o.detail.key)
  }
  return out
}
function unique(base, taken) {
  let key = base
  for (let n = 2; taken.has(key); n++) key = `${base}_${n}`
  return key
}

function add_question() {
  const label = new_label.value.trim()
  if (!label) return
  fields.value.push({ key: unique(slug(label, 'q'), all_keys()), section: 'additional',
                      type: 'text', required: false, editable: false, label, max: 300 })
  new_label.value = ''
}

function set_type(f, type) {
  f.type = type
  if (type === 'ack') f.required = true
  if (type === 'check') f.required = false
  if (type === 'choice' || type === 'select') {
    f.options ||= [{ value: 'yes', label: 'Yes' }, { value: 'no', label: 'No' }]
    if (type === 'select') for (const o of f.options) delete o.detail
  } else delete f.options
  if (type === 'text') f.max ||= 300
  else delete f.max
}

function add_option(f) {
  const values = new Set(f.options.map(o => o.value))
  f.options.push({ value: unique(`option_${f.options.length + 1}`, values), label: '', _new: true })
}
// A new option's value follows its label until it's saved.
function option_label(f, o) {
  if (!o._new) return
  const values = new Set(f.options.filter(x => x !== o).map(x => x.value))
  o.value = unique(slug(o.label, 'o').replace(/^o_/, '') || 'option', values)
}
function toggle_detail(f, o, on) {
  if (on) o.detail = { key: unique(`${f.key}_${o.value}`.slice(0, 36), all_keys()),
                       label: 'Please say more', max: 200 }
  else delete o.detail
}

function move(i, d) {
  const j = i + d
  if (j < 0 || j >= fields.value.length) return
  ;[fields.value[i], fields.value[j]] = [fields.value[j], fields.value[i]]
}
function answered(f) {
  return [f.key, ...(f.options || []).filter(o => o.detail).map(o => o.detail.key)]
    .reduce((n, k) => Math.max(n, counts.value[k] || 0), 0)
}

function clean() {
  return fields.value.map(f => ({
    ...f, options: f.options?.map(({ _new, ...o }) => o),
  })).map(f => { if (!f.options) delete f.options; return f })
}

async function do_save() {
  busy.value = true
  msg.value = { severity: '', text: '' }
  const r = await save('questionnaire', clean())
  busy.value = false
  if (r.ok) {
    fields.value = current('questionnaire')
    msg.value = { severity: 'success', text: 'Saved. New signups get it within a few seconds.' }
  } else msg.value = { severity: 'error', text: r.body.detail || 'Saving failed.' }
}

async function do_reset() {
  busy.value = true
  const r = await save('questionnaire', null)
  busy.value = false
  if (r.ok) {
    fields.value = current('questionnaire')
    msg.value = { severity: 'success', text: 'Back to the default questions.' }
  } else msg.value = { severity: 'error', text: r.body.detail || 'Reset failed.' }
}
</script>

<template>
  <div v-if="fields && data" class="editor">
    <p class="hint">
      The signup form asks these in order, on the step chosen for each. Answers are stored as
      plain text, so don't ask for anything sensitive: no phone numbers, no emergency contacts.
      "Can change later" lets volunteers edit their answer from their own page.
    </p>

    <div v-for="(f, i) in fields" :key="f.key" class="q">
      <div class="q-head">
        <code class="key">{{ f.key }}</code>
        <Tag v-if="f.key === data.flexible_key" severity="info" value="Used by the assignment picker" />
        <span v-if="answered(f)" class="muted">{{ answered(f) }} answered</span>
        <span class="spacer" />
        <Button icon="pi pi-arrow-up" text size="small" aria-label="Up" @click="move(i, -1)" />
        <Button icon="pi pi-arrow-down" text size="small" aria-label="Down" @click="move(i, 1)" />
        <template v-if="removing === i">
          <span class="warn">
            Remove?{{ answered(f) ? ` The ${answered(f)} answers already given are kept, but no longer asked.` : '' }}
          </span>
          <Button label="Remove" size="small" severity="danger"
                  @click="fields.splice(i, 1); removing = null" />
          <Button label="Keep" size="small" text @click="removing = null" />
        </template>
        <Button v-else icon="pi pi-trash" text size="small" severity="danger" aria-label="Remove"
                @click="removing = i" />
      </div>

      <Textarea v-model="f.label" rows="1" autoResize fluid aria-label="Question" />
      <div class="q-row">
        <Select :modelValue="f.type" :options="TYPES" optionLabel="label" optionValue="value"
                :disabled="f.key === data.flexible_key" aria-label="Type"
                @update:modelValue="t => set_type(f, t)" />
        <Select v-model="f.section" :options="SECTIONS" optionLabel="label" optionValue="value"
                aria-label="Step" />
        <label class="chk">
          <Checkbox v-model="f.required" binary :disabled="f.type === 'ack' || f.type === 'check'" />
          Required
        </label>
        <label class="chk"><Checkbox v-model="f.editable" binary /> Can change later</label>
        <label v-if="f.type === 'text'" class="chk">
          Max <InputNumber v-model="f.max" :min="1" :max="2000" inputClass="num" />
        </label>
      </div>

      <div v-if="f.options" class="opts">
        <div v-for="(o, j) in f.options" :key="j" class="opt">
          <InputText v-model="o.label" placeholder="Option" class="opt-label"
                     @update:modelValue="option_label(f, o)" />
          <code class="key">{{ o.value }}</code>
          <label v-if="f.type === 'choice'" class="chk">
            <Checkbox :modelValue="!!o.detail" binary
                      @update:modelValue="on => toggle_detail(f, o, on)" />
            asks a follow-up
          </label>
          <Button icon="pi pi-times" text size="small" severity="danger" aria-label="Remove option"
                  :disabled="f.options.length <= 1" @click="f.options.splice(j, 1)" />
          <div v-if="o.detail" class="detail">
            <InputText v-model="o.detail.label" placeholder="Follow-up question" class="opt-label" />
            <span class="muted">max</span>
            <InputNumber v-model="o.detail.max" :min="1" :max="500" inputClass="num" />
          </div>
        </div>
        <Button label="Add option" icon="pi pi-plus" size="small" text
                :disabled="f.options.length >= 30" @click="add_option(f)" />
      </div>
    </div>

    <div class="add">
      <InputText v-model="new_label" placeholder="New question" fluid @keyup.enter="add_question" />
      <Button label="Add" icon="pi pi-plus" :disabled="!new_label.trim() || fields.length >= 40"
              @click="add_question" />
    </div>

    <Message v-if="msg.text" :severity="msg.severity" :closable="false">{{ msg.text }}</Message>
    <div class="actions">
      <Button label="Save" icon="pi pi-check" :loading="busy" @click="do_save" />
      <span class="spacer" />
      <Button label="Reset to default" severity="secondary" text :disabled="busy" @click="do_reset" />
    </div>
  </div>
</template>

<style scoped>
.editor  { display: flex; flex-direction: column; gap: 0.85rem; }
.hint    { font-size: 0.8rem; color: var(--p-text-muted-color); line-height: 1.6; margin: 0; }
.q       { border: 1px solid var(--p-content-border-color); border-radius: 4px; padding: 0.6rem 0.75rem;
           display: flex; flex-direction: column; gap: 0.5rem; }
.q-head  { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
.q-row   { display: flex; align-items: center; gap: 0.6rem; flex-wrap: wrap; }
.key     { font-size: 0.75rem; color: var(--p-text-muted-color); }
.muted   { font-size: 0.8rem; color: var(--p-text-muted-color); }
.warn    { font-size: 0.8rem; color: var(--p-orange-400); }
.chk     { display: inline-flex; align-items: center; gap: 0.35rem; font-size: 0.85rem; }
.opts    { display: flex; flex-direction: column; gap: 0.35rem; padding-left: 0.75rem;
           border-left: 2px solid var(--p-content-border-color); }
.opt     { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
.opt-label { flex: 1; min-width: 14rem; }
.detail  { display: flex; align-items: center; gap: 0.5rem; width: 100%; padding-left: 1.5rem; }
.add     { display: flex; gap: 0.5rem; }
.actions { display: flex; gap: 0.5rem; align-items: center; }
.spacer  { flex: 1; }
:deep(.num) { width: 4.5rem; }
</style>
