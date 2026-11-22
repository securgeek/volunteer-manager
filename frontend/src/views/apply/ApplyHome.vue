<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import Button from 'primevue/button'
import { portal_get } from '../../composables/usePortal.js'

const router = useRouter()
// open | closed | full — the card says so up front rather than after a click.
const signup = ref('open')
onMounted(async () => {
  const { ok, body } = await portal_get('signup/status')
  if (ok) signup.value = body.state
})
</script>

<template>
  <!-- vol_signup.md §5: two equal-weight cards, no form fields on the page itself. -->
  <div class="cards">
    <div class="apply-card">
      <h2>New Volunteer</h2>
      <p v-if="signup === 'closed'">Signups open soon. Check back here to sign up and pick
         your shifts.</p>
      <p v-else-if="signup === 'full'">All volunteer shifts are currently full.</p>
      <p v-else>Sign up to volunteer and pick the shifts that work for you.</p>
      <Button label="Get started" :disabled="signup !== 'open'"
              @click="router.push('/apply/signup')" />
    </div>
    <div class="apply-card">
      <h2>Existing Volunteer</h2>
      <p>Sign in to see the shifts you've been given.</p>
      <Button label="Sign in" severity="secondary" @click="router.push('/apply/login')" />
    </div>
  </div>
</template>

<style scoped>
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(16rem, 1fr));
         gap: 1rem; width: 100%; }
.cards .apply-card { max-width: none; display: flex; flex-direction: column; }
.cards .apply-card p { flex: 1; }
</style>
