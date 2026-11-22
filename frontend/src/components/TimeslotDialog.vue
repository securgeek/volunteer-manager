<script setup>
import { ref, computed, watch } from 'vue'
import Dialog   from 'primevue/dialog'
import Select   from 'primevue/select'
import Button   from 'primevue/button'
import Textarea from 'primevue/textarea'
import { confirm_text } from '../lib/checkin_timing.js'

const props = defineProps({
  pos:  { type: Object, default: null },
  role: { type: String, default: 'view' },
})

const emit = defineEmits(['schedule-updated'])

const visible         = ref(false)
const local_pos       = ref(null)
const avail_vols      = ref([])

// get_avail_vols tags each volunteer with why they fit, already in this order
// (backend routers/schedule.py AVAIL_GROUPS).
const GROUPS = [
  { key: 'requested', label: 'Requested this shift' },
  { key: 'flexible',  label: 'Flexible, same day' },
  { key: 'available', label: 'Also available' },
  { key: 'no_picks',  label: 'No picks on file' },
]
const grouped_vols = computed(() => GROUPS
  .map(g => ({ label: g.label, items: avail_vols.value.filter(v => v.group === g.key) }))
  .filter(g => g.items.length))
const selected_vol_id = ref(null)
const show_vol_select = ref(false)
const slot_note       = ref(null)
const note_text       = ref('')
const slot_ratings    = ref([])
const checkin_confirm = ref(null)
const checkin_err     = ref('')

watch(() => props.pos, (pos) => {
  if (pos) {
    local_pos.value       = { ...pos }
    checkin_confirm.value = null
    checkin_err.value     = ''
    // Straight to the picker when the slot is empty, or when its volunteer has cancelled or
    // is inactive and the point of opening it is to choose someone else.
    show_vol_select.value = !pos.vol_id || !!pos.needs_replacement
    selected_vol_id.value = null
    slot_ratings.value    = []
    fetch_avail_vols(pos.key)
    fetch_slot_note(pos.key)
    if (pos.vol_id) fetch_slot_rating(pos.vol_id, pos.key)
    visible.value = true
  } else {
    visible.value = false
  }
})

function post(url, fields) {
  const fd = new FormData()
  for (const [k, v] of Object.entries(fields)) fd.append(k, v)
  return fetch('/api/' + url, { method: 'POST', body: fd })
}

function fetch_avail_vols(pos_key) {
  post('get_avail_vols', { pos_key }).then(r => {
    if (r.ok) r.json().then(d => { avail_vols.value = d })
  })
}

function assign_vol() {
  if (!selected_vol_id.value) return
  post('assign_vol', { pos_key: local_pos.value.key, vol_id: selected_vol_id.value }).then(r => {
    if (r.ok) r.json().then(data => {
      emit('schedule-updated', data.schedule)
      visible.value = false
    })
  })
}

function clear_ts() {
  post('clear_timeslot', { pos_key: local_pos.value.key }).then(r => {
    if (r.ok) r.json().then(data => {
      emit('schedule-updated', data.schedule)
      local_pos.value = { ...local_pos.value, vol_id: '', vol_name: '', vol_nick: '', checked_in: false }
      show_vol_select.value = true
      selected_vol_id.value = null
      slot_ratings.value = []
      fetch_avail_vols(local_pos.value.key)
    })
  })
}

function fetch_slot_rating(vol_id, slot_key) {
  post('get_vol_rating', { vol_id, slot_key }).then(r => {
    if (r.ok) r.json().then(d => { slot_ratings.value = d })
  })
}

// More than 45 min early or 10 min late comes back as needs_confirm with nothing written;
// checkin_confirm holds that result until Check in anyway (resent with force) or Cancel.
function checkin_vol(force = false) {
  checkin_confirm.value = null
  checkin_err.value     = ''
  const fields = { vol_id: local_pos.value.vol_id, pos_key: local_pos.value.key }
  if (force) fields.force = 'true'
  post('checkin_vol', fields).then(async r => {
    const body = await r.json().catch(() => null)
    if (!r.ok)                    checkin_err.value = body?.detail || 'Check-in failed.'
    else if (body.needs_confirm)  checkin_confirm.value = body
    else                          local_pos.value = { ...local_pos.value, checked_in: true }
  })
}

