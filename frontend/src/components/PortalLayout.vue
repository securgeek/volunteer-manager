<script setup>
// The volunteer pages' frame, after bsidescharm.org: a light header bar with the header logo,
// an optional dark title band, the page over a faint copy of the year's theme logo, and a
// dark footer. Used by Landing.vue and apply/ApplyShell.vue. Colors and fonts are
// portal-theme.css's.
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { usePortalBranding, portal_post } from '../composables/usePortal.js'

defineProps({
  // Text for the dark band under the header, as on the conference site's pages. Omit for none.
  band: { type: String, default: '' },
  // A volunteer is signed in on this page (route meta signedIn, via ApplyShell): the header
  // offers Sign out. There are no Volunteer / Sign in links; each page has its own buttons.
  signedIn: { type: Boolean, default: false },
})

const router = useRouter()
async function sign_out() {
  await portal_post('logout')
  router.replace('/apply/')
}

const { branding, logo_url, header_logo_url } = usePortalBranding()

// The conference's website, contact address and code of conduct come from App Settings
// (pushed with the branding). Each is left out when unset.
// The header shows the website by its host name, as "bsidescharm.org".
const site_host = computed(() => {
  try { return new URL(branding.value.conference_url).host.replace(/^www\./, '') }
  catch { return '' }
})
// The footer: the conference's name (linked to its site when there is one), then the code of
// conduct and the contact address, whichever are set.
const footer_links = computed(() => {
  const b = branding.value
  const out = []
  if (b.code_of_conduct_url) out.push({ href: b.code_of_conduct_url, text: 'Code of Conduct' })
  if (b.contact_email) out.push({ href: `mailto:${b.contact_email}`, text: b.contact_email })
  return out
})

// Upper left: the header logo, else the theme logo, else the title in text.
const brand_src = computed(() => {
  if (branding.value.has_header_logo) return header_logo_url
  if (branding.value.has_logo) return logo_url
  return null
})
</script>

<template>
  <div class="portal">
    <header class="portal-header">
      <div class="portal-inner header-row">
        <RouterLink to="/" class="brand">
          <img v-if="brand_src" :src="brand_src" :alt="branding.title" class="brand-logo" />
          <strong v-else class="brand-title">{{ branding.title }}</strong>
        </RouterLink>
        <nav class="header-nav">
          <button v-if="signedIn" type="button" class="nav-btn" @click="sign_out">Sign out</button>
          <a v-if="site_host" :href="branding.conference_url">{{ site_host }}</a>
        </nav>
      </div>
    </header>

    <div v-if="band" class="portal-band">
      <h1 class="portal-inner">{{ band }}</h1>
    </div>

    <!-- The theme logo, faint and fixed behind the page; decorative, so hidden from screen
         readers and from the pointer. -->
    <div v-if="branding.has_logo" class="portal-watermark" aria-hidden="true"
         :style="{ backgroundImage: `url(${logo_url})` }" />

    <main class="portal-inner portal-main">
      <slot />
    </main>

    <footer class="portal-footer">
      <div class="portal-inner">
        <p>
          <a v-if="branding.conference_url" :href="branding.conference_url">{{ branding.title }}</a>
          <span v-else>{{ branding.title }}</span>
          <template v-for="link in footer_links" :key="link.href">
            · <a :href="link.href">{{ link.text }}</a>
          </template>
        </p>
      </div>
    </footer>
  </div>
</template>

<style scoped>
.portal        { min-height: 100vh; display: flex; flex-direction: column; }
.portal-inner  { width: 100%; max-width: 60rem; margin: 0 auto; padding: 0 1rem;
                 box-sizing: border-box; }

/* Header, band, page and footer sit above the watermark; the bars' own backgrounds hide it. */
.portal-header, .portal-band, .portal-main, .portal-footer { position: relative; z-index: 1; }

.portal-watermark {
  position: fixed; z-index: 0; top: 55%; left: 50%; transform: translate(-50%, -50%);
  width: min(32rem, 85vw); aspect-ratio: 1;
  background: center / contain no-repeat;
  opacity: 0.06; pointer-events: none;
}

.portal-header { background: var(--bsc-header-bg); }
.header-row    { display: flex; align-items: center; justify-content: space-between;
                 gap: 1rem; min-height: 5rem; flex-wrap: wrap; }
.brand         { display: inline-flex; align-items: center; text-decoration: none; }
.brand-logo    { height: 4rem; max-width: 10rem; object-fit: contain; padding: 0.5rem 0; }
.brand-title   { font-family: var(--bsc-heading-font); color: var(--bsc-heading);
                 font-size: 1.3rem; }
.header-nav    { display: flex; gap: 1.25rem; font-family: var(--bsc-heading-font);
                 font-weight: 600; font-size: 0.8rem; }
.header-nav a,
.nav-btn       { color: var(--bsc-ink); text-decoration: none; }
.nav-btn       { background: none; border: 0; padding: 0; cursor: pointer;
                 font: inherit; letter-spacing: inherit; }
.header-nav a:hover, .nav-btn:hover,
.header-nav a.router-link-exact-active { color: var(--bsc-blue); }

.portal-band   { background: var(--bsc-band-bg); }
.portal-band h1 { color: var(--bsc-band-ink); font-size: 1.6rem; text-align: center;
                  margin: 0 auto; padding-block: 1.75rem; }

.portal-main   { flex: 1; padding-block: 2rem 3rem; }

.portal-footer { background: var(--bsc-footer-bg); color: #e6e6e6; font-size: 0.85rem;
                 text-align: center; padding-block: 1.25rem; }
.portal-footer p { margin: 0.35rem 0; }
.portal-footer a { color: #6cb8ea; }
</style>
