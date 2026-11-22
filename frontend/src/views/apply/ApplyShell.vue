<script setup>
import { useRoute } from 'vue-router'
import PortalLayout from '../../components/PortalLayout.vue'

const route = useRoute()
</script>

<template>
  <PortalLayout band="Volunteers" :signed-in="!!route.meta.signedIn">
    <div class="apply-body">
      <RouterView />
    </div>
  </PortalLayout>
</template>

<style scoped>
.apply-body   { display: flex; justify-content: center; }
</style>

<style>
/* Shared by every /apply/ view. Unscoped on purpose: the children render inside this shell.
   The panel look is the conference site's side box: pale panel, thin blue border. */
.apply-card       { width: 100%; max-width: 26rem; background: var(--bsc-panel-bg);
                    border: 1px solid var(--bsc-blue); border-radius: 2px;
                    padding: 1.75rem; box-sizing: border-box; }
.apply-card h2    { margin: 0 0 0.5rem; font-size: 1.4rem; }
.apply-card p     { color: var(--p-text-muted-color); line-height: 1.5; }
.apply-card .field { display: flex; flex-direction: column; gap: 0.4rem; margin: 1.25rem 0; }
.apply-msg        { font-size: 0.9rem; margin: 1rem 0 0; }
.apply-msg.err    { color: var(--p-red-600); }
.apply-links      { margin-top: 1.25rem; font-size: 0.9rem; }
</style>