function reset_vol_status() {
  post('reset_vol_status', { vol_id: local_pos.value.vol_id, pos_key: local_pos.value.key }).then(r => {
    if (r.ok) {
      local_pos.value = { ...local_pos.value, checked_in: false }
      slot_ratings.value = slot_ratings.value.filter(r => r.rating !== 'No-Show')
    }
  })
}

function submit_rating(rating) {
  post('rate_volunteer', { vol_id: local_pos.value.vol_id, slot_key: local_pos.value.key, rating }).then(r => {
    if (r.ok) r.json().then(entry => { slot_ratings.value.unshift(entry) })
  })
}

function fetch_slot_note(pos_key) {
  slot_note.value = null
  note_text.value = ''
  post('get_schedule_note', { pos_key }).then(r => {
    if (r.ok) r.json().then(data => {
      slot_note.value = Object.keys(data).length ? data : null
      note_text.value = data.note ?? ''
    })
  })
}

function save_slot_note() {
  post('set_schedule_note', { pos_key: local_pos.value.key, note: note_text.value }).then(r => {
    if (r.ok) {
      local_pos.value = { ...local_pos.value, has_note: !!note_text.value.trim() }
      fetch_slot_note(local_pos.value.key)
    }
  })
}
</script>

<template>
  <Dialog v-model:visible="visible" :style="{ width: '32rem' }" modal
          :header="local_pos ? `${local_pos.category} / ${local_pos.title}` : ''">
    <template v-if="local_pos">

      <div class="slot-meta">{{ local_pos.day }} &mdash; {{ local_pos.time }}</div>

      <!-- Assigned volunteer ──────────────────────────────── -->
      <template v-if="local_pos.vol_id">
        <div class="vol-name">
          {{ local_pos.vol_name }}
          <span v-if="local_pos.vol_nick" class="vol-nick">{{ local_pos.vol_nick }}</span>
        </div>

        <div v-if="props.role === 'organizer'" class="btn-row">
          <Button size="small" label="Replace" severity="secondary" @click="show_vol_select = true" />
          <Button size="small" label="Clear"   severity="danger" variant="outlined" @click="clear_ts()" />
        </div>

        <!-- Check-in -->
        <div v-if="props.role !== 'view'" class="btn-row">
          <Button v-if="!local_pos.checked_in"
                  size="small" icon="pi pi-check" label="Check In"
                  severity="success" @click="checkin_vol()" />
          <template v-else>
            <span class="checked-in-label">
              <i class="pi pi-check-circle" /> Checked In
            </span>
            <Button size="small" icon="pi pi-refresh" label="Reset"
                    severity="secondary" @click="reset_vol_status()" />
          </template>
          <Button v-if="!local_pos.checked_in && slot_ratings.some(r => r.rating === 'No-Show')"
                  size="small" icon="pi pi-refresh" label="Reset No-Show"
                  severity="secondary" @click="reset_vol_status()" />
        </div>
        <div v-if="checkin_confirm" class="checkin-confirm">
          <div><i class="pi pi-exclamation-triangle" /> {{ confirm_text(checkin_confirm) }}</div>
          <div class="btn-row" style="margin:0;">
            <Button size="small" icon="pi pi-check" label="Check in anyway" severity="warn"
                    @click="checkin_vol(true)" />
            <Button size="small" label="Cancel" severity="secondary" @click="checkin_confirm = null" />
          </div>
        </div>
        <div v-if="checkin_err" class="checkin-err">{{ checkin_err }}</div>

        <!-- Ratings -->
        <div v-if="props.role !== 'view'" class="btn-row">
          <template v-if="props.role === 'organizer'">
            <Button size="small" icon="pi pi-thumbs-up"   label="Good" severity="success" outlined @click="submit_rating('Good')" />
            <Button size="small" icon="pi pi-thumbs-down" label="Bad"  severity="warn"    outlined @click="submit_rating('Bad')" />
          </template>
          <Button size="small" icon="pi pi-ban" label="No-Show" severity="danger" outlined @click="submit_rating('No-Show')" />
        </div>

        <div v-if="slot_ratings.length" class="rating-list">
          <div v-for="(r, i) in slot_ratings" :key="i" class="rating-row">
            <i :class="{ 'pi pi-thumbs-up': r.rating==='Good', 'pi pi-thumbs-down': r.rating==='Bad', 'pi pi-ban': r.rating==='No-Show' }"
               :style="{ color: r.rating==='Good' ? 'var(--p-green-400)' : r.rating==='Bad' ? 'var(--p-orange-400)' : 'var(--p-red-400)' }" />
            <strong>{{ r.rating }}</strong>
            <span class="muted">{{ r.username }} &mdash; {{ r.created_at }}</span>
          </div>
        </div>
      </template>

      <!-- Volunteer select ────────────────────────────────── -->
      <template v-if="show_vol_select && props.role === 'organizer'">
        <hr class="divider">
        <Select v-model="selected_vol_id" option-value="key" :options="grouped_vols"
                option-group-label="label" option-group-children="items"
                filter option-label="name" placeholder="Select a volunteer…" fluid>
          <template #option="{ option }">
            <div v-tooltip.right="option.requested">{{ option.name }}</div>
          </template>
        </Select>
        <div class="btn-row" style="margin-top:0.4rem;">
          <Button size="small" severity="secondary" label="Assign"
                  :disabled="!selected_vol_id" @click="assign_vol()" />
          <Button v-if="local_pos.vol_id" size="small" severity="secondary"
                  label="Cancel" @click="show_vol_select = false" />
        </div>
      </template>

      <!-- Slot note ───────────────────────────────────────── -->
      <hr class="divider">
      <div v-if="slot_note" class="note-meta">
        Last updated by <strong>{{ slot_note.username }}</strong> &mdash; {{ slot_note.updated_at }}
      </div>
      <Textarea v-model="note_text" rows="3" fluid autoResize placeholder="Slot note…"
                :readonly="props.role !== 'organizer'" />
      <Button v-if="props.role === 'organizer'"
              size="small" severity="secondary" label="Save Note"
              style="margin-top:0.4rem;" @click="save_slot_note()" />

    </template>
  </Dialog>
