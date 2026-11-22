<script setup>
import { ref, computed, useTemplateRef } from 'vue'
import Button        from 'primevue/button'
import RadioCheckout from '../../components/RadioCheckout.vue'
import RadioCheckin  from '../../components/RadioCheckin.vue'
import RadiosList    from '../../components/RadiosList.vue'

// The Radios tab: Check Out / Check In at the top for the radio roles, then the lists.
// The forms mount only while open, so the camera (BarcodeScanner) starts only when someone
// presses a button, never just because the tab was opened.

const props = defineProps({ role: String })
const RADIO_ROLES = ['organizer', 'owner', 'checkin']
const can_radio = computed(() => RADIO_ROLES.includes(props.role))

const mode = ref(null)      // null | 'out' | 'in'
const list = useTemplateRef('list')

function toggle(m) { mode.value = mode.value === m ? null : m }

defineExpose({ reload: () => list.value?.reload() })
</script>

<template>
  <div class="tab">
    <div v-if="can_radio" class="top">
      <Button label="Check Out" icon="pi pi-arrow-right" :severity="mode === 'out' ? undefined : 'secondary'"
              @click="toggle('out')" />
      <Button label="Check In" icon="pi pi-reply" :severity="mode === 'in' ? undefined : 'secondary'"
              @click="toggle('in')" />
    </div>
    <RadioCheckout v-if="mode === 'out'" @changed="list?.reload()" @close="mode = null" />
    <RadioCheckin  v-if="mode === 'in'"  @changed="list?.reload()" @close="mode = null" />
    <RadiosList ref="list" :role="props.role" />
  </div>
</template>

<style scoped>
.tab { display: flex; flex-direction: column; gap: 1rem; }
.top { display: flex; gap: 0.5rem; flex-wrap: wrap; }
</style>
