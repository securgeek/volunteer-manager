<script setup>
import { ref } from 'vue'
import Button from 'primevue/button'

import BuildSchedule  from './BuildSchedule.vue'
import ImportSchedule from './ImportSchedule.vue'

const emit = defineEmits(['done', 'skipped'])

// 'choose' | 'build' | 'csv'
const mode = ref('choose')
</script>

<template>
  <!-- ── pick a method ────────────────────────────────────────────────────── -->
  <div v-if="mode === 'choose'"
       style="display:flex; justify-content:center; align-items:flex-start;
              min-height:80vh; padding-top:6rem;">
    <div style="width:40rem; display:flex; flex-direction:column; gap:1rem;">
      <div>
        <h2 style="margin:0;">Set Up the Schedule</h2>
        <p style="color:var(--p-text-muted-color); margin:0.5rem 0 0;">
          Define the days, times and positions volunteers can be assigned to.
        </p>
      </div>

      <button class="method" @click="mode = 'build'">
        <i class="pi pi-th-large" />
        <span>
          <strong>Build one here</strong>
          <em>Step through days, categories and time slots, then set how many of each
              position you need at each time.</em>
        </span>
      </button>

      <button class="method" @click="mode = 'csv'">
        <i class="pi pi-file-import" />
        <span>
          <strong>Import a CSV</strong>
          <em>Upload a file with one row per position-at-timeslot. Best if you already
              have the schedule written down somewhere.</em>
        </span>
      </button>

      <Button label="Skip for now" severity="secondary" @click="emit('skipped')" />
    </div>
  </div>

  <!-- ── builder ──────────────────────────────────────────────────────────── -->
  <BuildSchedule v-else-if="mode === 'build'"
                 @done="emit('done')" @back="mode = 'choose'" />

  <!-- ── CSV import, unchanged ────────────────────────────────────────────── -->
  <div v-else>
    <div style="display:flex; justify-content:center; padding-top:2.5rem;">
      <div style="width:38rem;">
        <Button label="Choose a different method" icon="pi pi-arrow-left" text
                size="small" severity="secondary" @click="mode = 'choose'" />
      </div>
    </div>
    <ImportSchedule @done="emit('done')" @skipped="emit('skipped')" />
  </div>
</template>

<style scoped>
.method {
  display: flex;
  align-items: flex-start;
  gap: 1rem;
  width: 100%;
  text-align: left;
  padding: 1.1rem 1.25rem;
  background: var(--p-content-background);
  border: 1px solid var(--p-content-border-color);
  border-radius: 8px;
  color: inherit;
  font: inherit;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s;
}
.method:hover {
  background: var(--p-content-hover-background);
  border-color: var(--p-primary-color);
}
.method i { font-size: 1.35rem; color: var(--p-primary-color); margin-top: 0.15rem; }
.method strong { display: block; margin-bottom: 0.3rem; }
.method em {
  display: block;
  font-style: normal;
  font-size: 0.85rem;
  color: var(--p-text-muted-color);
  line-height: 1.55;
}
</style>
