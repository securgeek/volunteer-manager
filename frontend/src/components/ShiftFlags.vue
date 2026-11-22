<script setup>
import { ref, onMounted } from 'vue'
import Message from 'primevue/message'
import Button  from 'primevue/button'

// A banner over the schedule: volunteers who dropped a shift in the portal that they're still
// assigned to (GET /api/admin/shift_flags). Volunteers may do that — it's flagged rather than
// refused — so it has to be impossible to miss.
//
// Unassign clears the slot (POST /api/clear_timeslot); Keep dismisses the flag and leaves the
// assignment. Either way the flag goes, and it would also go by itself if the volunteer picked
// the shift again. Admin.vue calls reload() on the `volunteers` and `assignment` events.
//
// Below that: slots held by volunteers who have cancelled or been made inactive. Those stay
// assigned until someone else is; Open emits `open-slot` so Admin.vue opens that slot's
// dialog, where picking a replacement is one step. Each goes once replaced, or when the
// volunteer rejoins or is reinstated.

const emit = defineEmits(['open-slot'])

const flags   = ref([])
const replace = ref([])
const acting = ref(null)
const err    = ref('')

async function reload() {
  try {
    const r = await fetch('/api/admin/shift_flags')
    if (r.ok) {
      const body = await r.json()
      flags.value   = body.flags
      replace.value = body.replace || []
    }
  } catch { /* keep the last list */ }
}
onMounted(reload)
defineExpose({ reload })

const flag_id = (f) => `${f.vol_id}|${f.day_key}|${f.time_key}`

async function post(url, fields) {
  const fd = new FormData()
  for (const [k, v] of Object.entries(fields)) fd.append(k, v)
  const r = await fetch(url, { method: 'POST', body: fd })
  if (!r.ok) {
    const b = await r.json().catch(() => null)
    throw new Error(b?.detail || 'Request failed.')
  }
}

async function unassign(f) {
  acting.value = flag_id(f); err.value = ''
  try {
    for (const s of f.slots) await post('/api/clear_timeslot', { pos_key: s.key })
    await reload()
  } catch (e) { err.value = e.message }
  finally { acting.value = null }
}

async function keep(f) {
  acting.value = flag_id(f); err.value = ''
  try {
    await post('/api/admin/shift_flags/keep',
               { vol_id: f.vol_id, day_key: f.day_key, time_key: f.time_key })
    await reload()
  } catch (e) { err.value = e.message }
  finally { acting.value = null }
}
</script>

<template>
  <Message v-if="replace.length" severity="warn" :closable="false" class="banner">
    <strong>{{ replace.length === 1 ? 'A shift needs' : `${replace.length} shifts need` }}
      a replacement.</strong>
    <div v-for="r in replace" :key="r.key" class="row">
      <span>
        {{ r.day_str }} {{ r.time_str }} · <strong>{{ r.pos_str }}</strong>
        <span class="muted">(was {{ r.name }}, {{ r.reason }})</span>
      </span>
      <Button size="small" label="Open" icon="pi pi-external-link" severity="warn"
              @click="emit('open-slot', r.key)" />
    </div>
  </Message>

  <Message v-if="flags.length" severity="warn" :closable="false" class="banner">
    <strong>{{ flags.length === 1 ? 'A volunteer has' : `${flags.length} volunteers have` }}
      dropped a shift they're scheduled for.</strong>
    <div v-for="f in flags" :key="flag_id(f)" class="row">
      <span>
        <strong>{{ f.name }}</strong> ({{ f.vol_id }}) no longer picks
        {{ f.day_str }} {{ f.time_str }}, but is assigned to
        {{ f.slots.map(s => s.pos_str).join(', ') }}.
      </span>
      <span class="actions">
        <Button size="small" label="Unassign" severity="warn" :loading="acting === flag_id(f)"
                @click="unassign(f)" />
        <Button size="small" label="Keep" severity="secondary" variant="text"
                :disabled="acting === flag_id(f)" @click="keep(f)" />
      </span>
    </div>
    <div v-if="err" class="err">{{ err }}</div>
  </Message>
</template>

<style scoped>
.banner  { margin-bottom: 1rem; }
.row     { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap;
           margin-top: 0.4rem; font-size: 0.9rem; }
.actions { display: flex; gap: 0.25rem; }
.err     { margin-top: 0.4rem; color: var(--p-red-400); font-size: 0.85rem; }
.muted   { color: var(--p-text-muted-color); }
</style>
