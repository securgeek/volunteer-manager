<script setup>
import { ref } from 'vue'
import InputText from 'primevue/inputtext'
import Password  from 'primevue/password'
import Button    from 'primevue/button'
import Message   from 'primevue/message'

const props = defineProps({ appTitle: String })
const emit  = defineEmits(['done'])

const username   = ref('')
const password   = ref('')
const pw_confirm = ref('')
const error_msg  = ref('')
const loading    = ref(false)

async function submit() {
  error_msg.value = ''
  if (!username.value.trim() || !password.value) {
    error_msg.value = 'Username and password are required.'
    return
  }
  if (password.value !== pw_confirm.value) {
    error_msg.value = 'Passwords do not match.'
    return
  }
  if (password.value.length < 8) {
    error_msg.value = 'Password must be at least 8 characters.'
    return
  }
  loading.value = true
  const form = new FormData()
  form.append('username', username.value.trim())
  form.append('password', password.value)
  try {
    const resp = await fetch('/api/setup/create_admin', { method: 'POST', body: form })
    if (resp.ok) {
      emit('done')
    } else {
      const body = await resp.json().catch(() => null)
      error_msg.value = body?.detail || 'Failed to create admin account.'
    }
  } catch {
    error_msg.value = 'Unable to reach the server.'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div style="display:flex; justify-content:center; align-items:center; min-height:80vh;">
    <div style="width:22rem; display:flex; flex-direction:column; gap:1rem;">
      <div style="text-align:center; margin-bottom:0.25rem;">
        <h2 style="margin:0;">{{ appTitle }}</h2>
        <p style="color:var(--p-text-muted-color); margin:0.5rem 0 0;">
          No users found. Create the first organizer account.
        </p>
      </div>
      <Message v-if="error_msg" severity="error" :closable="false">{{ error_msg }}</Message>
      <InputText v-model="username"   placeholder="Username"         @keyup.enter="submit" fluid />
      <Password  v-model="password"   placeholder="Password"         :feedback="true"  @keyup.enter="submit" fluid />
      <Password  v-model="pw_confirm" placeholder="Confirm password" :feedback="false" @keyup.enter="submit" fluid />
      <Button label="Create Admin Account" :loading="loading" @click="submit" fluid />
    </div>
  </div>
</template>
