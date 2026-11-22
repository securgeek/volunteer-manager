<script setup>
import { computed } from 'vue'
import { norm_color, text_on } from '../lib/colors.js'

const props = defineProps({
  pos:        { type: Object, required: true },
  event_date: { type: String, default: null },
  color:      { type: String, default: null },
})

// Background comes from the category's cat_color; text is derived so it stays readable
// whatever the organizer picks.
const chip_style = computed(() => {
  const bg = norm_color(props.color)
  return { background: bg, color: text_on(bg) }
})

const isPast = computed(() => {
  if (!props.pos.time_key) return false
  const now  = new Date()
  const hhmm = now.getHours() * 100 + now.getMinutes()

  if (props.event_date) {
    const [y, m, d] = props.event_date.split('-').map(Number)
    const slot_day = new Date(y, m - 1, d)
    const today    = new Date(now.getFullYear(), now.getMonth(), now.getDate())
    if (slot_day < today) return true
    if (slot_day > today) return false
    return parseInt(props.pos.time_key) <= hhmm
  }

  // fallback: sat/sun day_key detection
  const day_map = { sat: 6, sun: 0 }
  const pos_dow = day_map[props.pos.day_key]
  if (pos_dow === undefined) return false
  const today_dow = now.getDay()
  if (today_dow > pos_dow) return true
  if (today_dow < pos_dow) return false
  return parseInt(props.pos.time_key) <= hhmm
})
</script>

<template>
  <div class="ts-chip" :style="chip_style"
       :class="[pos.category, pos.checked_in ? 'checkin-green' : (pos.vol_id && isPast ? 'checkin-red' : ''),
                { 'needs-replacement': pos.needs_replacement }]"
       :title="pos.needs_replacement ? 'Needs a replacement: this volunteer cancelled or is inactive' : undefined">
    <div class="ts-title">
      {{ pos.title }}
      <i v-if="pos.has_note" class="pi pi-comment note-icon" />
    </div>
    <template v-if="pos.vol_id">
      <hr>
      <div class="ts-vol">
        <span :class="{ gone: pos.needs_replacement }">{{ pos.vol_name }}</span>
        <template v-if="pos.vol_nick">
          <br><span class="ts-nick">{{ pos.vol_nick }}</span>
        </template>
      </div>
    </template>
    <div v-else class="empty-slot">EMPTY</div>
    <div v-if="pos.needs_replacement" class="replace-tag">NEEDS REPLACEMENT</div>
  </div>
</template>

<style scoped>
.ts-chip {
  border-radius: 20px;
  text-align: center;
  padding: 10px 16px;
  min-width: 145px;
  cursor: pointer;
  user-select: none;
  transition: filter 0.15s;
}
.ts-chip:hover { filter: brightness(1.12); }

.ts-title {
  white-space: nowrap;
  position: relative;
  font-weight: 600;
  font-size: 1rem;
}
.ts-vol   { line-height: 1.25; font-size: 0.9em; margin-top: 2px; }
.ts-nick  { font-size: 0.85em; }
hr        { margin: 6px 0; border-color: currentColor; opacity: 0.3; }

.note-icon {
  position: absolute;
  top: -2px;
  right: -4px;
  font-size: 0.85rem;
  font-weight: bolder;
}

.empty-slot {
  display: inline-block;
  background: yellow;
  color: black;
  font-weight: bolder;
  border-radius: 10px;
  padding: 2px 12px;
  margin-top: 4px;
  font-size: 0.82em;
  white-space: nowrap;
}

.checkin-green { box-shadow: 0 0 10px 4px #22c55e; }
/* Assigned to someone who has cancelled or is inactive. An outline, so it still shows
   alongside the check-in glows, which are box-shadows. */
.needs-replacement { outline: 3px dashed #f59e0b; outline-offset: 2px; }
.gone          { text-decoration: line-through; opacity: 0.75; }
.replace-tag   { margin-top: 4px; font-size: 0.65em; font-weight: 700; letter-spacing: 0.04em;
                 color: #92400e; background: #fde68a; border-radius: 3px; padding: 1px 4px;
                 display: inline-block; }
.checkin-red   {
  box-shadow: 0 0 10px 4px #ef4444;
  animation: pulse-red 2s ease-in-out infinite;
}
@keyframes pulse-red {
  0%, 100% { box-shadow: 0 0 8px 3px #ef4444; }
  50%       { box-shadow: 0 0 16px 7px #ef4444; }
}
</style>
