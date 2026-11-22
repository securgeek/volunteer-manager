<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import Button from 'primevue/button'
import PortalLayout from '../components/PortalLayout.vue'
import { usePortalBranding, portal_get } from '../composables/usePortal.js'

const router = useRouter()
// The year's theme logo, large and centred, as on bsidescharm.org's home page.
const { branding, logo_url } = usePortalBranding()

// The words are the organizers' (Volunteer Portal → Landing page in the scheduler), pushed to
// the portal, which renders the Markdown parts to safe HTML (portal/content.py: no raw HTML,
// no images, web and email links only) and sends the opportunity cards as data. Until it
// answers, the page shows its frame and buttons only.
const page = ref(null)
// This page is the public portal's front door. On the admin hostname nginx already sends a
// hard load of / to /admin (default.conf, port 8080); this catches the in-app ways here —
// the router's catch-all, the logo link on /apply/ pages. The public host 404s every
// scheduler endpoint, so only there does setup_status not answer ok. A 502 or a network
// error leaves the page where it is.
onMounted(async () => {
  portal_get('landing').then(r => { if (r.ok) page.value = r.body })
  try {
    const resp = await fetch('/api/setup_status')
    if (resp.ok) router.replace('/admin')
  } catch { /* scheduler unreachable — the landing page still renders */ }
})
</script>

<template>
  <PortalLayout band="Call for Volunteers">
    <img v-if="branding.has_logo" :src="logo_url" :alt="branding.title" class="theme-logo" />
    <section class="intro">
      <!-- eslint-disable-next-line vue/no-v-html -- the portal's sanitised render -->
      <div v-if="page" class="md" v-html="page.intro" />
      <div class="cta">
        <Button label="Volunteer with us" icon="pi pi-arrow-right" iconPos="right"
                @click="router.push('/apply/')" />
        <Button label="Already signed up? Sign in" severity="secondary" outlined
                @click="router.push('/apply/login')" />
      </div>
    </section>

    <div v-if="page" class="columns">
      <div>
        <template v-if="page.roles.length">
          <h2 v-if="page.roles_heading">{{ page.roles_heading }}</h2>
          <div class="roles">
            <div v-for="r in page.roles" :key="r.name" class="role">
              <i :class="`pi pi-${r.icon}`" aria-hidden="true" />
              <div>
                <h3>{{ r.name }}</h3>
                <p v-if="r.text">{{ r.text }}</p>
              </div>
            </div>
          </div>
        </template>
        <!-- eslint-disable-next-line vue/no-v-html -- the portal's sanitised render -->
        <div class="md main" v-html="page.main" />
      </div>

      <!-- eslint-disable-next-line vue/no-v-html -- the portal's sanitised render -->
      <aside v-if="page.sidebar.trim()" class="notes md" v-html="page.sidebar" />
    </div>
  </PortalLayout>
</template>

<style scoped>
.theme-logo    { display: block; margin: 0 auto 2rem; width: min(20rem, 70vw); height: auto; }
.intro         { max-width: 46rem; margin: 0 auto 2.5rem; }
/* The intro's first paragraph is the lead, as the page always had it. */
.intro .md :deep(p:first-child) { font-size: clamp(1.35rem, 3.5vw, 1.9rem); line-height: 1.3;
                 text-align: center; margin: 0 0 1.5rem; }
.cta           { display: flex; gap: 0.75rem; justify-content: center; flex-wrap: wrap;
                 margin-top: 2rem; }

.columns       { display: grid; grid-template-columns: minmax(0, 1fr) 18rem; gap: 2.5rem;
                 align-items: start; }
@media (max-width: 760px) {
  .columns     { grid-template-columns: minmax(0, 1fr); }
}
.columns h2, .columns .md :deep(h2) { font-size: clamp(1.5rem, 4vw, 2rem); margin: 0 0 1rem; }
.roles + .main :deep(h2:first-child) { margin-top: 2.5rem; }

.roles         { display: grid; grid-template-columns: repeat(auto-fit, minmax(13rem, 1fr));
                 gap: 0.75rem; }
.role          { display: flex; gap: 0.85rem; align-items: flex-start; padding: 1rem;
                 border: 1px solid #e3e6ea; border-radius: 3px; background: #ffffff; }
.role i        { font-size: 1.3rem; color: var(--bsc-blue); margin-top: 0.15rem; }
.role h3       { margin: 0 0 0.2rem; font-size: 1rem; }
.role p        { margin: 0; font-size: 0.95rem; line-height: 1.45; }

.md :deep(ul), .md :deep(ol) { margin: 0; padding-left: 1.25rem; line-height: 1.7; }

.notes         { background: var(--bsc-panel-bg); border: 1px solid var(--bsc-blue);
                 border-radius: 2px; padding: 0.25rem 1rem; font-size: 0.95rem; }
.notes :deep(p) { margin: 1rem 0; line-height: 1.55; }
</style>
