<script setup>
import { ref, reactive, computed } from 'vue'
import InputText from 'primevue/inputtext'
import Select    from 'primevue/select'
import Checkbox  from 'primevue/checkbox'
import Button    from 'primevue/button'
import Message   from 'primevue/message'

const props = defineProps({ positions: Array, categories: Array })
const emit  = defineEmits(['reload'])

const cat_names = computed(() => props.categories.map(c => c.cat_name))

const err        = ref('')
const note       = ref('')
const pos_edit   = ref(null)
const pos_form   = reactive({ pos_key: '', pos_cat: '', pos_str: '', fill_all: true })
const pos_saving = ref(false)

// The key follows the display name until you type one yourself.
const key_dirty = ref(false)
const KEY_RE    = /^[a-z0-9][a-z0-9-]*$/

function sanitize_key(s) {
  return s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '')
}
function on_name_input() {
  if (pos_edit.value === null && !key_dirty.value)
    pos_form.pos_key = sanitize_key(pos_form.pos_str)
}
const key_invalid = computed(() => !!pos_form.pos_key && !KEY_RE.test(pos_form.pos_key))

// inline quick-add category
const show_new_cat  = ref(false)
const new_cat_name  = ref('')
const cat_adding    = ref(false)

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

async function add_quick_cat(target_form) {
  const name = new_cat_name.value.trim()
  if (!name) return
  cat_adding.value = true
  const ok = await api_post('/api/schedule/upsert_category', { cat_name: name })
  cat_adding.value = false
  if (ok) {
    target_form.pos_cat = name
    new_cat_name.value  = ''
    show_new_cat.value  = false
    emit('reload')
  }
}

function pos_start_edit(p) {
  pos_edit.value = p.pos_key
  show_new_cat.value = false
  key_dirty.value = true
  Object.assign(pos_form, { pos_key: p.pos_key, pos_cat: p.pos_cat, pos_str: p.pos_str })
}
function pos_cancel() {
  pos_edit.value = null
  show_new_cat.value = false
  key_dirty.value = false
  Object.assign(pos_form, { pos_key: '', pos_cat: '', pos_str: '', fill_all: true })
}
async function pos_save() {
  if (!pos_form.pos_key.trim() || !pos_form.pos_cat.trim() || !pos_form.pos_str.trim()) return
  if (key_invalid.value) return
  const adding = pos_edit.value === null
  pos_saving.value = true
  note.value = ''
  // fill_all applies to new positions only — relabeling one must not create slots.
  const d = await api_post('/api/schedule/upsert_position', {
    pos_key: pos_form.pos_key.trim(),
    pos_cat: pos_form.pos_cat.trim(),
    pos_str: pos_form.pos_str.trim(),
    ...(adding ? { fill_all: pos_form.fill_all } : {}),
  })
  pos_saving.value = false
  if (d) {
    if (d.slots_created)
      note.value = `Created ${d.slots_created} slot${d.slots_created !== 1 ? 's' : ''}.`
    pos_cancel()
    emit('reload')
  }
}
async function pos_delete(pos_key) {
  if (await api_post('/api/schedule/delete_position', { pos_key })) emit('reload')
}
</script>

