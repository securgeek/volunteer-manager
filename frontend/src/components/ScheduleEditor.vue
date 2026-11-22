<script setup>
import { ref } from 'vue'
import Dialog    from 'primevue/dialog'
import Tabs      from 'primevue/tabs'
import TabList   from 'primevue/tablist'
import Tab       from 'primevue/tab'
import TabPanels from 'primevue/tabpanels'
import TabPanel  from 'primevue/tabpanel'
import Message   from 'primevue/message'

import EditorDays       from './schedule-editor/EditorDays.vue'
import EditorTimes      from './schedule-editor/EditorTimes.vue'
import EditorGrid       from './schedule-editor/EditorGrid.vue'
import EditorCategories from './schedule-editor/EditorCategories.vue'

const visible    = ref(false)
const days       = ref([])
const times      = ref([])
const day_times  = ref([])
const positions  = ref([])
const slots      = ref([])
const categories = ref([])
const err        = ref('')

async function load() {
  err.value = ''
  const r = await fetch('/api/schedule/structure')
  if (!r.ok) { err.value = 'Failed to load schedule structure.'; return }
  const d = await r.json()
  days.value       = d.days
  times.value      = d.times
  day_times.value  = d.day_times ?? []
  positions.value  = d.positions
  slots.value      = d.slots
  categories.value = d.categories ?? []
}

function open() { visible.value = true; load() }
defineExpose({ open })
</script>

<template>
  <Dialog v-model:visible="visible" header="Schedule Editor"
          :style="{ width: '70rem' }" modal maximizable>
    <Message v-if="err" severity="error" :closable="true" @close="err = ''"
             style="margin-bottom:1rem;">{{ err }}</Message>

    <Tabs value="categories">
      <TabList>
        <Tab value="categories">Categories</Tab>
        <Tab value="days">Days</Tab>
        <Tab value="times">Times</Tab>
        <Tab value="positions">Positions</Tab>
      </TabList>
      <TabPanels>
        <TabPanel value="categories">
          <EditorCategories :categories="categories" @reload="load" />
        </TabPanel>
        <TabPanel value="days">
          <EditorDays :days="days" :times="times" :day-times="day_times" @reload="load" />
        </TabPanel>
        <TabPanel value="times">
          <EditorTimes :times="times" :days="days" :day-times="day_times" @reload="load" />
        </TabPanel>
        <TabPanel value="positions">
          <EditorGrid :days="days" :times="times" :day-times="day_times"
                      :positions="positions" :slots="slots"
                      :categories="categories" @reload="load" />
        </TabPanel>
      </TabPanels>
    </Tabs>
  </Dialog>
</template>
