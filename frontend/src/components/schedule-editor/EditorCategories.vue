<script setup>
import { ref, computed, watch } from 'vue'
import InputText   from 'primevue/inputtext'
import Button      from 'primevue/button'
import Message     from 'primevue/message'
import ColorPicker from 'primevue/colorpicker'
import { norm_color, text_on } from '../../lib/colors.js'

const props = defineProps({ categories: Array })
const emit  = defineEmits(['reload'])

const err         = ref('')
const local_cats  = ref([])
const new_cat     = ref('')
const adding      = ref(false)
const saving      = ref(false)

// ColorPicker works in bare hex without the '#', so the local copy holds it that way and
// it gets a '#' back on save.
watch(() => props.categories, (cats) => {
  local_cats.value = cats.map(c => ({ ...c, cat_color: norm_color(c.cat_color).slice(1) }))
}, { immediate: true })

const order_dirty = computed(() =>
  local_cats.value.length !== props.categories.length ||
  local_cats.value.some((c, i) => c.cat_name !== props.categories[i]?.cat_name)
)

// Compared against the original row by name, since reordering moves the indexes.
const changed_colors = computed(() =>
  local_cats.value.filter((c) => {
    const orig = props.categories.find(o => o.cat_name === c.cat_name)
    return orig && norm_color(c.cat_color) !== norm_color(orig.cat_color)
  })
)

const dirty = computed(() => order_dirty.value || changed_colors.value.length > 0)

function swatch_style(cat) {
  const bg = norm_color(cat.cat_color)
  return { background: bg, color: text_on(bg) }
}

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

function move_up(i) {
  if (i === 0) return
  const arr = local_cats.value.slice()
  ;[arr[i - 1], arr[i]] = [arr[i], arr[i - 1]]
  local_cats.value = arr
}

function move_down(i) {
  if (i === local_cats.value.length - 1) return
  const arr = local_cats.value.slice()
  ;[arr[i], arr[i + 1]] = [arr[i + 1], arr[i]]
  local_cats.value = arr
}

// One button for both edits: colors go first so a failure there doesn't leave the order
// saved and the colors silently dropped.
async function save_changes() {
  saving.value = true
  let ok = true
  for (const cat of changed_colors.value) {
    if (!await api_post('/api/schedule/upsert_category',
                        { cat_name: cat.cat_name, cat_color: norm_color(cat.cat_color) })) {
      ok = false
      break
    }
  }
  if (ok && order_dirty.value) {
    ok = !!await api_post('/api/schedule/reorder_categories', {
      cat_names: local_cats.value.map(c => c.cat_name).join(','),
    })
  }
  saving.value = false
  if (ok) emit('reload')
}

async function add_category() {
  const name = new_cat.value.trim()
  if (!name) return
  adding.value = true
  const ok = await api_post('/api/schedule/upsert_category', { cat_name: name })
  adding.value = false
  if (ok) { new_cat.value = ''; emit('reload') }
}

async function delete_category(cat_name) {
  if (await api_post('/api/schedule/delete_category', { cat_name })) emit('reload')
}
</script>

<template>
  <Message v-if="err" severity="error" :closable="true" @close="err = ''"
           style="margin-bottom:0.75rem;">{{ err }}</Message>

  <table class="se-table">
    <thead>
      <tr><th>Order</th><th>Category name</th><th>Chip color</th><th></th></tr>
    </thead>
    <tbody>
      <tr v-for="(cat, i) in local_cats" :key="cat.cat_name">
        <td style="width:1%; white-space:nowrap;">
          <Button size="small" icon="pi pi-arrow-up" severity="secondary" text
                  :disabled="i === 0" @click="move_up(i)" />
          <Button size="small" icon="pi pi-arrow-down" severity="secondary" text
                  :disabled="i === local_cats.length - 1" @click="move_down(i)" />
        </td>
        <td>{{ cat.cat_name }}</td>
        <td style="width:1%; white-space:nowrap;">
          <div class="color-cell">
            <ColorPicker v-model="cat.cat_color" format="hex" />
            <span class="chip-preview" :style="swatch_style(cat)">{{ cat.cat_name }}</span>
          </div>
        </td>
        <td class="actions">
          <Button size="small" icon="pi pi-trash" severity="danger" text
                  @click="delete_category(cat.cat_name)" />
        </td>
      </tr>
      <tr v-if="local_cats.length === 0">
        <td colspan="4" class="empty">No categories defined yet.</td>
      </tr>
    </tbody>
  </table>

  <div style="display:flex; gap:0.75rem; align-items:center; margin-top:0.75rem; flex-wrap:wrap;">
    <div class="add-row" style="flex:1; margin-top:0;">
      <InputText v-model="new_cat" placeholder="New category name"
                 style="flex:1;" @keyup.enter="add_category" />
      <Button label="Add Category" :loading="adding"
              :disabled="!new_cat.trim()" @click="add_category" />
    </div>
    <Button v-if="dirty" label="Save Changes" icon="pi pi-check"
            :loading="saving" @click="save_changes" />
  </div>
</template>

<style scoped>
@import './editor.css';

.color-cell {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

/* Miniature of the schedule chip, so the picked color is judged as it will be seen. */
.chip-preview {
  border-radius: 20px;
  padding: 3px 12px;
  font-size: 0.8rem;
  font-weight: 600;
  white-space: nowrap;
}
</style>
