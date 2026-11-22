<script setup>
import { ref, computed, watch } from 'vue'
import Select          from 'primevue/select'
import Button          from 'primevue/button'
import Message         from 'primevue/message'
import Tabs            from 'primevue/tabs'
import TabList         from 'primevue/tablist'
import Tab             from 'primevue/tab'
import TabPanels       from 'primevue/tabpanels'
import TabPanel        from 'primevue/tabpanel'
import Accordion       from 'primevue/accordion'
import AccordionPanel  from 'primevue/accordionpanel'
import AccordionHeader from 'primevue/accordionheader'
import AccordionContent from 'primevue/accordioncontent'

const props = defineProps({
  days:       Array,
  times:      Array,
  dayTimes:   Array,
  positions:  Array,
  slots:      Array,
  categories: Array,
})
const emit = defineEmits(['reload'])

const cat_order = computed(() => props.categories.map(c => c.cat_name))

const err         = ref('')
const slot_day    = ref('')
const slot_time   = ref('')
const slot_pos    = ref('')
const slot_saving = ref(false)

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

// Only offer times the chosen day actually runs.
const add_times = computed(() => {
  if (!slot_day.value) return props.times
  const running = new Set(
    (props.dayTimes ?? []).filter(dt => dt.day_key === slot_day.value).map(dt => dt.time_key)
  )
  return props.times.filter(t => running.has(t.time_key))
})

watch(add_times, (opts) => {
  if (slot_time.value && !opts.some(t => t.time_key === slot_time.value)) slot_time.value = ''
})

async function slot_add() {
  if (!slot_day.value || !slot_time.value || !slot_pos.value) return
  slot_saving.value = true
  const ok = await api_post('/api/schedule/add_slot', {
    day_key: slot_day.value, time_key: slot_time.value, pos_key: slot_pos.value,
  })
  slot_saving.value = false
  if (ok) { slot_day.value = ''; slot_time.value = ''; slot_pos.value = ''; emit('reload') }
}
async function slot_remove(day_key, time_key, pos_key) {
  if (await api_post('/api/schedule/remove_slot', { day_key, time_key, pos_key })) emit('reload')
}

const slots_tree = computed(() => {
  const day_lbl  = Object.fromEntries(props.days.map(d => [d.day_key,  d.day_str]))
  const time_lbl = Object.fromEntries(props.times.map(t => [t.time_key, t.time_str]))
  const pos_lbl  = Object.fromEntries(props.positions.map(p => [p.pos_key, p.pos_str]))
  const pos_cat  = Object.fromEntries(props.positions.map(p => [p.pos_key, p.pos_cat]))
  const order    = cat_order.value

  const tree = {}
  for (const s of props.slots) {
    const cat = pos_cat[s.pos_key] || 'Other'
    if (!tree[s.day_key])                  tree[s.day_key] = {}
    if (!tree[s.day_key][s.time_key])      tree[s.day_key][s.time_key] = {}
    if (!tree[s.day_key][s.time_key][cat]) tree[s.day_key][s.time_key][cat] = []
    tree[s.day_key][s.time_key][cat].push({
      pos_key: s.pos_key, pos_str: pos_lbl[s.pos_key] || s.pos_key,
      day_key: s.day_key, time_key: s.time_key, assigned: s.assigned,
    })
  }

  return Object.keys(tree).sort().map(dk => ({
    key: dk, label: day_lbl[dk] || dk,
    times: Object.keys(tree[dk]).sort().map(tk => ({
      key: tk, label: time_lbl[tk] || tk,
      categories: [
        ...order
          .filter(c => tree[dk][tk][c])
          .map(c => ({ key: c, slots: tree[dk][tk][c] })),
        ...Object.keys(tree[dk][tk])
          .filter(c => !order.includes(c))
          .map(c => ({ key: c, slots: tree[dk][tk][c] })),
      ],
    })),
  }))
})

const slot_sel_day  = ref('')
const slot_sel_time = ref('')
const slot_acc_val  = ref([])

watch(slots_tree, (tree) => {
  if (!tree.length) return
  if (!tree.find(d => d.key === slot_sel_day.value))
    slot_sel_day.value = tree[0].key
}, { immediate: true })

watch(slot_sel_day, (dk) => {
  const day = slots_tree.value.find(d => d.key === dk)
  if (!day?.times.length) { slot_sel_time.value = ''; return }
  if (!day.times.find(t => t.key === slot_sel_time.value))
    slot_sel_time.value = day.times[0].key
})

watch([slot_sel_day, slot_sel_time], ([dk, tk]) => {
  const day  = slots_tree.value.find(d => d.key === dk)
  const time = day?.times.find(t => t.key === tk)
  slot_acc_val.value = time?.categories.map(c => c.key) ?? []
}, { immediate: true })

