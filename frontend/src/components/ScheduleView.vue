<script setup>
import { ref, watch } from 'vue'
import Tabs      from 'primevue/tabs'
import Tab       from 'primevue/tab'
import TabList   from 'primevue/tablist'
import TabPanels from 'primevue/tabpanels'
import TabPanel  from 'primevue/tabpanel'
import Timeslot       from './Timeslot.vue'
import TimeslotDialog from './TimeslotDialog.vue'
import { norm_color } from '../lib/colors.js'

const props = defineProps({
  schedule: { type: Array,   default: () => [] },
  role:     { type: String,  default: 'view' },
  appLogo:  { type: String,  default: null },
})

const emit = defineEmits(['schedule-updated'])

const active_day   = ref(null)
const time_sel     = ref({})   // { [day_key]: time_key }
const selected_pos = ref(null)

// ── Auto-detect current day/time ──────────────────────────────────────────────
function detect_day(sched) {
  const dow     = new Date().getDay()
  const matched = { 6: 'sat', 0: 'sun' }[dow]
  // prefer event_date match if available
  const today_str = new Date().toISOString().slice(0, 10)
  const by_date = sched.find(d => d.event_date === today_str)
  if (by_date) return by_date.key
  return sched.find(d => d.key === matched)?.key ?? sched[0]?.key ?? null
}

function detect_time(day) {
  if (!day?.times?.length) return null
  const hhmm = new Date().getHours() * 100 + new Date().getMinutes()
  let best = day.times[0].key
  for (const ts of day.times) {
    if (parseInt(ts.key) <= hhmm) best = ts.key
  }
  return best
}

watch(() => props.schedule, (sched) => {
  if (!sched.length) return
  if (!active_day.value || !sched.find(d => d.key === active_day.value)) {
    active_day.value = detect_day(sched)
  }
  for (const day of sched) {
    if (!time_sel.value[day.key]) {
      time_sel.value[day.key] = detect_time(day)
    }
  }
}, { immediate: true })

watch(active_day, (dk) => {
  if (!dk || time_sel.value[dk]) return
  const day = props.schedule.find(d => d.key === dk)
  if (day) time_sel.value[dk] = detect_time(day)
})

// ── Helpers ───────────────────────────────────────────────────────────────────
function times_for(day_key) {
  return props.schedule.find(d => d.key === day_key)?.times ?? []
}

function cats_for(day_key) {
  const tk = time_sel.value[day_key]
  if (!tk) return []
  const day = props.schedule.find(d => d.key === day_key)
  return day?.times.find(t => t.key === tk)?.categories ?? []
}

function event_date_for(day_key) {
  return props.schedule.find(d => d.key === day_key)?.event_date ?? null
}

function fmt_day(day) {
  if (!day.event_date) return day.title
  const [y, m, d] = day.event_date.split('-').map(Number)
  const dt = new Date(y, m - 1, d)
  return dt.toLocaleDateString('en-US', { weekday: 'short' }) + ' ' + m + '/' + d
}

// ── Dialog ────────────────────────────────────────────────────────────────────
function open_slot(pos) {
  // reassign to ensure watcher fires even if same slot re-clicked
  selected_pos.value = null
  setTimeout(() => { selected_pos.value = { ...pos } }, 0)
}

// For the replacement banner (ShiftFlags.vue via Admin.vue): open a slot by its
// `day_time_pos` key without the user hunting for it in the tabs.
function open_slot_by_key(key) {
  for (const day of props.schedule)
    for (const time of day.times || [])
      for (const cat of time.categories || [])
        for (const pos of cat.positions || [])
          if (pos.key === key) { open_slot(pos); return true }
  return false
}
defineExpose({ open_slot_by_key })

function on_schedule_updated(new_schedule) {
  emit('schedule-updated', new_schedule)
}
</script>

<template>
  <div v-if="!schedule.length" class="loading">
    <i class="pi pi-spin pi-spinner" style="font-size:2rem;" />
  </div>

  <div v-else style="position: relative; min-height: 72vh; overflow: hidden;">
    <img v-if="appLogo" :src="appLogo" class="logo-watermark" />
    <Tabs v-model:value="active_day" scrollable>
      <TabList>
        <Tab v-for="day in schedule" :key="day.key" :value="day.key">
          {{ fmt_day(day) }}
        </Tab>
      </TabList>

      <TabPanels>
        <TabPanel v-for="day in schedule" :key="day.key" :value="day.key">
          <div class="day-body">

            <!-- vertical time nav -->
            <nav class="time-nav">
              <div v-for="ts in times_for(day.key)" :key="ts.key"
                   class="time-item" :class="{ active: time_sel[day.key] === ts.key }"
                   @click="time_sel[day.key] = ts.key">
                {{ ts.title }}
              </div>
            </nav>

            <!-- categories + chips -->
            <div class="cat-area">
              <div v-if="!cats_for(day.key).length" class="empty-msg">No slots.</div>
              <div v-for="cat in cats_for(day.key)" :key="cat.key" class="cat-block">
                <div class="cat-label">
                  <span class="cat-swatch" :style="{ background: norm_color(cat.color) }" />
                  {{ cat.key }}
                </div>
                <div class="chip-row">
                  <Timeslot v-for="pos in cat.positions" :key="pos.key"
                            :pos="pos" :event_date="event_date_for(day.key)"
                            :color="cat.color" @click="open_slot(pos)" />
                </div>
              </div>
            </div>

          </div>
        </TabPanel>
      </TabPanels>
    </Tabs>
  </div>

  <TimeslotDialog :pos="selected_pos" :role="props.role"
                  @schedule-updated="on_schedule_updated" />
</template>

<style scoped>
.loading {
  display: flex;
  justify-content: center;
  align-items: center;
  min-height: 50vh;
}

.day-body {
  display: flex;
  min-height: 65vh;
}

/* ── time sidebar ─────────────────────────────────────────────────────────── */
.time-nav {
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
  flex-shrink: 0;
  width: 10rem;
  padding: 0.5rem 0.75rem 0.5rem 0;
  border-right: 1px solid var(--p-content-border-color);
}

.time-item {
  padding: 0.55rem 0.8rem;
  border-radius: 6px;
  cursor: pointer;
  font-size: 0.9rem;
  white-space: nowrap;
  transition: background 0.12s;
}
.time-item:hover { background: var(--p-content-hover-background); }
.time-item.active {
  background: var(--p-primary-color);
  color: var(--p-primary-contrast-color);
  font-weight: 600;
}
.time-item.active:hover { filter: brightness(1.1); }

/* ── category + chip area ─────────────────────────────────────────────────── */
.cat-area {
  flex: 1;
  padding: 0.5rem 0 0.5rem 1.25rem;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 1.25rem;
}

.cat-block  { }
.cat-label  {
  font-size: 0.75rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.07em;
  color: var(--p-text-muted-color);
  margin-bottom: 0.55rem;
}

.cat-swatch {
  display: inline-block;
  width: 0.6rem;
  height: 0.6rem;
  border-radius: 2px;
  margin-right: 0.35rem;
  vertical-align: baseline;
}

.chip-row {
  display: flex;
  flex-wrap: wrap;
  gap: 0.65rem;
}

.empty-msg { color: var(--p-text-muted-color); font-style: italic; }

.logo-watermark {
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  object-fit: contain;
  opacity: 0.07;
  pointer-events: none;
  user-select: none;
}
</style>
