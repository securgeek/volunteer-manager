<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import Button from 'primevue/button'
import { portal_post, portal_get, APPLY_EMAIL_KEY } from '../../composables/usePortal.js'

const RESEND_COOLDOWN_SEC = 60
const POLL_MS             = 5000

const router   = useRouter()
const email    = sessionStorage.getItem(APPLY_EMAIL_KEY) || ''
const cooldown = ref(RESEND_COOLDOWN_SEC)
const message  = ref('')
const error    = ref(false)

let tick_timer = null
let poll_timer = null

function start_cooldown() {
  cooldown.value = RESEND_COOLDOWN_SEC
  clearInterval(tick_timer)
  tick_timer = setInterval(() => {
    if (--cooldown.value <= 0) clearInterval(tick_timer)
  }, 1000)
}

async function resend() {
  const { body } = await portal_post('request_link', { email })
  message.value = body?.detail || ''
  error.value   = !body?.sent
  if (body?.sent) start_cooldown()
}

// The mail link usually opens in a new tab. Polling /me lets this tab notice the sign-in
// and move on by itself — the session cookie is shared across tabs.
async function poll() {
  const { ok } = await portal_get('me')
  if (ok) router.replace('/apply/me')
}

onMounted(() => {
  if (!email) { router.replace('/apply/login'); return }
  start_cooldown()
  poll_timer = setInterval(poll, POLL_MS)
})
onUnmounted(() => { clearInterval(tick_timer); clearInterval(poll_timer) })
</script>

<template>
  <div class="apply-card">
    <h2>Check your email</h2>
    <p>
      We sent a sign-in link to <strong>{{ email }}</strong>. It works once and expires
      shortly. This page will move on by itself once you've signed in.
    </p>
    <Button :label="cooldown > 0 ? `Resend in ${cooldown}s` : 'Resend link'"
            severity="secondary" :disabled="cooldown > 0" @click="resend" />
    <p v-if="message" class="apply-msg" :class="{ err: error }">{{ message }}</p>
    <p class="apply-links">
      Wrong address? <RouterLink to="/apply/login">Use a different email</RouterLink>
    </p>
  </div>
</template>
