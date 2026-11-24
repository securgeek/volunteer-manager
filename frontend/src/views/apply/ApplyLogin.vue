<script setup>
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import Button    from 'primevue/button'
import InputText from 'primevue/inputtext'
import { portal_post, APPLY_EMAIL_KEY } from '../../composables/usePortal.js'
import { check, problem as rule_problem } from '../../lib/portal_rules.js'

const router  = useRouter()
const email   = ref(sessionStorage.getItem(APPLY_EMAIL_KEY) || '')
const busy    = ref(false)
const message = ref('')
const unknown = ref(false)
// The address, by the portal's rules (lib/portal_rules.js); '' when it's fine.
const email_problem = computed(() => rule_problem('email', email.value))
const touched = ref(false)

async function submit() {
  touched.value = true
  if (email_problem.value) return
  email.value   = check('email', email.value).clean
  busy.value    = true
  message.value = ''
  unknown.value = false
  const { body } = await portal_post('request_link', { email: email.value })
  busy.value = false

  if (body?.sent) {
    sessionStorage.setItem(APPLY_EMAIL_KEY, email.value)
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
      <InputText id="apply-email" v-model="email" type="email" autocomplete="email" autofocus
                 :invalid="touched && !!email_problem" @blur="touched = !!email.trim()" />
      <small v-if="touched && email_problem" class="problem">{{ email_problem }}</small>
    </div>
    <Button type="submit" label="Send me a link" :loading="busy" :disabled="!!email_problem" />
    <p v-if="message" class="apply-msg err">
      {{ message }}
      <RouterLink v-if="unknown" to="/apply/signup">Sign up</RouterLink>
    </p>
  </form>
</template>

<style scoped>
.problem { color: var(--p-red-400); }
</style>
