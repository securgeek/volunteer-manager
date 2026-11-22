<script setup>
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Button   from 'primevue/button'
import Checkbox from 'primevue/checkbox'
import { portal_post, APPLY_EMAIL_KEY } from '../../composables/usePortal.js'

// Mail scanners and link prefetchers GET the link. Rendering this page must therefore do
// nothing; the token is consumed only by the POST behind the button (portal/CLAUDE.md,
// auth invariant 2).

const route    = useRoute()
const router   = useRouter()
const token    = typeof route.query.t === 'string' ? route.query.t : ''
const remember = ref(false)
const busy     = ref(false)
const message  = ref('')

async function confirm() {
  busy.value    = true
  message.value = ''
  const { ok, body } = await portal_post('verify', { t: token, remember: remember.value ? '1' : '' })
  busy.value = false
  if (!ok) { message.value = body?.detail || 'Sign-in failed. Request a new link.'; return }

  sessionStorage.removeItem(APPLY_EMAIL_KEY)
  // replace, not push: the token must not sit in history for Back to re-submit.
  // A signup-purpose token yields a signup session with no volunteer yet.
  router.replace(body.volunteer ? '/apply/me' : '/apply/signup')
}
</script>

<template>
  <div class="apply-card">
    <h2>Confirm sign-in</h2>
    <template v-if="token">
      <p>Click below to finish signing in.</p>
      <div class="field" style="flex-direction:row; align-items:center;">
        <Checkbox v-model="remember" inputId="apply-remember" binary />
        <label for="apply-remember">Remember this browser</label>
      </div>
      <Button label="Sign in" :loading="busy" @click="confirm" />
    </template>
    <p v-else class="apply-msg err">This link is missing its token. Request a new one.</p>
    <p v-if="message" class="apply-msg err">{{ message }}</p>
    <p v-if="!token || message" class="apply-links">
      <RouterLink to="/apply/login">Request a new link</RouterLink>
    </p>
  </div>
</template>
