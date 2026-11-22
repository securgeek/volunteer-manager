<script setup>
import { computed } from 'vue'
import Checkbox from 'primevue/checkbox'
import Tag      from 'primevue/tag'

// Two shifts, plus optional extras or "any time", from the portal's timeslots
// (GET /api/portal/timeslots). Shared by signup (ApplySignup) and editing (ApplyMe), so the
// rules can't drift apart: exactly two, no overlap, full ones can't be newly picked.
//
// v-model:prefs, v-model:extras and v-model:add-any; every change emits a fresh array.
//   held      — keys the volunteer already holds, which stay pickable even when full: their
//               place is already counted (portal routers/signup.py _check_picks).
//   scheduled — keys they're assigned to; tagged, so dropping one is a visible choice.

const props = defineProps({
  slots:     { type: Array, required: true },
  prefs:     { type: Array, required: true },
  extras:    { type: Array, required: true },
  addAny:    { type: Boolean, default: false },
  held:      { type: Array, default: () => [] },
  scheduled: { type: Array, default: () => [] },
  idPrefix:  { type: String, default: 'sp' },
})
const emit = defineEmits(['update:prefs', 'update:extras', 'update:addAny'])

const days = computed(() => {
  const out = []
  for (const t of props.slots) {
    let day = out.find(d => d.day_key === t.day_key)
    if (!day) out.push(day = { day_key: t.day_key, day_str: t.day_str, slots: [] })
    day.slots.push(t)
  }
  return out
})

// Why a timeslot can't be picked right now, or '' if it can.
function blocked(t) {
  if (props.prefs.includes(t.key)) return ''
  if (t.full && !props.held.includes(t.key)) return 'Full'
  if (props.prefs.length >= 2) return ' '
  if (props.prefs.some(k => t.overlaps.includes(k))) return 'Overlaps your pick'
  return ''
}

function toggle_pref(t) {
  if (props.prefs.includes(t.key)) {
    emit('update:prefs', props.prefs.filter(k => k !== t.key))
    return
  }
  if (blocked(t)) return
  emit('update:prefs', [...props.prefs, t.key])
  if (props.extras.includes(t.key)) emit('update:extras', props.extras.filter(k => k !== t.key))
}

// A click inside the checkbox is the checkbox's (its update:modelValue toggles); anything
// else on the row toggles here.
function on_row_click(event, t) {
  if (event.target.closest('.p-checkbox')) return
  toggle_pref(t)
}

function toggle_extra(t) {
  emit('update:extras', props.extras.includes(t.key)
    ? props.extras.filter(k => k !== t.key) : [...props.extras, t.key])
}
</script>

<template>
  <div v-for="d in days" :key="d.day_key" class="day">
    <h3>{{ d.day_str }}</h3>
    <!-- One click, one toggle. The checkbox is the control (mouse, and Space from the
         keyboard); the row toggles only for clicks that didn't land on the checkbox, so the
         whole row stays a big tap target. Both reacting to one click flipped the shift and
         the box in opposite directions. The name is aria-label, not <label for>: a label
         click would reach the checkbox and the row both. -->
    <div v-for="t in d.slots" :key="t.key" class="slot"
         :class="{ picked: prefs.includes(t.key), off: blocked(t) }" @click="on_row_click($event, t)">
      <Checkbox :modelValue="prefs.includes(t.key)" binary :disabled="!!blocked(t)"
                :ariaLabel="`${d.day_str} ${t.time_str}`" @update:modelValue="toggle_pref(t)" />
      <span class="slot-label">{{ t.time_str }}</span>
      <Tag v-if="scheduled.includes(t.key)" severity="success" value="Scheduled" />
      <Tag v-if="prefs.includes(t.key)" severity="info"
           :value="prefs.indexOf(t.key) === 0 ? '1st choice' : '2nd choice'" />
      <Tag v-else-if="blocked(t) === 'Full'" severity="secondary" value="Full" />
      <span v-else-if="blocked(t).trim()" class="left">{{ blocked(t) }}</span>
      <span v-else-if="!held.includes(t.key)" class="left">
        {{ t.remaining }} spot{{ t.remaining === 1 ? '' : 's' }} left
      </span>
    </div>
  </div>

  <h3 class="extra-head">Also available if needed <span class="opt">(optional)</span></h3>
  <div class="check">
    <Checkbox :modelValue="addAny" :inputId="`${idPrefix}-any`" binary
              @update:modelValue="v => emit('update:addAny', v)" />
    <label :for="`${idPrefix}-any`">Any time</label>
  </div>
  <div v-if="!addAny" class="extras">
    <template v-for="t in slots" :key="t.key">
      <div v-if="!prefs.includes(t.key)" class="check">
        <Checkbox :modelValue="extras.includes(t.key)" binary
                  :inputId="`${idPrefix}-x-${t.key}`" @update:modelValue="toggle_extra(t)" />
        <label :for="`${idPrefix}-x-${t.key}`">{{ t.label }}</label>
      </div>
    </template>
  </div>
</template>

<style scoped>
.day h3, .extra-head { font-size: 0.95rem; margin: 1.25rem 0 0.5rem; }
.opt       { color: var(--p-text-muted-color); font-weight: normal; font-size: 0.85em; }
.check     { display: flex; gap: 0.5rem; align-items: flex-start; }
.check label { line-height: 1.4; }
.slot      { display: flex; align-items: center; gap: 0.6rem; padding: 0.55rem 0.6rem;
             border: 1px solid var(--p-content-border-color); border-radius: 6px;
             margin-bottom: 0.4rem; cursor: pointer; }
.slot.picked { border-color: var(--p-primary-color); }
.slot.off  { opacity: 0.55; cursor: not-allowed; }
.slot-label { flex: 1; }
.left      { font-size: 0.8rem; color: var(--p-text-muted-color); }
.extras    { display: flex; flex-direction: column; gap: 0.45rem; margin-top: 0.5rem; }
</style>