const slot_cats = computed(() => {
  const day  = slots_tree.value.find(d => d.key === slot_sel_day.value)
  const time = day?.times.find(t => t.key === slot_sel_time.value)
  return time?.categories ?? []
})
</script>

<template>
  <Message v-if="err" severity="error" :closable="true" @close="err = ''"
           style="margin-bottom:0.75rem;">{{ err }}</Message>

  <div class="add-row" style="margin-bottom:1.25rem;">
    <Select v-model="slot_day"  :options="days"      option-value="day_key"  option-label="day_str"
            placeholder="Day"      style="flex:1; min-width:8rem;" />
    <Select v-model="slot_time" :options="add_times" option-value="time_key" option-label="time_str"
            :placeholder="slot_day && add_times.length === 0 ? 'No times on this day' : 'Time'"
            :disabled="!slot_day" style="flex:1; min-width:9rem;" />
    <Select v-model="slot_pos"  :options="positions" option-value="pos_key"  option-label="pos_str"
            placeholder="Position" style="flex:2; min-width:12rem;" />
    <Button label="Add Slot" :loading="slot_saving"
            :disabled="!slot_day || !slot_time || !slot_pos"
            @click="slot_add" />
  </div>

  <div v-if="slots_tree.length === 0" class="empty">No slots defined yet.</div>

  <template v-else>
    <Tabs v-model:value="slot_sel_day">
      <TabList>
        <Tab v-for="d in slots_tree" :key="d.key" :value="d.key">{{ d.label }}</Tab>
      </TabList>
      <TabPanels>
        <TabPanel v-for="d in slots_tree" :key="d.key" :value="d.key">
          <div style="display:flex; min-height:16rem;">

            <nav class="time-nav">
              <div v-for="t in d.times" :key="t.key"
                   :class="['time-nav-item', { 'time-nav-active': slot_sel_time === t.key }]"
                   @click="slot_sel_time = t.key">
                {{ t.label }}
              </div>
            </nav>

            <div style="flex:1; padding-left:1rem; min-width:0;">
              <Accordion multiple v-model:value="slot_acc_val">
                <AccordionPanel v-for="cat in slot_cats" :key="cat.key" :value="cat.key">
                  <AccordionHeader>
                    {{ cat.key }}
                    <span class="cat-count">({{ cat.slots.length }})</span>
                  </AccordionHeader>
                  <AccordionContent>
                    <div class="slot-list">
                      <div v-for="s in cat.slots" :key="s.pos_key" class="slot-item">
                        <span class="slot-name">{{ s.pos_str }}</span>
                        <span :class="s.assigned ? 'status-assigned' : 'status-empty'">
                          {{ s.assigned ? 'Assigned' : 'Empty' }}
                        </span>
                        <Button v-if="!s.assigned" size="small" icon="pi pi-trash"
                                severity="danger" text
                                @click="slot_remove(s.day_key, s.time_key, s.pos_key)" />
                        <span v-else style="width:2.25rem; flex-shrink:0;" />
                      </div>
                    </div>
                  </AccordionContent>
                </AccordionPanel>
              </Accordion>
              <div v-if="slot_cats.length === 0" class="empty">No slots for this time.</div>
            </div>

          </div>
        </TabPanel>
      </TabPanels>
    </Tabs>
  </template>
</template>

<style scoped>
@import './editor.css';
.status-assigned { color: var(--p-green-400); }
.status-empty    { color: var(--p-text-muted-color); }
.time-nav {
  width: 9rem;
  flex-shrink: 0;
  border-right: 1px solid var(--p-content-border-color);
  display: flex;
  flex-direction: column;
  gap: 0.125rem;
  padding-right: 0.5rem;
}
.time-nav-item {
  padding: 0.4rem 0.75rem;
  border-radius: 6px;
  border-left: 2px solid transparent;
  cursor: pointer;
  font-size: 0.875rem;
  color: var(--p-text-muted-color);
  transition: background 0.15s, color 0.15s;
}
.time-nav-item:hover { background: var(--p-content-hover-background); color: var(--p-text-color); }
.time-nav-active {
  background: var(--p-content-hover-background);
  color: var(--p-text-color);
  border-left-color: var(--p-primary-color);
}
.slot-list { display: flex; flex-direction: column; }
.slot-item {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.3rem 0.5rem;
  border-radius: 4px;
  font-size: 0.875rem;
}
.slot-item:hover { background: var(--p-content-hover-background); }
.slot-name { flex: 1; }
.cat-count {
  margin-left: 0.4rem;
  font-size: 0.75rem;
  font-weight: normal;
  color: var(--p-text-muted-color);
}
</style>