<template>
  <Message v-if="err" severity="error" :closable="true" @close="err = ''"
           style="margin-bottom:0.75rem;">{{ err }}</Message>
  <Message v-if="note" severity="success" :closable="true" @close="note = ''"
           style="margin-bottom:0.75rem;">{{ note }}</Message>
  <table class="se-table">
    <thead><tr><th>Key</th><th>Category</th><th>Display name</th><th></th></tr></thead>
    <tbody>
      <tr v-for="p in positions" :key="p.pos_key">
        <template v-if="pos_edit === p.pos_key">
          <td colspan="3">
            <div style="display:flex; gap:0.5rem; align-items:center; flex-wrap:wrap;">
              <InputText :model-value="p.pos_key" disabled style="width:8rem;" />
              <Select v-model="pos_form.pos_cat" :options="cat_names" editable
                      placeholder="Category" style="width:13rem;" />
              <Button icon="pi pi-plus" size="small" severity="secondary" text
                      title="Add new category" @click="show_new_cat = !show_new_cat" />
              <InputText v-model="pos_form.pos_str" placeholder="Display name"
                         style="flex:1; min-width:8rem;" @keyup.enter="pos_save" />
            </div>
            <div v-if="show_new_cat"
                 style="display:flex; gap:0.5rem; align-items:center; margin-top:0.5rem;">
              <InputText v-model="new_cat_name" placeholder="New category name"
                         style="flex:1;" @keyup.enter="add_quick_cat(pos_form)" />
              <Button size="small" label="Add" :loading="cat_adding"
                      :disabled="!new_cat_name.trim()" @click="add_quick_cat(pos_form)" />
              <Button size="small" icon="pi pi-times" severity="secondary" text
                      @click="show_new_cat = false; new_cat_name = ''" />
            </div>
          </td>
          <td class="actions">
            <Button size="small" label="Save" :loading="pos_saving" @click="pos_save" />
            <Button size="small" label="Cancel" severity="secondary" @click="pos_cancel" />
          </td>
        </template>
        <template v-else>
          <td><code>{{ p.pos_key }}</code></td>
          <td>{{ p.pos_cat }}</td>
          <td>{{ p.pos_str }}</td>
          <td class="actions">
            <Button size="small" icon="pi pi-pencil" severity="secondary" text
                    @click="pos_start_edit(p)" />
            <Button size="small" icon="pi pi-trash" severity="danger" text
                    @click="pos_delete(p.pos_key)" />
          </td>
        </template>
      </tr>
      <tr v-if="positions.length === 0">
        <td colspan="4" class="empty">No positions defined yet.</td>
      </tr>
    </tbody>
  </table>
  <div v-if="pos_edit === null" class="add-row">
    <InputText v-model="pos_form.pos_key" placeholder="Key (e.g. sec1)"
               :invalid="key_invalid" style="width:8rem;"
               @input="key_dirty = true" @keyup.enter="pos_save" />
    <Select v-model="pos_form.pos_cat" :options="cat_names" editable
            placeholder="Category" style="width:13rem;" />
    <Button icon="pi pi-plus" size="small" severity="secondary" text
            title="Add new category" @click="show_new_cat = !show_new_cat" />
    <InputText v-model="pos_form.pos_str" placeholder="Display name"
               style="flex:1;" @input="on_name_input" @keyup.enter="pos_save" />
    <Button label="Add Position" :loading="pos_saving"
            :disabled="key_invalid || !pos_form.pos_key.trim() || !pos_form.pos_cat.trim() || !pos_form.pos_str.trim()"
            @click="pos_save" />
  </div>
  <div v-if="pos_edit === null" class="add-opts">
    <span v-if="key_invalid" class="key-err">
      Key must be lowercase letters, numbers, or hyphens — no spaces or underscores.
    </span>
    <span class="opt" style="margin-left:auto;">
      <Checkbox v-model="pos_form.fill_all" binary inputId="pos_fill_all" />
      <label for="pos_fill_all">Add to all days &amp; times</label>
    </span>
  </div>
  <div v-if="show_new_cat && pos_edit === null"
       style="display:flex; gap:0.5rem; align-items:center; margin-top:0.5rem;">
    <InputText v-model="new_cat_name" placeholder="New category name"
               style="flex:1;" @keyup.enter="add_quick_cat(pos_form)" />
    <Button size="small" label="Add" :loading="cat_adding"
            :disabled="!new_cat_name.trim()" @click="add_quick_cat(pos_form)" />
    <Button size="small" icon="pi pi-times" severity="secondary" text
            @click="show_new_cat = false; new_cat_name = ''" />
  </div>
</template>

<style scoped>
@import './editor.css';
.key-err { color: var(--p-red-400); font-size: 0.8rem; }
</style>
