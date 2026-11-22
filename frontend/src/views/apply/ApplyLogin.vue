<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import Button    from 'primevue/button'
import InputText from 'primevue/inputtext'
import { portal_post, APPLY_EMAIL_KEY } from '../../composables/usePortal.js'

const router  = useRouter()
const email   = ref(sessionStorage.getItem(APPLY_EMAIL_KEY) || '')
const busy    = ref(false)
const message = ref('')
const unknown = ref(false)

async function submit() {
  if (!email.value.trim()) return
  busy.value    = true
  message.value = ''
  unknown.value = false
  const { body } = await portal_post('request_link', { email: email.value.trim() })
  busy.value = false

  if (body?.sent) {
    sessionStorage.setItem(APPLY_EMAIL_KEY, email.value.trim())
    router.push('/apply/inbox')
    return
  }
  // Not sent: an unknown address (when portal_login_disclose is on), a declined account,
  // a rate limit, or a network failure. The portal's copy covers each case.
  message.value = body?.detail || "Something went wrong. Try again."
  unknown.value = body?.known === false
}
</script>

<template>
  <form class="apply-card" @submit.prevent="submit">
    <h2>Volunteer sign in</h2>
    <p>Enter the email you signed up with and we'll send you a sign-in link. No password needed.</p>
    <div class="field">
      <label for="apply-email">Email</label>
      <InputText id="apply-email" v-model="email" type="email" autocomplete="email" autofocus />
    </div>
    <Button type="submit" label="Send me a link" :loading="busy" :disabled="!email.trim()" />
    <p v-if="message" class="apply-msg err">
      {{ message }}
      <RouterLink v-if="unknown" to="/apply/signup">Sign up</RouterLink>
    </p>
  </form>
</template>