</template>

<style scoped>
.slot-meta       { font-size: 0.85rem; color: var(--p-text-muted-color); margin-bottom: 0.75rem; }
.vol-name        { font-size: 1.05rem; font-weight: 600; margin-bottom: 0.5rem; }
.vol-nick        { font-weight: 400; color: var(--p-text-muted-color); margin-left: 0.4rem; }
.btn-row         { display: flex; gap: 0.4rem; flex-wrap: wrap; align-items: center; margin-bottom: 0.5rem; }
.checked-in-label { color: var(--p-green-400); font-weight: bold; font-size: 0.9rem; display: flex; align-items: center; gap: 0.3rem; }
.divider         { margin: 0.75rem 0; border-color: var(--p-content-border-color); }
.note-meta       { font-size: 0.8rem; color: var(--p-text-muted-color); margin-bottom: 0.35rem; }
.rating-list     { font-size: 0.82rem; max-height: 8rem; overflow-y: auto; margin-bottom: 0.5rem; }
.rating-row      { display: flex; align-items: center; gap: 0.4rem; padding: 0.15rem 0; }
.muted           { color: var(--p-text-muted-color); }
.checkin-confirm { display: flex; flex-direction: column; gap: 0.4rem; font-size: 0.85rem;
                   padding: 0.5rem 0.6rem; margin-bottom: 0.5rem;
                   border: 1px solid var(--p-orange-400); border-radius: 6px; }
.checkin-confirm .pi { color: var(--p-orange-400); }
.checkin-err     { color: var(--p-red-400); font-size: 0.85rem; margin-bottom: 0.5rem; }
</style>
